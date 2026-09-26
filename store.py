# -*- coding: utf-8 -*-
"""주석 누적 저장소.

화면 하나의 정체성은 (주소, 해상도) 다. URL 만으로 묶지 않는다.
  ★기기마다 화면 크기가 다르다. 같은 페이지를 1920 데스크톱과 1366 노트북에서 보면
    레이아웃이 다르고, 깨지는 곳도 다르다. URL 로만 묶으면 둘이 한 그룹으로 합쳐지고
    해상도 값은 나중에 온 것이 앞의 것을 덮어써서, "어느 해상도에서 깨졌는지" 가
    결과에서 사라진다. 그래서 해상도가 다르면 다른 화면으로 센다.

각 화면에는 정보(제목·해상도·DPR·시각)와 그 화면에서 난 콘솔 에러·실패한 네트워크
요청이 함께 남는다.

출력 2종
  latest.md         사람이 읽고 그대로 전달하는 형식
  annotations.jsonl 들어온 이벤트 원본. 프로그램을 다시 켜면 이걸 재생해 목록을 복원한다.
"""
import io
import json
import os
import threading
from collections import OrderedDict
from datetime import datetime

NOISE_CAP = 50          # 화면당 콘솔 에러·실패 요청 보관 상한(그 이상은 오래된 것부터 버린다)


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _page_id(url):
    """화면을 가리키는 식별자: 출처 + 경로. 질의문자열·해시는 뗀다.

    ★agentation 은 주석을 `feedback-annotations-<pathname>` 로 저장한다. 즉 저장 단위가
      '경로' 다. 그래서 [전체 지우기]·개별 삭제도 경로 단위로 일어난다. 우리 목록은
      (주소, 해상도) 단위라 현재 해상도만 지우면 예전 해상도로 기록된 주석이 남는다.
      지우는 범위는 저장하는 쪽과 같아야 한다."""
    try:
        from urllib.parse import urlsplit
        u = urlsplit(url or '')
        return '%s://%s%s' % (u.scheme, u.netloc, u.path)
    except Exception:
        return (url or '').split('?')[0].split('#')[0]


class Store(object):
    def __init__(self, out_dir):
        self.out_dir = out_dir
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        self.md_path = os.path.join(out_dir, 'latest.md')
        self.jsonl_path = os.path.join(out_dir, 'annotations.jsonl')
        self.lock = threading.RLock()
        self.pages = OrderedDict()      # url -> page dict
        self._replaying = False

    # ── 화면 ──────────────────────────────────────────────────
    @staticmethod
    def key_of(url, viewport):
        """화면 키 = (주소, 해상도). 해상도를 모르면 '?' 로 둔다(합치지는 않는다)."""
        return (url or '', viewport or '?')

    def _page(self, key):
        p = self.pages.get(key)
        if p is None:
            p = {
                'url': key[0], 'viewport': key[1], 'title': '', 'dpr': None,
                'referrer': '', 'in_iframe': False,
                'first_seen': _now(), 'last_seen': _now(),
                'annotations': OrderedDict(),   # id -> annotation
                'console': [], 'network': [],
            }
            self.pages[key] = p
        return p

    def touch_page(self, info):
        """주석이 없어도 화면 정보를 먼저 기록한다(콘솔·네트워크 귀속용). 화면 키를 돌려준다."""
        url = info.get('url') or ''
        if not url:
            return None
        key = self.key_of(url, info.get('viewport'))
        with self.lock:
            p = self._page(key)
            # url·viewport 는 키라서 덮어쓰지 않는다. 나머지만 채운다.
            for k_src, k_dst in (('title', 'title'), ('dpr', 'dpr'),
                                 ('referrer', 'referrer'), ('inIframe', 'in_iframe')):
                v = info.get(k_src)
                if v not in (None, ''):
                    p[k_dst] = v
            p['last_seen'] = _now()
            return key

    def merge_unknown(self, url, viewport):
        """해상도를 알기 전에 쌓인 (url, '?') 항목을 실제 해상도 화면으로 옮긴다.

        문서 로드 직후의 콘솔 에러·실패 요청은 주입 스크립트가 해상도를 알리기
        전에 도착한다. 그대로 두면 결과에 '해상도 미상' 유령 화면이 한 줄 더 생긴다."""
        if not url or not viewport:
            return
        src = self.key_of(url, '?')
        dst = self.key_of(url, viewport)
        with self.lock:
            old = self.pages.get(src)
            if old is None or src == dst:
                return
            new = self._page(dst)
            new['console'] = (old['console'] + new['console'])[-NOISE_CAP:]
            new['network'] = (old['network'] + new['network'])[-NOISE_CAP:]
            for aid, a in old['annotations'].items():
                new['annotations'].setdefault(aid, a)
            for k in ('title', 'dpr', 'referrer'):
                if not new.get(k) and old.get(k):
                    new[k] = old[k]
            new['first_seen'] = min(new['first_seen'], old['first_seen'])
            self.pages.pop(src, None)
            # ★병합 사실을 기록에 남긴다. 안 남기면 재시작 복원(replay) 때
            #   '해상도 미상' 화면이 한 줄 더 살아나 라이브와 결과가 달라진다(실측).
            if not self._replaying:
                self._append_jsonl({'t': 'merge', 'url': url, 'viewport': viewport})

    # ── 이벤트 적용 ────────────────────────────────────────────
    def apply(self, payload, persist=True):
        """주입 스크립트가 보낸 payload 하나를 반영한다. (화면 수, 주석 수) 반환."""
        with self.lock:
            key = self.touch_page(payload)
            if key is None:
                return self.counts()
            p = self.pages[key]
            kind = payload.get('kind')
            anns = payload.get('annotations') or []
            # 지우는 범위는 agentation 의 저장 범위(경로)와 같아야 한다 - _page_id 주석 참고.
            same = [pp for k, pp in self.pages.items()
                    if _page_id(k[0]) == _page_id(p['url'])]
            if kind == 'delete':
                for a in anns:
                    for pp in same:
                        pp['annotations'].pop(a.get('id'), None)
            elif kind == 'clear':
                for pp in same:
                    pp['annotations'].clear()
            else:                                   # add · update · submit · copy
                for a in anns:
                    aid = a.get('id') or str(len(p['annotations']) + 1)
                    p['annotations'][aid] = a
            if persist and not self._replaying:
                self._append_jsonl({'t': 'annotation', 'payload': payload})
                self.write_md()
            return self.counts()

    def add_console(self, key, text, level='error'):
        with self.lock:
            p = self._page(key)
            p['console'].append({'ts': _now(), 'level': level, 'text': text})
            del p['console'][:-NOISE_CAP]
            if not self._replaying:
                self._append_jsonl({'t': 'console', 'url': p['url'],
                                    'viewport': p['viewport'], 'level': level, 'text': text})

    def add_network(self, key, request_url, status, reason=''):
        with self.lock:
            p = self._page(key)
            p['network'].append({'ts': _now(), 'request': request_url,
                                 'status': status, 'reason': reason})
            del p['network'][:-NOISE_CAP]
            if not self._replaying:
                self._append_jsonl({'t': 'network', 'url': p['url'], 'viewport': p['viewport'],
                                    'request': request_url, 'status': status, 'reason': reason})

    def counts(self):
        pages = [p for p in self.pages.values() if p['annotations']]
        return len(pages), sum(len(p['annotations']) for p in pages)

    def rows(self):
        """목록용 행. (번호, 제목, 해상도, 주소, 주석수, 콘솔수, 실패요청수)"""
        out = []
        with self.lock:
            i = 0
            for p in self.pages.values():
                if not p['annotations'] and not p['console'] and not p['network']:
                    continue
                i += 1
                out.append((i, p['title'] or '(제목 없음)', p['viewport'], p['url'],
                            len(p['annotations']), len(p['console']), len(p['network'])))
        return out

    # ── 저장 ──────────────────────────────────────────────────
    def _append_jsonl(self, rec):
        try:
            with io.open(self.jsonl_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        except Exception:
            pass

    def write_md(self):
        try:
            with io.open(self.md_path, 'w', encoding='utf-8') as f:
                f.write(self.render())
        except Exception:
            pass

    def render(self):
        with self.lock:
            pages = [p for p in self.pages.values()
                     if p['annotations'] or p['console'] or p['network']]
            total = sum(len(p['annotations']) for p in pages)
            lines = [u'# 화면 주석 - %s' % _now(), u'',
                     u'- 화면 %d개 · 주석 %d건' % (len(pages), total), u'']
            if not pages:
                lines += [u'---', u'', u'(아직 주석이 없습니다)', u'']
                return u'\n'.join(lines)
            for i, p in enumerate(pages, 1):
                lines += [u'---', u'',
                          u'# [화면 %d] %s  (%s)' % (i, p['title'] or u'(제목 없음)',
                                                    p['viewport'] or u'해상도 미상'), u'',
                          u'- 주소: %s' % p['url'],
                          u'- 해상도: %s%s' % (p['viewport'] or u'미상',
                                            u' · DPR %s' % p['dpr'] if p['dpr'] else u''),
                          u'- 시각: %s ~ %s' % (p['first_seen'], p['last_seen'])]
                if p['in_iframe']:
                    lines.append(u'- 이 화면은 iframe 안이었습니다')
                if p['referrer']:
                    lines.append(u'- 이전 화면: %s' % p['referrer'])
                lines += [u'- 주석 %d건' % len(p['annotations']), u'']
                lines += self._render_annotations(list(p['annotations'].values()))
                lines += self._render_noise(p)
            return u'\n'.join(lines)

    @staticmethod
    def _render_annotations(anns):
        lines = []
        for i, a in enumerate(anns, 1):
            lines += [u'## %d. %s' % (i, a.get('element') or u'?'), u'',
                      u'> %s' % (a.get('comment') or u'(메모 없음)'), u'',
                      u'- 경로: `%s`' % (a.get('elementPath') or u''),
                      u'- 클래스: `%s`' % (a.get('cssClasses') or u'')]
            if a.get('selectedText'):
                lines.append(u'- 선택 텍스트: %s' % a['selectedText'])
            if a.get('nearbyText'):
                lines.append(u'- 주변 텍스트: %s' % str(a['nearbyText'])[:200])
            b = a.get('boundingBox')
            b = b if isinstance(b, dict) else {}     # 형이 다르면 무시(외부에서 온 값)
            if b:
                lines.append(u'- 박스: x=%s y=%s w=%s h=%s'
                             % (b.get('x'), b.get('y'), b.get('width'), b.get('height')))
            if a.get('computedStyles'):
                lines.append(u'- 스타일: %s' % str(a['computedStyles'])[:300])
            if a.get('reactComponents'):
                lines.append(u'- React: %s' % a['reactComponents'])
            if a.get('sourceFile'):
                lines.append(u'- 소스: %s' % a['sourceFile'])
            lines.append(u'')
        return lines

    @staticmethod
    def _render_noise(p):
        lines = []
        if p['console']:
            lines += [u'### 이 화면의 콘솔 에러 %d건' % len(p['console']), u'']
            for c in p['console']:
                lines.append(u'- `%s` %s' % (c['ts'], str(c['text'])[:300]))
            lines.append(u'')
        if p['network']:
            lines += [u'### 이 화면의 실패한 요청 %d건' % len(p['network']), u'']
            for n in p['network']:
                tail = u' (%s)' % n['reason'] if n['reason'] else u''
                lines.append(u'- `%s` %s → %s%s' % (n['ts'], n['request'], n['status'], tail))
            lines.append(u'')
        return lines

    # ── 복원 · 비우기 ──────────────────────────────────────────
    def replay(self):
        """프로그램을 다시 켰을 때 jsonl 을 재생해 목록을 되살린다."""
        if not os.path.exists(self.jsonl_path):
            return self.counts()
        with self.lock:
            self._replaying = True
            try:
                with io.open(self.jsonl_path, encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        t = rec.get('t')
                        if t == 'annotation':
                            self.apply(rec.get('payload') or {}, persist=False)
                        elif t == 'console':
                            self.add_console(self.key_of(rec.get('url'), rec.get('viewport')),
                                             rec.get('text'), rec.get('level') or 'error')
                        elif t == 'network':
                            self.add_network(self.key_of(rec.get('url'), rec.get('viewport')),
                                             rec.get('request'), rec.get('status'),
                                             rec.get('reason') or '')
                        elif t == 'merge':
                            self.merge_unknown(rec.get('url'), rec.get('viewport'))
            finally:
                self._replaying = False
            self.write_md()
            return self.counts()

    def export(self, dest_path):
        """현재 내용을 파일로 저장한 뒤 비운다. 저장이 실패하면 원본을 보존한다."""
        with self.lock:
            text = self.render()
            with io.open(dest_path, 'w', encoding='utf-8') as f:
                f.write(text)
            self.reset(keep_archive=True)
            return dest_path

    def reset(self, keep_archive=True):
        """목록을 비운다. keep_archive 면 jsonl 을 타임스탬프 이름으로 옮겨 남긴다."""
        with self.lock:
            if keep_archive and os.path.exists(self.jsonl_path):
                stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
                arch = os.path.join(self.out_dir, 'archive')
                if not os.path.isdir(arch):
                    os.makedirs(arch)
                try:
                    os.replace(self.jsonl_path,
                               os.path.join(arch, '%s-annotations.jsonl' % stamp))
                except Exception:
                    pass
            elif os.path.exists(self.jsonl_path):
                try:
                    os.remove(self.jsonl_path)
                except Exception:
                    pass
            self.pages.clear()
            self.write_md()
