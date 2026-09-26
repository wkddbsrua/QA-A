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
import re
import threading
from collections import OrderedDict
from datetime import datetime

NOISE_CAP = 50          # 화면당 콘솔 에러·실패 요청 보관 상한(그 이상은 오래된 것부터 버린다)


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# 우선순위는 코드로 저장하고 라벨은 한 곳에서만 만든다(나중에 바꿀 때 한 군데).
PRIORITIES = [('high', u'높음'), ('mid', u'보통'), ('low', u'낮음')]
PRIORITY_LABEL = dict(PRIORITIES)


def _luma(css_color):
    """rgb/rgba 문자열의 밝기(0~255). 못 읽으면 None."""
    try:
        nums = re.findall(r'[\d.]+', css_color or '')
        if len(nums) < 3:
            return None
        r, g, b = (float(nums[0]), float(nums[1]), float(nums[2]))
        return 0.299 * r + 0.587 * g + 0.114 * b
    except Exception:
        return None


def _color_basis(p):
    """색 지적의 기준을 한 줄로. '다크에서 캡처했는데 라이트 기준으로 본다' 를 막는다.

    ★SC-295 인계 문서의 QA 요청 ①: 첨부의 computed color 가 전부 다크값이었는데
      그 사실이 결과에 안 적혀 있어 색 지적의 기준이 어긋났다. 강제할 수는 없으니
      무엇을 보고 있었는지 기록한다."""
    bg = p.get('bg') or ''
    scheme = p.get('scheme') or ''
    prefers = p.get('prefers_dark')
    if not (bg or scheme or prefers is not None):
        return None
    lum = _luma(bg)
    if lum is not None:
        tone = u'다크 배경' if lum < 110 else u'라이트 배경'
    else:
        tone = u'배경 불명'
    bits = [tone]
    if bg:
        bits.append(bg)
    if scheme and scheme not in ('normal', 'auto'):
        bits.append(u'color-scheme: %s' % scheme)
    if prefers is not None:
        bits.append(u'브라우저 선호: %s' % (u'다크' if prefers else u'라이트'))
    return u' · '.join(bits)


# 배치를 읽는 데 쓰는 속성. '안쪽으로 옮겨 달라' 류의 지적은 이 값들로 판단한다.
LAYOUT_KEYS = ('display', 'position', 'flex-direction', 'justify-content', 'align-items',
               'gap', 'padding', 'margin', 'width', 'height', 'text-align',
               'grid-template-columns', 'float', 'top', 'left', 'right', 'bottom')


def _layout_bits(styles):
    """computedStyles 에서 배치 관련 속성만 앞으로 뽑는다.

    ★스타일 줄은 300자에서 자른다. 그런데 정작 필요한 padding·gap·정렬 값이 뒤쪽에 있어
      잘려 나갔다(실측: font-family 목록이 길어 그 뒤가 전부 사라진다)."""
    got = {}
    for decl in str(styles or '').split(';'):
        if ':' not in decl:
            continue
        k, v = decl.split(':', 1)
        k, v = k.strip(), v.strip()
        if k in LAYOUT_KEYS and v:
            got[k] = v
    # 원본 선언 순서가 아니라 LAYOUT_KEYS 순서로 낸다 - 줄이 매번 같은 모양이어야
    # 사람이 훑어 읽는다(display 다음에 여백·정렬, 크기는 뒤).
    return [u'%s: %s' % (k, got[k]) for k in LAYOUT_KEYS if k in got]


def _nums(boxes, key):
    out = []
    for b in boxes:
        try:
            out.append(round(float(b.get(key))))
        except Exception:
            return []
    return out


def _spread(vals):
    return (max(vals) - min(vals)) if vals else 0


def _geometry_lines(boxes):
    """묶어 잡은 요소들의 정렬·크기·간격을 숫자로 읽어 준다.

    ★"이 카드 3개 정렬이 안 맞음" 을 그림 없이 전달하려면 숫자여야 한다.
      스크린샷을 붙이지 않는 것이 이 도구의 존재 이유이므로, 텍스트가 그 일을 해야 한다."""
    boxes = [b for b in (boxes or []) if isinstance(b, dict)]
    if len(boxes) < 2:
        return []
    lines = [u'- 묶음: 요소 %d개' % len(boxes)]

    lefts = _nums(boxes, 'x')
    tops = _nums(boxes, 'y')
    widths = _nums(boxes, 'width')
    heights = _nums(boxes, 'height')
    rights = [l + w for l, w in zip(lefts, widths)] if lefts and widths else []

    def axis(label, vals):
        if not vals:
            return None
        d = _spread(vals)
        if d <= 1:
            return u'%s %d 일치' % (label, vals[0])
        return u'%s %s (최대 %dpx 차이)' % (label, u'/'.join(str(v) for v in vals), d)

    align = [x for x in (axis(u'좌', lefts), axis(u'우', rights)) if x]
    if align:
        lines.append(u'- 정렬: %s' % u' · '.join(align))
    size = [x for x in (axis(u'폭', widths), axis(u'높이', heights)) if x]
    if size:
        lines.append(u'- 크기: %s' % u' · '.join(size))

    # 세로로 늘어놓은 경우의 간격(y 순서로 정렬해 인접 간격을 낸다)
    if len(boxes) >= 3 and tops and heights:
        order = sorted(range(len(boxes)), key=lambda i: tops[i])
        gaps = []
        for a, b in zip(order, order[1:]):
            gaps.append(round(tops[b] - (tops[a] + heights[a])))
        if gaps and max(gaps) >= 0:
            d = _spread(gaps)
            tail = u' — %s' % (u'고르다' if d <= 1 else u'최대 %dpx 차이' % d)
            lines.append(u'- 세로 간격: %spx%s' % (u'/'.join(str(g) for g in gaps), tail))
    return lines


def _hhmmss(ms):
    """agentation 의 timestamp(밀리초)를 시:분:초로. 이 값은 지금까지 버려지고 있었다."""
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0).strftime('%H:%M:%S')
    except Exception:
        return u''


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
        # ★프로그램이 소유하는 메타. 주석 객체 '안'에 넣지 않는다 -
        #   apply() 는 브라우저가 보낸 주석 dict 를 id 로 통째 덮어쓰므로,
        #   사용자가 브라우저에서 메모를 한 번 고치면 우리가 넣은 필드가 날아간다.
        # aid -> {'note': str, 'refs': [aid…], 'expected': str, 'priority': ''|high|mid|low}
        self.meta = {}
        self.closing = u''              # 총평(추출·복사 때 사람이 적는 마지막 코멘트)
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
                'bg': '', 'scheme': '', 'prefers_dark': None,
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
                                 ('referrer', 'referrer'), ('inIframe', 'in_iframe'),
                                 ('bg', 'bg'), ('scheme', 'scheme'),
                                 ('prefersDark', 'prefers_dark')):
                v = info.get(k_src)
                if v is not None and v != '':
                    p[k_dst] = v
                elif v is False:            # prefers_dark=False 는 '라이트' 라는 정보다
                    p[k_dst] = False
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
                    # 자기 메타는 버린다. 이 주석을 가리키던 연결은 남겨 두고
                    # 렌더에서 '(삭제된 주석)' 으로 보여 준다 - 조용히 사라지지 않게.
                    self.meta.pop(a.get('id'), None)
            elif kind == 'clear':
                for pp in same:
                    for aid in list(pp['annotations'].keys()):
                        self.meta.pop(aid, None)
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

    def tree_rows(self):
        """목록용 행. 화면 한 줄 + 그 밑에 붙는 주석 줄들.

        화면 번호는 render() 와 같은 순서로 붙는다(_render_pages 와 같은 필터) -
        목록의 [화면 2] 와 결과 문서의 [화면 2] 가 어긋나면 연결을 읽을 수 없다."""
        out = []
        with self.lock:
            # ★render() 와 같은 목록·같은 순서를 쓴다. 따로 걸러 세면 목록의 [화면 2] 와
            #   결과 문서의 [화면 2] 가 어긋나 연결을 못 읽는다.
            for i, p in enumerate(self._render_pages(), 1):
                anns = []
                for j, (aid, a) in enumerate(p['annotations'].items(), 1):
                    m = self.meta.get(aid) or {}
                    anns.append({
                        'aid': aid, 'no': j,
                        'element': a.get('element') or u'?',
                        'comment': a.get('comment') or u'',
                        'note': m.get('note') or u'',
                        'refs': len(m.get('refs') or []),
                        'expected': m.get('expected') or u'',
                        'priority': m.get('priority') or u'',
                    })
                out.append({
                    'no': i, 'title': p['title'] or u'(제목 없음)',
                    'vp': p['viewport'], 'url': p['url'],
                    'ann': len(p['annotations']), 'con': len(p['console']),
                    'net': len(p['network']), 'anns': anns,
                })
        return out

    def all_annotations(self):
        """연결 대상 고르기용. 화면·주석 번호가 붙은 전체 목록(렌더와 같은 순서)."""
        out = []
        with self.lock:
            for i, p in enumerate(self._render_pages(), 1):
                for j, (aid, a) in enumerate(p['annotations'].items(), 1):
                    out.append({
                        'aid': aid, 'screen': i, 'no': j,
                        'title': p['title'] or u'(제목 없음)', 'vp': p['viewport'],
                        'element': a.get('element') or u'?',
                        'comment': a.get('comment') or u'',
                    })
        return out

    def get_meta(self, aid):
        with self.lock:
            m = self.meta.get(aid) or {}
            return {'note': m.get('note') or u'', 'refs': list(m.get('refs') or []),
                    'expected': m.get('expected') or u'',
                    'priority': m.get('priority') or u''}

    def set_meta(self, aid, note=u'', refs=None, expected=u'', priority=u''):
        """보충 메모·연결·기대·우선순위를 저장한다. 전부 비면 항목 자체를 지운다.

        ★'기대' 와 '우선순위' 는 SC-295 인계 문서의 QA 요청 ③ 이다 - 받는 쪽이
          '지금 어떻고 어떻게 되어야 하나' 를 되묻지 않게 한다. 주석의 메모가 '현재',
          여기 적는 것이 '기대' 다."""
        refs = [r for r in (refs or []) if r and r != aid]
        seen, uniq = set(), []
        for r in refs:                              # 중복 제거(순서 유지)
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        if priority not in PRIORITY_LABEL:
            priority = u''
        with self.lock:
            if note or uniq or expected or priority:
                self.meta[aid] = {'note': note or u'', 'refs': uniq,
                                  'expected': expected or u'', 'priority': priority}
            else:
                self.meta.pop(aid, None)
            if not self._replaying:
                self._append_jsonl({'t': 'meta', 'aid': aid, 'note': note or u'',
                                    'refs': uniq, 'expected': expected or u'',
                                    'priority': priority})
                self.write_md()

    def set_closing(self, text):
        with self.lock:
            self.closing = text or u''
            if not self._replaying:
                self._append_jsonl({'t': 'closing', 'text': self.closing})
                self.write_md()

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

    def _render_pages(self):
        """결과에 실리는 화면 목록. 화면 번호는 이 순서로 붙는다(목록과 렌더가 같아야 한다)."""
        return [p for p in self.pages.values()
                if p['annotations'] or p['console'] or p['network']]

    def _ref_index(self, pages):
        """aid -> (화면번호, 주석번호, element, comment). 연결을 사람이 읽는 좌표로 옮긴다.

        ★연결은 aid 로 저장하고 번호는 이 시점에 해석한다. 번호를 저장해 두면
          화면 순서가 바뀌는 순간(되돌아온 화면·해상도 변경) 참조가 어긋난다."""
        idx = {}
        for i, p in enumerate(pages, 1):
            for j, (aid, a) in enumerate(p['annotations'].items(), 1):
                idx[aid] = (i, j, a.get('element') or u'?', a.get('comment') or u'')
        return idx

    def _render_index(self, pages):
        """항목 목차. 한 줄에 하나 - 이슈 댓글에 그대로 붙일 수 있게.

        ★SC-295 인계 문서의 QA 요청 ④: 댓글이 "첨부하였습니다" 한 줄이라 이슈 검색·추적에
          안 걸렸다. 문서 맨 앞에 제목 줄이 있으면 그걸 그대로 붙이면 된다."""
        rows = []
        n = 0
        for i, p in enumerate(pages, 1):
            for j, (aid, a) in enumerate(p['annotations'].items(), 1):
                n += 1
                m = self.meta.get(aid) or {}
                pr = PRIORITY_LABEL.get(m.get('priority') or '', u'')
                memo = (a.get('comment') or u'').replace(u'\n', u' ').strip()
                if len(memo) > 60:
                    memo = memo[:60] + u'…'
                rows.append(u'%d. [화면 %d] %s%s — %s%s'
                            % (n, i, a.get('element') or u'?',
                               u' %d번' % j if len(p['annotations']) > 1 else u'',
                               memo or u'(메모 없음)',
                               u'  *(%s)*' % pr if pr else u''))
        if not rows:
            return []
        return [u'## 항목 %d건' % n, u''] + rows + [u'']

    def _back_refs(self):
        """연결의 반대 방향. 어느 쪽 주석을 읽어도 관계가 보이려면 필요하다."""
        back = {}
        for src, m in self.meta.items():
            for dst in (m.get('refs') or []):
                back.setdefault(dst, []).append(src)
        return back

    def render(self):
        with self.lock:
            pages = self._render_pages()
            total = sum(len(p['annotations']) for p in pages)
            lines = [u'# 화면 주석 - %s' % _now(), u'',
                     u'- 화면 %d개 · 주석 %d건' % (len(pages), total), u'']
            if self.closing:
                # 받는 사람이 먼저 읽는 자리다(요약 바로 밑).
                lines += [u'## 총평', u'']
                lines += self.closing.strip().splitlines()
                lines.append(u'')
            lines += self._render_index(pages)
            if not pages:
                lines += [u'---', u'', u'(아직 주석이 없습니다)', u'']
                return u'\n'.join(lines)
            refidx = self._ref_index(pages)
            backidx = self._back_refs()
            for i, p in enumerate(pages, 1):
                lines += [u'---', u'',
                          u'# [화면 %d] %s  (%s)' % (i, p['title'] or u'(제목 없음)',
                                                    p['viewport'] or u'해상도 미상'), u'',
                          u'- 주소: %s' % p['url'],
                          u'- 해상도: %s%s' % (p['viewport'] or u'미상',
                                            u' · DPR %s' % p['dpr'] if p['dpr'] else u''),
                          u'- 시각: %s ~ %s' % (p['first_seen'], p['last_seen'])]
                basis = _color_basis(p)
                if basis:
                    # 색 지적을 받는 쪽이 어떤 테마의 값을 보고 있는지 알아야 한다.
                    lines.append(u'- 색 기준: %s' % basis)
                if p['in_iframe']:
                    lines.append(u'- 이 화면은 iframe 안이었습니다')
                if p['referrer']:
                    lines.append(u'- 이전 화면: %s' % p['referrer'])
                lines += [u'- 주석 %d건' % len(p['annotations']), u'']
                lines += self._render_annotations(list(p['annotations'].items()),
                                                  refidx, backidx)
                lines += self._render_noise(p)
            return u'\n'.join(lines)

    @staticmethod
    def _ref_label(refidx, aid):
        hit = refidx.get(aid)
        if not hit:
            return u'(삭제된 주석)'
        i, j, element, comment = hit
        tail = u' — "%s"' % comment[:40] if comment else u''
        return u'[화면 %d] %d번 %s%s' % (i, j, element, tail)

    def _render_annotations(self, items, refidx, backidx):
        lines = []
        for i, (aid, a) in enumerate(items, 1):
            m = self.meta.get(aid) or {}
            lines += [u'## %d. %s' % (i, a.get('element') or u'?'), u'',
                      u'> %s' % (a.get('comment') or u'(메모 없음)'), u'']
            pr = PRIORITY_LABEL.get(m.get('priority') or '', u'')
            if pr:
                lines.append(u'- 우선순위: %s' % pr)
            if m.get('expected'):
                # 메모가 '현재', 이것이 '기대' 다. 받는 쪽이 되묻지 않게 나눈다.
                exp = m['expected'].strip().splitlines()
                lines.append(u'- 기대: %s' % exp[0])
                for extra in exp[1:]:
                    lines.append(u'  %s' % extra)
            ts = _hhmmss(a.get('timestamp'))
            if ts:
                # ★이어지는 액션의 순서는 여기서만 읽을 수 있다(화면 키에는 시간이 없다).
                lines.append(u'- 시각: %s' % ts)
            if m.get('note'):
                note = m['note'].strip().splitlines()
                lines.append(u'- 보충: %s' % note[0])
                for extra in note[1:]:              # 두 칸 들여쓰기로 같은 항목을 잇는다
                    lines.append(u'  %s' % extra)
            for dst in (m.get('refs') or []):
                lines.append(u'- 관련: → %s' % self._ref_label(refidx, dst))
            for src in backidx.get(aid, []):
                lines.append(u'- 관련: ← %s' % self._ref_label(refidx, src))
            lines += [u'- 경로: `%s`' % (a.get('elementPath') or u''),
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
            lines += _geometry_lines(a.get('elementBoundingBoxes'))
            if a.get('nearbyElements'):
                lines.append(u'- 주변 요소: %s' % str(a['nearbyElements'])[:200])
            lay = _layout_bits(a.get('computedStyles'))
            if lay:
                # ★자르기에 걸려 사라지던 값들이다. 스타일 줄보다 먼저 세운다.
                lines.append(u'- 배치: %s' % u' · '.join(lay))
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
                        elif t == 'meta':
                            self.set_meta(rec.get('aid'), rec.get('note') or u'',
                                          rec.get('refs') or [],
                                          rec.get('expected') or u'',
                                          rec.get('priority') or u'')
                        elif t == 'closing':
                            self.set_closing(rec.get('text') or u'')
                        # 모르는 t 는 무시한다 - 옛 파일과 앞으로의 확장 양쪽을 위해.
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
            self.meta.clear()
            self.closing = u''
            self.write_md()
