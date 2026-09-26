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
  qa.sqlite         들어온 이벤트 원본(회차별). 프로그램을 다시 켜면 현재 회차를 재생해 목록을 복원한다.
                    (1.1 까지는 annotations.jsonl 이었다 - 첫 실행 때 qadb.migrate_legacy 가 옮긴다)

회차(round, 1.2)
  [비우기]·[추출] 은 현재 회차를 닫고 새 회차를 연다. 지난 회차는 **읽기 전용**(RoundView) -
  현재 목록에 합치지 않는다. 합치면 총평·순서·메타가 옛 값으로 덮인다(실사용 불만의 원인).
"""
import io
import json
import os
import re
import shutil
import threading

import qadb
import version as VER
from collections import OrderedDict
from datetime import datetime

ReadOnlyRound = qadb.ReadOnlyRound

# 첨부 종류 - 확장자로만 가른다(동영상은 사람이 Win+Alt+R 로 찍은 파일이다. 도구는 녹화하지 않는다).
VIDEO_EXT = ('.mp4', '.webm', '.mov', '.mkv', '.avi')
IMAGE_EXT = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')


def attach_kind(fname):
    ext = os.path.splitext(fname or '')[1].lower()
    if ext in VIDEO_EXT:
        return 'video'
    if ext in IMAGE_EXT:
        return 'image'
    return 'file'


def human_size(n):
    try:
        n = float(n)
    except Exception:
        return u''
    for unit in (u'B', u'KB', u'MB', u'GB'):
        if n < 1024 or unit == u'GB':
            return (u'%d %s' % (n, unit)) if unit == u'B' else (u'%.1f %s' % (n, unit))
        n /= 1024.0
    return u''

NOISE_CAP = 50          # 화면당 콘솔 에러·실패 요청 보관 상한(그 이상은 오래된 것부터 버린다)
API_CAP = 20            # 화면당 '작은 응답' 보관 상한. 실패 기록과 별도 목록이어야 한다
                        #   - 한 목록에 담으면 성공 기록이 실패 기록을 밀어내 버린다

# 연결(meta.refs)의 의미: A → refs 의 C 는 A 의 '하위 주석'이 된다(실사용 요구 4·5).
# ★참(기본값): 하위가 다른 화면 것이어도 상위 밑에 1.1 로 들어간다(연결의 목적이
#   "원인이 다른 화면에 있다" 를 표현하는 것이므로 화면을 가리지 않는다).
#   거짓으로 두면 같은 화면끼리만 묶이고, 다른 화면 것은 지금처럼 '- 관련:' 줄로 남는다.
#   이 상수 하나만 바꾸면 되도록 _nested() 안 한 곳에서만 참조한다.
CROSS_SCREEN_CHILDREN = True


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
# ★z-index·visibility·opacity·overflow 는 '안 보여요' 류 지적의 답이라 함께 본다.
LAYOUT_KEYS = ('display', 'position', 'z-index', 'visibility', 'opacity', 'overflow',
               'flex-direction', 'justify-content', 'align-items',
               'gap', 'padding', 'margin', 'width', 'height',
               'border', 'border-radius', 'text-align',
               'grid-template-columns', 'float', 'top', 'left', 'right', 'bottom')

# 기본값이면 줄만 길어지고 읽히지 않는다. 기본과 다를 때만 낸다.
#   ★실측: 주석 6건 전부에 'flex-direction: row' 와 'text-align: start' 가 붙어 있었다.
#     둘 다 기본값이라 아무것도 말해 주지 않으면서 줄의 절반을 먹었다.
LAYOUT_DEFAULTS = {'position': 'static', 'z-index': 'auto', 'visibility': 'visible',
                   'opacity': '1', 'overflow': 'visible', 'float': 'none',
                   'flex-direction': 'row', 'justify-content': 'normal',
                   'align-items': 'normal', 'gap': 'normal', 'padding': '0px',
                   'margin': '0px', 'border-radius': '0px', 'text-align': 'start'}

# 색 기준. '그림을 보내 달라' 대신 글로 답하는 자리다(README).
COLOR_KEYS = ('color', 'background-color', 'border-color', 'outline-color')

# 글자. font-family 는 목록이 길어 맨 앞 하나만 남긴다.
TEXT_KEYS = ('font-size', 'font-weight', 'line-height', 'font-family')


def _style_map(styles):
    """computedStyles 문자열을 {속성: 값} 으로. 먼저 나온 선언을 남긴다."""
    got = {}
    for decl in str(styles or '').split(';'):
        if ':' not in decl:
            continue
        k, v = decl.split(':', 1)
        k, v = k.strip(), v.strip()
        if k and v:
            got.setdefault(k, v)
    return got


def _layout_bits(styles):
    """computedStyles 에서 배치 관련 속성만 앞으로 뽑는다.

    ★예전에는 스타일 줄을 300자에서 통째로 잘랐다. 그런데 정작 필요한 padding·gap·정렬
      값이 뒤쪽에 있어 잘려 나갔다(실측: font-family 목록이 길어 그 뒤가 전부 사라진다)."""
    got = _style_map(styles)
    # 원본 선언 순서가 아니라 LAYOUT_KEYS 순서로 낸다 - 줄이 매번 같은 모양이어야
    # 사람이 훑어 읽는다(display 다음에 여백·정렬, 크기는 뒤).
    out = []
    for k in LAYOUT_KEYS:
        v = got.get(k)
        if not v or v == LAYOUT_DEFAULTS.get(k):
            continue
        if k == 'border' and v.startswith('0px none'):
            continue                            # 테두리 없음. 색만 붙어 있어 기본값 비교가 안 된다
        out.append(u'%s: %s' % (k, v))
    return out


def _color_bits(styles):
    """색 기준. 지적이 색이면 받는 쪽은 이 값만 있으면 된다."""
    got = _style_map(styles)
    return [u'%s: %s' % (k, got[k]) for k in COLOR_KEYS if got.get(k)]


def _text_bits(styles):
    """글자. font-family 는 첫 글꼴만 - 목록 전체는 읽히지 않고 줄만 먹는다."""
    got = _style_map(styles)
    out = []
    for k in TEXT_KEYS:
        v = got.get(k)
        if not v:
            continue
        if k == 'font-family':
            v = v.split(',')[0].strip().strip('"').strip("'")
        out.append(u'%s: %s' % (k, v))
    return out


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


def _spot_url(url):
    """재지적 대조용 주소. 질의문자열만 뗀다.

    ★_page_id 와 달리 해시를 남긴다. SPA 에서는 해시가 화면 구분이라
      (`#!page-edit/537`), 떼면 서로 다른 화면이 한 자리로 합쳐져 오탐이 된다."""
    try:
        from urllib.parse import urlsplit
        u = urlsplit(url or '')
        frag = ('#' + u.fragment) if u.fragment else ''
        return '%s://%s%s%s' % (u.scheme, u.netloc, u.path, frag)
    except Exception:
        return (url or '').split('?')[0]


def _round_title(row):
    """회차 한 줄 표기: '3회차 · 2026-08-21 11:45'. 지난 회차 창·재지적 줄에 같은 문자열이 나간다."""
    if row is None:
        return u''
    when = (row['closed_at'] or row['started_at'] or u'')[:16]
    return u'%d회차 · %s' % (int(row['seq'] or 0), when)


class Store(object):
    """현재(열린) 회차. 지난 회차는 RoundView(아래) 로 읽기만 한다."""
    readonly = False

    def __init__(self, out_dir, db=None, round_id=None, label=None):
        self.out_dir = out_dir
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        self.md_path = os.path.join(out_dir, 'latest.md')
        self.lock = threading.RLock()
        self.db = db or qadb.DB(os.path.join(out_dir, 'qa.sqlite'))
        self.label = label or VER.label()
        if round_id is None:
            row = self.db.open_round(self.label)
            round_id = row['id']
        self.round_id = round_id
        self.pages = OrderedDict()      # url -> page dict
        # aid -> [attach_dir 안 파일 이름…]. 그림 본문은 jsonl 에 남기지 않는다
        # (base64 를 기록에 넣으면 파일이 순식간에 커진다) - 파일로만 둔다.
        self.attach = {}
        # ★프로그램이 소유하는 메타. 주석 객체 '안'에 넣지 않는다 -
        #   apply() 는 브라우저가 보낸 주석 dict 를 id 로 통째 덮어쓰므로,
        #   사용자가 브라우저에서 메모를 한 번 고치면 우리가 넣은 필드가 날아간다.
        # aid -> {'note': str, 'refs': [aid…], 'expected': str, 'priority': ''|high|mid|low}
        self.meta = {}
        self.closing = u''              # 총평(추출·복사 때 사람이 적는 마지막 코멘트)
        # 화면 단위 캡처(1.2). key -> {'files': [...], 'partial': bool, 'reason': str,
        #   'pins': {aid: label}, 'at': '시각', 'css_w', 'css_h', 'dpr'}
        #   ★주석마다 찍지 않는다 - 화면(주소·해상도)당 1벌이다(CLAUDE.md 스크린샷 절).
        self.captures = {}
        # 사람이 정한 주석 순서. key -> [aid…] (없으면 들어온 순서)
        self.order = {}
        # 하위 주석끼리의 순서. 부모 aid -> [자식 aid…] (없으면 연결한 순서)
        #   ★화면(key) 이 아니라 부모 aid 로 둔다 - 하위는 다른 화면 것일 수 있어서
        #     화면 하나의 순서표로는 표현할 수 없다.
        self.child_order = {}
        self._replaying = False
        self._spots = None      # 지난 회차 자리 색인. 첫 렌더 때 한 번만 읽는다.
        self._past_cache = {}   # round_id -> [(spot_url, path)] (닫힌 회차는 바뀌지 않는다)
        # 해상도를 알기 전('?') 키에서 실제 키로 옮겨 간 자취.
        #   ★응답 본문은 요청이 끝난 뒤 작업 큐를 거쳐 늦게 도착한다. 그 사이에
        #     merge_unknown 이 화면 키를 바꾸면, 늦게 온 기록이 은퇴한 키를 들고 와
        #     조용히 버려지고 '해상도 미상' 유령 화면까지 생긴다(실측: 400 본문이
        #     붙는 판과 안 붙는 판이 갈렸다 - 경합이었다).
        self.moved = {}

    # ── 회차 ──────────────────────────────────────────────────
    @property
    def attach_dir(self):
        """이 회차의 첨부 폴더. 회차마다 따로 둔다 - 비우기·추출 때 파일을 옮길 일이 없다."""
        return os.path.join(self.out_dir, 'attach', 'r%d' % self.round_id)

    @property
    def capture_dir(self):
        return os.path.join(self.out_dir, 'capture', 'r%d' % self.round_id)

    def current_round(self):
        return self.db.round(self.round_id)

    def round_seq(self):
        row = self.current_round()
        return int(row['seq'] or 0) if row else 0

    def round_title(self):
        return _round_title(self.current_round())

    def list_rounds(self):
        """지난 회차 목록(닫힌 것만, 오래된 것부터)."""
        with self.lock:
            return [dict(r) for r in self.db.list_rounds('closed')]

    def versions_of(self, aid):
        """이 주석의 변경 이력(현재 회차). 보강 창 [변경 이력] 이 읽는다."""
        with self.lock:
            return self.db.versions_of(self.round_id, aid)

    def _writing(self):
        """★닫힌 회차에는 쓰지 않는다 - 조용히 무시하지 않고 예외를 던진다."""
        if self.readonly and not self._replaying:
            raise ReadOnlyRound(u'지난 회차(%s)는 읽기 전용입니다.' % self.round_title())

    def _close_and_reopen(self, reason, export_path=''):
        """현재 회차를 닫고 새 회차를 연다(비우기·추출). 파일은 옮기지 않는다 - 회차 폴더가 다르다."""
        self._writing()
        pages, total = self.counts()
        self.db.close_round(self.round_id, reason=reason, export_path=export_path,
                            n_pages=pages, n_anns=total)
        old = self.round_id
        row = self.db.open_round(self.label)
        self.round_id = row['id']
        self._past_cache.pop(old, None)
        self._spots = None                      # 방금 닫힌 회차가 '지난 회차' 가 됐다
        return old

    # ── 화면 ──────────────────────────────────────────────────
    @staticmethod
    def key_of(url, viewport):
        """화면 키 = (주소, 해상도). 해상도를 모르면 '?' 로 둔다(합치지는 않는다)."""
        return (url or '', viewport or '?')

    def _ordered(self, key, anns):
        """사람이 정한 순서가 있으면 그 순서로. 없거나 빠진 것은 뒤에 원래 순서로 붙인다."""
        want = self.order.get(key)
        if not want:
            return list(anns.items())
        out, seen = [], set()
        for aid in want:
            if aid in anns:
                out.append((aid, anns[aid]))
                seen.add(aid)
        for aid, a in anns.items():
            if aid not in seen:
                out.append((aid, a))
        return out

    def _nested(self, pages):
        """meta.refs 를 '하위 주석' 으로 엮는다(실사용 요구 4·5 - 연결 = 하위).

        tree_rows·all_annotations·_render_index·render(→_render_annotations) 다섯
        곳이 전부 같은 트리를 봐야 목록의 번호(1.1)와 결과 문서의 번호가 어긋나지
        않는다 - README 의 "번호는 렌더 시점에 해석한다" 원칙과 같은 이유다.
        그래서 이 계산은 여기 한 곳에서만 한다.

        반환: (roots_by_page, index, loose)
          roots_by_page[i]  pages[i] 의 최상위 주석 노드 목록(그 화면 순서)
          index             aid -> 노드
          loose             부모 aid -> [하위가 되지 못한 aid…]
                            (이미 다른 부모가 있음 · 순환이 됨 · 지워진 대상)
                            → 렌더에서 '- 하위: →' 로, 반대쪽은 '- 상위: ←' 로 남는다.

        노드 = {aid, ann, key(원 화면 키), home(원 화면 번호, 1-based),
                screen(지금 표시되는 화면 번호), label('1.2'), depth,
                parent(aid|None), children([노드…]), foreign(원 화면과 다른 화면
                밑에 표시되는가)}."""
        raw, home, ann_of = {}, {}, {}
        for i, p in enumerate(pages, 1):
            key = self.key_of(p['url'], p['viewport'])
            ids = [aid for aid, _ in self._ordered(key, p['annotations'])]
            raw[key] = ids
            for aid in ids:
                home[aid] = (i, key)
                ann_of[aid] = p['annotations'][aid]

        parent, loose = {}, {}

        def chain_has(start, target):
            # start 의 부모 사슬을 타고 올라가며 target 을 만나는가 - A 를 C 의
            # 자식으로 붙이려 할 때(parent[C]=A), C 가 이미 A 의 조상이면 순환이다.
            cur, seen = parent.get(start), set()
            while cur is not None and cur not in seen:
                if cur == target:
                    return True
                seen.add(cur)
                cur = parent.get(cur)
            return False

        for i, p in enumerate(pages, 1):
            key = self.key_of(p['url'], p['viewport'])
            for a_aid in raw[key]:
                refs = (self.meta.get(a_aid) or {}).get('refs') or []
                for c_aid in refs:
                    ok = (c_aid in home and c_aid != a_aid and c_aid not in parent
                          and not chain_has(a_aid, c_aid)
                          and (CROSS_SCREEN_CHILDREN or home[c_aid][1] == key))
                    if ok:
                        parent[c_aid] = a_aid
                    else:
                        loose.setdefault(a_aid, []).append(c_aid)

        def children_of(a_aid):
            kids = [c for c, par in parent.items() if par == a_aid]
            refs = (self.meta.get(a_aid) or {}).get('refs') or []
            refs_pos = {c: n for n, c in enumerate(refs)}
            want = self.child_order.get(a_aid) or []
            want_pos = {c: n for n, c in enumerate(want)}
            # 사람이 드래그로 정한 순서(child_order)가 있으면 그것부터, 없거나
            # 새로 연결된 것은 연결한 순서(refs) 뒤에 붙인다.
            kids.sort(key=lambda c: (want_pos.get(c, len(want) + refs_pos.get(c, 0)),))
            return kids

        index = {}

        def build(a_aid, label, depth, screen):
            home_i, home_key = home[a_aid]
            node = {'aid': a_aid, 'ann': ann_of[a_aid], 'key': home_key,
                    'home': home_i, 'screen': screen, 'label': label,
                    'depth': depth, 'parent': parent.get(a_aid), 'children': [],
                    'foreign': home_i != screen}
            index[a_aid] = node
            for j, c_aid in enumerate(children_of(a_aid), 1):
                node['children'].append(
                    build(c_aid, u'%s.%d' % (label, j), depth + 1, screen))
            return node

        roots_by_page = []
        for i, p in enumerate(pages, 1):
            key = self.key_of(p['url'], p['viewport'])
            roots, n = [], 0
            for a_aid in raw[key]:
                if a_aid in parent:
                    continue
                n += 1
                roots.append(build(a_aid, str(n), 0, i))
            roots_by_page.append(roots)
        return roots_by_page, index, loose

    @staticmethod
    def _flatten(nodes):
        """트리를 전위 순회로 편다(부모가 자식보다 먼저 나온다 - 트리뷰 삽입 순서)."""
        out = []
        for node in nodes:
            out.append(node)
            out.extend(Store._flatten(node['children']))
        return out

    def move_page(self, key, delta):
        """화면(그룹) 자체의 순서를 사람이 정한다(프4, Ctrl+↑/↓).

        ★화면 번호는 이 순서로 붙고, 연결(`- 관련:`)은 렌더 시점에 번호를 해석하므로
          순서를 바꿔도 참조가 어긋나지 않는다. 화면 정체성 (주소, 해상도) 은 건드리지 않는다."""
        with self.lock:
            keys = list(self.pages.keys())
            if key not in keys:
                return None
            i = keys.index(key)
            j = max(0, min(len(keys) - 1, i + delta))
            if i == j:
                return keys
            keys.insert(j, keys.pop(i))
        return self.reorder_pages(keys)

    def reorder_pages(self, keys):
        """목록에서 화면 줄을 드래그해 옮긴 뒤의 최종 순서를 저장한다(브1).

        ★move_page 와 저장 경로를 하나로 둔다 - 따로 두면 한쪽만 고쳤을 때
          Ctrl+↑/↓ 와 드래그의 결과가 갈린다. 주어진 목록이 지금 화면 전체와
          정확히 같을 때만 반영한다(화면이 느는·주는 동안의 드래그를 막는다)."""
        self._writing()
        with self.lock:
            keys = [tuple(k) for k in keys]
            if set(keys) != set(self.pages.keys()) or len(keys) != len(self.pages):
                return None
            self.pages = OrderedDict((k, self.pages[k]) for k in keys)
            if not self._replaying:
                self._append_event({'t': 'porder',
                                    'keys': [[k[0], k[1]] for k in keys]})
                self.write_md()
            return keys

    def set_page_order(self, keys):
        """기록에서 화면 순서를 되살린다. 모르는 화면은 무시하고, 빠진 화면은 뒤에 붙인다."""
        with self.lock:
            want = [tuple(k) for k in (keys or []) if isinstance(k, (list, tuple)) and len(k) == 2]
            rest = [k for k in self.pages.keys() if k not in want]
            ordered = [k for k in want if k in self.pages] + rest
            self.pages = OrderedDict((k, self.pages[k]) for k in ordered)

    def set_order(self, key, ids):
        """화면 안 주석(최상위) 순서를 사람이 정한다(프4)."""
        self._writing()
        with self.lock:
            self.order[key] = list(ids)
            if not self._replaying:
                self._append_event({'t': 'order', 'url': key[0], 'viewport': key[1],
                                    'ids': list(ids)})
                self.write_md()

    def set_child_order(self, parent_aid, ids):
        """한 상위 주석 밑, 하위 주석끼리의 순서를 사람이 정한다(브5·6).

        ★화면(key) 이 아니라 부모 aid 로 저장한다 - 하위는 다른 화면 것일 수 있다."""
        self._writing()
        with self.lock:
            self.child_order[parent_aid] = list(ids)
            if not self._replaying:
                self._append_event({'t': 'corder', 'aid': parent_aid, 'ids': list(ids)})
                self.write_md()

    def _page(self, key):
        p = self.pages.get(key)
        if p is None:
            p = {
                'url': key[0], 'viewport': key[1], 'title': '', 'dpr': None,
                'referrer': '', 'in_iframe': False,
                'bg': '', 'scheme': '', 'prefers_dark': None,
                'first_seen': _now(), 'last_seen': _now(),
                'annotations': OrderedDict(),   # id -> annotation
                'console': [], 'network': [], 'api': [],
                'layout': None,   # 레이아웃 모드에서 옮긴 것(브3) - set_layout() 이 채운다
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

    def _find_home(self, aid):
        """이 주석 id 가 지금 어느 화면에 살고 있는지 찾는다(전역 유일 id 전제).

        apply() 의 add/update/submit 이 엉뚱한 화면에 사본을 만들지 않게 먼저 확인한다."""
        for key, pp in self.pages.items():
            if aid in pp['annotations']:
                return (key, pp)
        return None

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
            # ★이걸 빼면 안 된다. 화면 진입 직후의 조회는 해상도를 알기 전에 도착하므로
            #   여기서 옮기지 않으면 '목록이 비었다' 의 근거가 통째로 사라진다.
            new['api'] = ((old.get('api') or []) + new['api'])[-API_CAP:]
            for aid, a in old['annotations'].items():
                new['annotations'].setdefault(aid, a)
            for k in ('title', 'dpr', 'referrer'):
                if not new.get(k) and old.get(k):
                    new[k] = old[k]
            if not new.get('layout') and old.get('layout'):
                new['layout'] = old['layout']
            new['first_seen'] = min(new['first_seen'], old['first_seen'])
            self.pages.pop(src, None)
            self.moved[src] = dst
            # ★병합 사실을 기록에 남긴다. 안 남기면 재시작 복원(replay) 때
            #   '해상도 미상' 화면이 한 줄 더 살아나 라이브와 결과가 달라진다(실측).
            if not self._replaying:
                self._append_event({'t': 'merge', 'url': url, 'viewport': viewport})

    def set_layout(self, payload):
        """레이아웃 모드(요소 이동·배치 상자)에서 옮긴 것을 화면에 붙인다(브3).

        ★agentation 의 저장 상태를 그대로 치환한다(누적하지 않는다) - inject.jsx 가
          보낼 때마다 그 시점의 전체 diff 를 계산해 보내므로, 여기서 합치면 이미
          되돌린 변경까지 남는다. 손대지 않은 화면(layout 이 None) 은 애초에 오지
          않는다(inject.jsx 의 lastLayoutSig 초기값이 'null' 이라서)."""
        self._writing()
        with self.lock:
            key = self.touch_page(payload)
            if key is None:
                return
            p = self.pages[key]
            lay = payload.get('layout') or None
            if lay is None and not p.get('layout'):
                return                      # 원래도 없었다 - 기록을 늘리지 않는다
            p['layout'] = lay
            if not self._replaying:
                self._append_event({'t': 'layout', 'url': key[0], 'viewport': key[1],
                                    'layout': lay})
                self.write_md()

    # ── 이벤트 적용 ────────────────────────────────────────────
    def apply(self, payload, persist=True):
        """주입 스크립트가 보낸 payload 하나를 반영한다. (화면 수, 주석 수) 반환."""
        self._writing()
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
                    aid = a.get('id')
                    # ★id 는 전역 유일하다. same(같은 경로) 만 지우면, 해상도가 바뀐
                    #   사이에 update 로 다른 화면에 새로 생긴 사본(아래 참고)이 남는다.
                    for pp in self.pages.values():
                        pp['annotations'].pop(aid, None)
                    # 자기 메타는 버린다. 이 주석을 가리키던 연결은 남겨 두고
                    # 렌더에서 '(삭제된 주석)' 으로 보여 준다 - 조용히 사라지지 않게.
                    self.meta.pop(aid, None)
            elif kind == 'clear':
                for pp in same:
                    for aid in list(pp['annotations'].keys()):
                        self.meta.pop(aid, None)
                    pp['annotations'].clear()
            else:                                   # add · update · submit · copy
                # ★agentation 은 주석을 pathname 단위로 저장해 해시·쿼리·해상도가
                #   달라도 같은 핀을 보여준다. 그 화면에서 메모를 고치면 update 가
                #   다른 (url,viewport) 키로 도착해, 여기서 그냥 삽입하면 같은 id 가
                #   두 화면에 남는다(실사용 보고: "주석이 2번씩 달린다"). id 가 이미
                #   사는 화면을 찾아 그 자리에서 갱신한다 - 새 화면에 또 넣지 않는다.
                for a in anns:
                    aid = a.get('id') or str(len(p['annotations']) + 1)
                    home = self._find_home(aid)
                    if home is not None and home[0] != key:
                        home[1]['annotations'][aid] = a
                    else:
                        p['annotations'][aid] = a
            if persist and not self._replaying:
                self._append_event({'t': 'annotation', 'payload': payload})
                self.write_md()
            return self.counts()

    def add_console(self, key, text, level='error'):
        with self.lock:
            p = self._page(key)
            p['console'].append({'ts': _now(), 'level': level, 'text': text})
            del p['console'][:-NOISE_CAP]
            if not self._replaying:
                self._append_event({'t': 'console', 'url': p['url'],
                                    'viewport': p['viewport'], 'level': level, 'text': text})

    def add_network(self, key, request_url, status, reason='', token=''):
        with self.lock:
            p = self._page(key)
            p['network'].append({'ts': _now(), 'request': request_url,
                                 'status': status, 'reason': reason,
                                 'token': token, 'body': ''})
            del p['network'][:-NOISE_CAP]
            if not self._replaying:
                self._append_event({'t': 'network', 'url': p['url'], 'viewport': p['viewport'],
                                    'request': request_url, 'status': status,
                                    'reason': reason, 'token': token})

    def set_network_body(self, key, token, body):
        """실패 요청 기록에 응답 본문을 붙인다(token = CDP requestId).

        ★상태코드만으로는 받는 쪽이 원인을 못 읽는다. 서버가 400 에 적어 보낸 사유
          ("이미 콘텐츠가 배치된 영역입니다") 가 본문에 있고, 그게 없으면 되묻게 된다.
        ★목록 위치가 아니라 token 으로 찾는다. NOISE_CAP 으로 앞이 잘리거나 되돌리기로
          줄이 빠져도 엉뚱한 기록에 붙지 않아야 한다."""
        if not token or not body:
            return
        with self.lock:
            p = self._page(self._live_key(key))
            for rec in reversed(p['network']):
                if rec.get('token') == token:
                    if rec.get('body'):
                        return                  # 이미 붙었다(재생 중 중복 호출)
                    rec['body'] = body
                    break
            else:
                return
            if not self._replaying:
                self._append_event({'t': 'netbody', 'url': p['url'],
                                    'viewport': p['viewport'], 'token': token, 'body': body})

    def _live_key(self, key):
        """늦게 도착한 기록의 화면 키를 지금 살아 있는 키로 옮긴다(merge_unknown 자취)."""
        seen = 0
        while key in self.moved and seen < 5:       # 해상도가 두 번 바뀐 경우까지
            key = self.moved[key]
            seen += 1
        return key

    def add_api(self, key, request_url, status, body):
        """성공했지만 응답이 작은 조회를 남긴다.

        ★'목록이 비었다' 류 지적의 근거다. 실패가 아니라 200 이라 실패 목록에는 안 잡히고,
          그래서 지금까지 "조회가 0건을 냈는지, 화면이 걸러낸 건지" 를 가릴 수 없었다.
        ★앱이 '비었다' 를 판정하지 않는다. 작은 응답을 그대로 보여 주고 읽는 쪽이 판단한다
          - 무엇을 비었다고 볼지(빈 배열·total 0·페이지네이션)는 화면마다 다르다."""
        with self.lock:
            p = self._page(self._live_key(key))
            for rec in p['api']:                # 같은 조회를 여러 번 하면 줄만 늘어난다
                if rec['request'] == request_url and rec['status'] == status:
                    rec['ts'], rec['body'] = _now(), body
                    rec['hits'] = rec.get('hits', 1) + 1
                    break
            else:
                p['api'].append({'ts': _now(), 'request': request_url,
                                 'status': status, 'body': body, 'hits': 1})
                del p['api'][:-API_CAP]
            if not self._replaying:
                self._append_event({'t': 'api', 'url': p['url'], 'viewport': p['viewport'],
                                    'request': request_url, 'status': status, 'body': body})

    def counts(self):
        pages = [p for p in self.pages.values() if p['annotations']]
        return len(pages), sum(len(p['annotations']) for p in pages)

    def _past_spots(self):
        """지난 회차들이 어느 자리를 지적했는지. {(주소, 경로): [회차 표기...]}

        ★같은 자리가 회차를 넘겨 다시 올라오면 받는 쪽이 가장 먼저 알아야 하는 사실이다.
          '고쳤다더니 또 안 된다' 가 여기서 갈린다 - 지난 회차에 고쳤다고 회신한 자리가
          다시 지적되면 원인이 그때와 다르다는 뜻이므로, 회신 문안 자체가 달라져야 한다.
        ★닫힌 회차의 annotation_versions 에서 '마지막 버전' 만 본다(지운 것은 빠진다).
          한 번만 읽고 들고 있는다(렌더는 주석마다 불린다). 회차가 닫힐 때 무효화한다."""
        if self._spots is not None:
            return self._spots
        spots = {}
        for row in self.db.list_rounds('closed'):
            rid = row['id']
            if rid == self.round_id:
                continue                        # RoundView 자신은 '지난 회차' 가 아니다
            label = _round_title(row)
            got = self._past_cache.get(rid)
            if got is None:
                got = [(_spot_url(u), path) for u, path in self.db.latest_spots(rid)]
                self._past_cache[rid] = got
            for spot in got:
                seen = spots.setdefault(spot, [])
                if label not in seen:
                    seen.append(label)
        self._spots = spots
        return spots

    def tree_rows(self):
        """목록용 행. 화면 한 줄 + 그 밑에 붙는 주석 줄들(하위 주석은 더 깊이).

        화면 번호는 render() 와 같은 순서로 붙는다(_render_pages 와 같은 필터) -
        목록의 [화면 2] 와 결과 문서의 [화면 2] 가 어긋나면 연결을 읽을 수 없다."""
        out = []
        with self.lock:
            pages = self._render_pages()
            roots_by_page, _index, loose = self._nested(pages)
            for i, p in enumerate(pages, 1):
                anns = []
                for node in self._flatten(roots_by_page[i - 1]):
                    aid, a = node['aid'], node['ann']
                    m = self.meta.get(aid) or {}
                    anns.append({
                        'aid': aid, 'no': node['label'], 'parent': node['parent'],
                        'depth': node['depth'], 'foreign': node['foreign'],
                        'home_screen': node['home'],
                        'element': a.get('element') or u'?',
                        'comment': a.get('comment') or u'',
                        'note': m.get('note') or u'',
                        'refs': len(node['children']) + len(loose.get(aid) or []),
                        'expected': m.get('expected') or u'',
                        'priority': m.get('priority') or u'',
                        'files': len(self.attach.get(aid) or []),
                    })
                cap = self.captures.get(self.key_of(p['url'], p['viewport'])) or {}
                out.append({
                    'no': i, 'title': p['title'] or u'(제목 없음)',
                    'vp': p['viewport'], 'url': p['url'],
                    'ann': len(p['annotations']), 'con': len(p['console']),
                    'net': len(p['network']), 'anns': anns,
                    'layout': self._layout_rows(p),
                    'cap': (cap.get('at') or u'')[11:16] if cap.get('files') else u'',
                })
        return out

    def all_annotations(self):
        """연결 대상 고르기용. 화면·주석 번호가 붙은 전체 목록(렌더와 같은 순서)."""
        out = []
        with self.lock:
            pages = self._render_pages()
            roots_by_page, index, _loose = self._nested(pages)
            for i, roots in enumerate(roots_by_page, 1):
                for node in self._flatten(roots):
                    aid, a = node['aid'], node['ann']
                    out.append({
                        'aid': aid, 'screen': i, 'no': node['label'],
                        'parent': node['parent'],
                        'title': (pages[i - 1]['title'] or u'(제목 없음)'),
                        'vp': pages[i - 1]['viewport'],
                        'url': pages[i - 1]['url'],
                        # ★하위(1.1)로 다른 화면 밑에 표시돼도 원 화면 키는 이것이다(잘라 붙이기가 쓴다)
                        'home_url': node['key'][0], 'home_vp': node['key'][1],
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
        self._writing()
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
                self._append_event({'t': 'meta', 'aid': aid, 'note': note or u'',
                                    'refs': uniq, 'expected': expected or u'',
                                    'priority': priority})
                self.write_md()

    def set_closing(self, text):
        self._writing()
        with self.lock:
            self.closing = text or u''
            if not self._replaying:
                self._append_event({'t': 'closing', 'text': self.closing})
                self.write_md()

    # ── 첨부 그림(브8) ────────────────────────────────────────
    # ★스크린샷은 도구가 찍지 않는다(README) - 사람이 메모창에 붙여넣거나(Ctrl+V·
    #   드래그) 보강 창에서 파일로 고른 것만 붙는다. jsonl 에는 파일 이름만 남기고
    #   그림 본문(base64)은 out/attach 에 파일로만 둔다 - 기록이 순식간에 커지는
    #   것을 막고, latest.md 도 계속 가벼운 텍스트로 남는다.
    def _ensure_attach_dir(self):
        if not os.path.isdir(self.attach_dir):
            os.makedirs(self.attach_dir)

    def _next_attach_name(self, aid, names, ext):
        n = len(names) + 1
        while True:
            fname = '%s-%d%s' % (aid, n, ext)
            if not os.path.exists(os.path.join(self.attach_dir, fname)):
                return fname
            n += 1

    def _set_attach(self, aid, names):
        self._writing()
        with self.lock:
            if names:
                self.attach[aid] = list(names)
            else:
                self.attach.pop(aid, None)
            if not self._replaying:
                self._append_event({'t': 'attach', 'aid': aid, 'files': list(names or [])})
                self.write_md()

    def add_image_bytes(self, aid, raw, ext='.png'):
        """그림 바이트를 첨부로 저장한다(부분 캡처 - 사람이 영역을 골라 자른 것). 파일 이름."""
        self._writing()
        with self.lock:
            self._ensure_attach_dir()
            names = list(self.attach.get(aid) or [])
            fname = self._next_attach_name(aid, names, ext)
            with open(os.path.join(self.attach_dir, fname), 'wb') as f:
                f.write(raw)
            names.append(fname)
            self._set_attach(aid, names)
            return fname

    def add_images(self, aid, images):
        """메모창에 붙인 그림(브8). images = [{name?, mime?, data(base64 또는 data URL)}…]."""
        if not images:
            return []
        self._writing()
        import base64
        added = []
        with self.lock:
            self._ensure_attach_dir()
            names = list(self.attach.get(aid) or [])
            for img in images:
                data = img.get('data') or ''
                if data.startswith('data:') and ',' in data:
                    data = data.split(',', 1)[1]
                try:
                    raw = base64.b64decode(data)
                except Exception:
                    continue
                mime = (img.get('mime') or '').lower()
                ext = ('.jpg' if 'jpeg' in mime or 'jpg' in mime else
                       '.gif' if 'gif' in mime else
                       '.webp' if 'webp' in mime else '.png')
                fname = self._next_attach_name(aid, names, ext)
                try:
                    with open(os.path.join(self.attach_dir, fname), 'wb') as f:
                        f.write(raw)
                except Exception:
                    continue
                names.append(fname)
                added.append(fname)
            if added:
                self._set_attach(aid, names)
        return added

    def add_attachment_file(self, aid, src_path):
        """보강 창에서 사람이 파일을 골라 붙인다(그림 또는 동영상 - Win+Alt+R 로 찍은 것)."""
        self._writing()
        with self.lock:
            self._ensure_attach_dir()
            names = list(self.attach.get(aid) or [])
            ext = (os.path.splitext(src_path)[1] or '.png').lower()
            fname = self._next_attach_name(aid, names, ext)
            shutil.copyfile(src_path, os.path.join(self.attach_dir, fname))
            names.append(fname)
            self._set_attach(aid, names)
            return fname

    def remove_attachment(self, aid, fname):
        self._writing()
        with self.lock:
            names = list(self.attach.get(aid) or [])
            if fname not in names:
                return False
            names.remove(fname)
            try:
                os.remove(os.path.join(self.attach_dir, fname))
            except Exception:
                pass
            self._set_attach(aid, names)
            return True

    def get_attachments(self, aid):
        with self.lock:
            return list(self.attach.get(aid) or [])

    def attachment_rows(self, aid):
        """보강 창 목록용. [{'name', 'kind', 'bytes', 'label'}] - 동영상은 크기를 같이 보인다."""
        with self.lock:
            out = []
            for fn in (self.attach.get(aid) or []):
                p = os.path.join(self.attach_dir, fn)
                try:
                    size = os.path.getsize(p)
                except Exception:
                    size = 0
                kind = attach_kind(fn)
                tag = {u'video': u'동영상', u'image': u'그림'}.get(kind, u'파일')
                out.append({'name': fn, 'kind': kind, 'bytes': size,
                            'label': u'%s  (%s · %s)' % (fn, tag, human_size(size))})
            return out

    def has_attachments(self):
        with self.lock:
            return any(self.attach.values())

    def count_attachments(self):
        with self.lock:
            return sum(len(v) for v in self.attach.values())

    def all_attachment_paths(self):
        """(aid, 절대경로) 전체 목록 - 이슈에 올리기가 드롭할 파일들."""
        with self.lock:
            out = []
            for aid, names in self.attach.items():
                for fn in names:
                    out.append((aid, os.path.join(self.attach_dir, fn)))
            return out

    # ── 화면 단위 캡처(1.2) ─────────────────────────────────────
    # ★주석마다 찍지 않는다. 화면(주소·해상도)당 1벌, 그 화면 항목 번호를 핀으로 겹친다.
    #   찍는 것은 launcher(CDP), 여기는 대상 목록을 내주고 결과를 기록한다.
    #   Store.lock 을 잡은 채 CDP 를 기다리지 않는다 - capture_targets() 로 스냅샷을 뽑아
    #   잠금을 놓은 뒤 찍고, 결과만 set_capture() 로 잠금 안에서 저장한다.
    def capture_targets(self):
        """결과에 실리는 화면마다 {key, url, viewport, pins:{aid:{label, box, fixed, path}}}.

        ★다른 화면 밑에 하위(1.1)로 표시되는 주석도 **자기 화면에** 그 label 로 그린다 -
          문서 번호와 핀 번호가 1:1 이어야 한다."""
        with self.lock:
            pages = self._render_pages()
            _roots, index, _loose = self._nested(pages)
            out = []
            for i, p in enumerate(pages, 1):
                key = self.key_of(p['url'], p['viewport'])
                pins = {}
                for aid, a in p['annotations'].items():
                    node = index.get(aid)
                    if not node:
                        continue
                    pins[aid] = {'label': node['label'] if node['screen'] == i
                                 else u'[%d] %s' % (node['screen'], node['label']),
                                 'box': a.get('boundingBox') if isinstance(a.get('boundingBox'), dict) else None,
                                 'fixed': bool(a.get('isFixed')),
                                 'path': a.get('elementPath') or ''}
                out.append({'key': key, 'screen': i, 'url': p['url'], 'viewport': p['viewport'],
                            'pins': pins, 'title': p['title'] or u''})
            return out

    def set_capture(self, key, files, partial=False, reason=u'', pins=None, css_w=None,
                    css_h=None, dpr=None):
        """화면 하나의 캡처 결과를 기록한다(재캡처는 덮어쓴다 - 화면당 1벌)."""
        self._writing()
        key = tuple(key)
        with self.lock:
            rec = {'files': list(files or []), 'partial': bool(partial), 'reason': reason or u'',
                   'pins': dict(pins or {}), 'at': _now(), 'css_w': css_w, 'css_h': css_h,
                   'dpr': dpr}
            self.captures[key] = rec
            if not self._replaying:
                self._append_event({'t': 'capture', 'url': key[0], 'viewport': key[1],
                                    'files': rec['files'], 'partial': rec['partial'],
                                    'reason': rec['reason'], 'pins': rec['pins'],
                                    'at': rec['at'], 'css_w': css_w, 'css_h': css_h,
                                    'dpr': dpr})
                self.write_md()

    def capture_of(self, key):
        with self.lock:
            return self.captures.get(tuple(key))

    def has_captures(self):
        with self.lock:
            return any(c.get('files') for c in self.captures.values())

    def all_capture_paths(self):
        """(key, 절대경로) - 화면 순서대로. zip·Jira 가 담는 파일들."""
        with self.lock:
            out = []
            for p in self._render_pages():
                key = self.key_of(p['url'], p['viewport'])
                for fn in (self.captures.get(key) or {}).get('files') or []:
                    out.append((key, os.path.join(self.capture_dir, fn)))
            return out

    def _capture_lines(self, key, index):
        """화면 블록의 '- 캡처:' 줄. 번호가 바뀐 뒤면 갱신이 필요하다고 적는다."""
        c = self.captures.get(key)
        if not c or not c.get('files'):
            return []
        stale = False
        for aid, label in (c.get('pins') or {}).items():
            node = index.get(aid)
            now = (node['label'] if node else None)
            if node and node['key'] != key:
                now = u'[%d] %s' % (node['screen'], node['label'])
            if now != label:
                stale = True
                break
        tail = u''
        if c.get('partial'):
            tail += u' (아래쪽 잘림%s)' % ((u' · ' + c['reason']) if c.get('reason') else u'')
        if stale:
            tail += u' (번호 바뀜 · 캡처 갱신 필요)'
        lines = []
        for j, fn in enumerate(c['files']):
            lines.append(u'- 캡처: 캡처/%s%s' % (fn, tail if j == len(c['files']) - 1 else u''))
        return lines

    # ── 저장 ──────────────────────────────────────────────────
    def _append_event(self, rec):
        """이벤트를 현재 회차에 붙인다(옛 _append_jsonl). 실패는 조용히 넘기지 않는다 -
        기록이 안 남으면 재시작 뒤 목록이 달라지는데, 그것이 가장 알아채기 어려운 고장이다."""
        self._writing()
        return self.db.add_event(self.round_id, rec)

    def write_md(self):
        if self.readonly:
            return                              # 지난 회차를 보는 중에 latest.md 를 덮지 않는다
        try:
            with io.open(self.md_path, 'w', encoding='utf-8') as f:
                f.write(self.render())
        except Exception:
            pass

    def _render_pages(self):
        """결과에 실리는 화면 목록. 화면 번호는 이 순서로 붙는다(목록과 렌더가 같아야 한다)."""
        return [p for p in self.pages.values()
                if p['annotations'] or p['console'] or p['network'] or p.get('api')
                or p.get('layout')]

    def _layout_rows(self, p):
        """레이아웃 모드에서 옮긴 것(브3) - 목록·결과 문서가 같은 표현을 쓴다."""
        lay = p.get('layout') or {}
        out = []
        for m in (lay.get('moved') or []):
            f, t = m.get('from') or {}, m.get('to') or {}
            out.append({'label': u'⇄ %s' % (m.get('label') or '?'),
                        'detail': u'(%s,%s %sx%s) → (%s,%s %sx%s)%s'
                        % (f.get('x'), f.get('y'), f.get('w'), f.get('h'),
                           t.get('x'), t.get('y'), t.get('w'), t.get('h'),
                           u' · %s' % m['sel'] if m.get('sel') else u'')})
        order = lay.get('order')
        if order:
            out.append({'label': u'⇄ 순서 변경',
                        'detail': u' → '.join(str(x) for x in (order.get('to') or []))})
        for pl in (lay.get('placements') or []):
            out.append({'label': u'+ %s' % (pl.get('type') or '?'),
                        'detail': u'%sx%s @(%s,%s)%s'
                        % (pl.get('w'), pl.get('h'), pl.get('x'), pl.get('y'),
                           u' "%s"' % pl['text'] if pl.get('text') else u'')})
        return out

    def _render_index(self, roots_by_page):
        """항목 목차. 한 줄에 하나(하위는 들여쓴 줄로) - 이슈 댓글에 그대로 붙일 수 있게.

        ★SC-295 인계 문서의 QA 요청 ④: 댓글이 "첨부하였습니다" 한 줄이라 이슈 검색·추적에
          안 걸렸다. 문서 맨 앞에 제목 줄이 있으면 그걸 그대로 붙이면 된다.
        ★번호를 매기는 것은 최상위(루트)뿐이다 - 하위는 상위 밑에 들여써 보여 준다.
          하위까지 따로 세면 '항목 N건' 이 사람이 실제로 처리할 덩어리 수와 달라진다."""
        # 어디부터 볼지가 목차에서 보이게 한다.
        # ★미지정을 '낮음' 보다 앞에 둔다 - '낮음' 은 나중에 해도 된다고 사람이 판단한
        #   것이고, 미지정은 아직 판단이 안 된 것이라 눈에 띄어야 한다.
        rank = {'high': 0, 'mid': 1, '': 2, 'low': 3}

        def prio_of(aid):
            return (self.meta.get(aid) or {}).get('priority') or ''

        def memo_of(a):
            memo = (a.get('comment') or u'').replace(u'\n', u' ').strip()
            return (memo[:60] + u'…') if len(memo) > 60 else memo

        items = []
        for i, roots in enumerate(roots_by_page, 1):
            for j, node in enumerate(roots, 1):
                items.append((rank.get(prio_of(node['aid']), 3), i, j, node))
        if not items:
            return []
        items.sort(key=lambda x: (x[0], x[1], x[2]))
        rows = []
        for n, (_r, i, _j, node) in enumerate(items, 1):
            aid, a = node['aid'], node['ann']
            pr = PRIORITY_LABEL.get(prio_of(aid), u'')
            rows.append(u'%d. [화면 %d] %s번 %s — %s%s'
                        % (n, i, node['label'], a.get('element') or u'?',
                           memo_of(a) or u'(메모 없음)',
                           u'  *(%s)*' % pr if pr else u''))
            for child in self._flatten(node['children']):
                ca = child['ann']
                rows.append(u'   - %s %s — %s' % (child['label'],
                            ca.get('element') or u'?', memo_of(ca) or u'(메모 없음)'))
        return [u'## 항목 %d건 (우선순위순)' % len(items), u''] + rows + [u'']

    def render(self):
        with self.lock:
            pages = self._render_pages()
            total = sum(len(p['annotations']) for p in pages)
            lines = [u'# 화면 주석 - %s' % _now(), u'',
                     u'- 화면 %d개 · 주석 %d건' % (len(pages), total),
                     u'- 도구: 화면주석-QA %s' % VER.label(), u'']
            if self.closing:
                # 받는 사람이 먼저 읽는 자리다(요약 바로 밑).
                lines += [u'## 총평', u'']
                lines += self.closing.strip().splitlines()
                lines.append(u'')
            roots_by_page, index, loose = self._nested(pages)
            lines += self._render_index(roots_by_page)
            if not pages:
                lines += [u'---', u'', u'(아직 주석이 없습니다)', u'']
                return u'\n'.join(lines)
            # 연결의 반대 방향(상위 ←). 다른 화면 하위가 이 화면에서 빠져나간 개수도 센다.
            back = {}
            for aid, node in index.items():
                if node['parent']:
                    back.setdefault(node['parent'], []).append(aid)
            for i, p in enumerate(pages, 1):
                lines += [u'---', u'',
                          u'# [화면 %d] %s  (%s)' % (i, p['title'] or u'(제목 없음)',
                                                    p['viewport'] or u'해상도 미상'), u'',
                          u'- 주소: %s' % p['url'],
                          u'- 해상도: %s%s' % (p['viewport'] or u'미상',
                                            u' · DPR %s' % p['dpr'] if p['dpr'] else u''),
                          u'- 시각: %s ~ %s' % (p['first_seen'], p['last_seen'])]
                # 화면 단위 캡처(1.2) - 있을 때만. 핀 번호는 아래 '## N.' 과 같다.
                lines += self._capture_lines(self.key_of(p['url'], p['viewport']), index)
                basis = _color_basis(p)
                if basis:
                    # 색 지적을 받는 쪽이 어떤 테마의 값을 보고 있는지 알아야 한다.
                    lines.append(u'- 색 기준: %s' % basis)
                if p['in_iframe']:
                    lines.append(u'- 이 화면은 iframe 안이었습니다')
                if p['referrer']:
                    lines.append(u'- 이전 화면: %s' % p['referrer'])
                lines.append(u'- 주석 %d건' % len(p['annotations']))
                # ★이 화면 소속인데 다른 화면의 상위 밑으로 옮겨 표시된 것이 있으면
                #   '주석 N건' 인데 본문이 그보다 적어 보인다 - 그 이유를 적는다.
                moved_out = sum(1 for aid in p['annotations']
                                if index.get(aid) and index[aid]['screen'] != i)
                if moved_out:
                    lines.append(u'- 이 중 %d건은 상위 주석이 있는 다른 화면 밑에 표시됩니다'
                                 % moved_out)
                lines.append(u'')
                lines += self._render_annotations(roots_by_page[i - 1], index, loose)
                lines += self._render_layout(p)
                lines += self._render_noise(p)
            return u'\n'.join(lines)

    @staticmethod
    def _ref_label(index, aid):
        node = index.get(aid)
        if not node:
            return u'(삭제된 주석)'
        a = node['ann']
        comment = a.get('comment') or u''
        tail = u' — "%s"' % comment[:40] if comment else u''
        return u'[화면 %d] %s번 %s%s' % (node['screen'], node['label'],
                                       a.get('element') or u'?', tail)

    def _render_annotations(self, nodes, index, loose):
        """주석 하나의 본문(재귀 - 하위는 바로 뒤에 이어 나온다).

        ★번호는 트리에서 미리 매긴 label('1.1') 을 그대로 쓴다 - enumerate 로 다시
          매기면 하위가 섞인 순서에서 번호가 화면·목차와 어긋난다."""
        back_loose = {}
        for src, dsts in loose.items():
            for dst in dsts:
                back_loose.setdefault(dst, []).append(src)
        lines = []
        for node in nodes:
            lines += self._render_one(node, index, loose, back_loose)
            lines += self._render_annotations(node['children'], index, loose)
        return lines

    def _render_one(self, node, index, loose, back_loose):
        aid, a = node['aid'], node['ann']
        m = self.meta.get(aid) or {}
        head = a.get('element') or u'?'
        if node['foreign']:
            # ★다른 화면의 하위 주석이다 - 어느 화면 것인지 제목에서부터 밝힌다.
            head = u'[화면 %d] %s' % (node['home'], head)
        lines = [u'## %s. %s' % (node['label'], head), u'',
                 u'> %s' % (a.get('comment') or u'(메모 없음)'), u'']
        if node['foreign']:
            hp = self._page(node['key'])
            lines.append(u'- 화면: [화면 %d] %s (%s)'
                         % (node['home'], hp['title'] or u'(제목 없음)',
                            hp['viewport'] or u'해상도 미상'))
            lines.append(u'- 주소: %s' % node['key'][0])
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
        # ★연결 = 하위(1.1). 자식이 된 것은 바로 밑에 중첩돼 나오므로 따로 줄을 안
        #   낸다. 하위가 되지 못한 것(loose)만 글로 남긴다 - 조용히 사라지지 않게.
        for dst in loose.get(aid, []):
            lines.append(u'- 하위: → %s' % self._ref_label(index, dst))
        for src in back_loose.get(aid, []):
            lines.append(u'- 상위: ← %s' % self._ref_label(index, src))
        for fn in (self.attach.get(aid) or []):
            # ★그림 본문은 여기 없다 - 파일 이름만 적는다(위 첨부 절 주석 참고).
            #   추출(zip) 때 이 이름 그대로 '첨부/' 안에 함께 담긴다.
            if attach_kind(fn) == 'video':
                try:
                    size = human_size(os.path.getsize(os.path.join(self.attach_dir, fn)))
                except Exception:
                    size = u''
                lines.append(u'- 첨부: 첨부/%s (동영상%s)' % (fn, u' ' + size if size else u''))
            else:
                lines.append(u'- 첨부: 첨부/%s' % fn)
        lines += [u'- 경로: `%s`' % (a.get('elementPath') or u''),
                  u'- 클래스: `%s`' % (a.get('cssClasses') or u'')]
        again = self._past_spots().get(
            (_spot_url(node['key'][0]), a.get('elementPath') or u''))
        if again:
            # ★회차를 넘겨 같은 자리가 또 올라왔다. 받는 쪽이 제일 먼저 볼 줄이다.
            lines.append(u'- ★재지적: 지난 회차에도 같은 자리 (%s)'
                         % u' · '.join(again[-3:]))
        if a.get('attrs'):
            # ★서버가 요소에 실어 보낸 값이다. '무엇이 표시되느냐' 가 아니라
            #   '무엇이 와 있느냐' 라서, 화면 탓인지 서버 탓인지를 여기서 가른다.
            lines.append(u'- 속성: %s' % str(a['attrs'])[:300])
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
        # ★예전에는 원본 스타일을 300자에서 잘라 통째로 냈다. 그 줄은 font-family
        #   목록이 앞을 먹어 매번 문장 중간에서 끊겼고, 그래서 읽히지 않았다.
        #   같은 정보를 배치·색·글자 세 줄로 나눠 전부 읽히게 한다.
        lay = _layout_bits(a.get('computedStyles'))
        if lay:
            lines.append(u'- 배치: %s' % u' · '.join(lay))
        col = _color_bits(a.get('computedStyles'))
        if col:
            lines.append(u'- 색: %s' % u' · '.join(col))
        txt = _text_bits(a.get('computedStyles'))
        if txt:
            lines.append(u'- 글자: %s' % u' · '.join(txt))
        if a.get('reactComponents'):
            lines.append(u'- React: %s' % a['reactComponents'])
        if a.get('sourceFile'):
            lines.append(u'- 소스: %s' % a['sourceFile'])
        lines.append(u'')
        return lines

    def _render_layout(self, p):
        """레이아웃 모드에서 옮긴 것(브3). 목록(_layout_rows)과 같은 내용을 쓴다."""
        rows = self._layout_rows(p)
        if not rows:
            return []
        lines = [u'### 레이아웃 변경 %d건' % len(rows), u'']
        for r in rows:
            lines.append(u'- %s — %s' % (r['label'], r['detail']))
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
        if p.get('api'):
            lines += [u'### 이 화면의 조회 응답 %d건 (성공했으나 응답이 짧은 것만)' % len(p['api']),
                      u'']
            for a in p['api']:
                hits = u' ·%d회' % a['hits'] if a.get('hits', 1) > 1 else u''
                lines.append(u'- `%s` %s → %s%s' % (a['ts'], a['request'], a['status'], hits))
                if a.get('body'):
                    lines.append(u'  - 응답: %s' % a['body'])
            lines.append(u'')
        if p['network']:
            lines += [u'### 이 화면의 실패한 요청 %d건' % len(p['network']), u'']
            for n in p['network']:
                tail = u' (%s)' % n['reason'] if n['reason'] else u''
                lines.append(u'- `%s` %s → %s%s' % (n['ts'], n['request'], n['status'], tail))
                if n.get('body'):
                    # 서버가 적어 보낸 사유. 이 줄이 없으면 받는 쪽이 되묻는다.
                    lines.append(u'  - 응답: %s' % n['body'])
            lines.append(u'')
        return lines

    # ── 복원 · 비우기 ──────────────────────────────────────────
    def _apply_event(self, rec):
        """이벤트 하나를 메모리에 반영한다(재생 전용 - persist 하지 않는다)."""
        t = rec.get('t')
        if t == 'annotation':
            self.apply(rec.get('payload') or {}, persist=False)
        elif t == 'console':
            self.add_console(self.key_of(rec.get('url'), rec.get('viewport')),
                             rec.get('text'), rec.get('level') or 'error')
        elif t == 'network':
            self.add_network(self.key_of(rec.get('url'), rec.get('viewport')),
                             rec.get('request'), rec.get('status'),
                             rec.get('reason') or '', rec.get('token') or '')
        elif t == 'api':
            self.add_api(self.key_of(rec.get('url'), rec.get('viewport')),
                         rec.get('request'), rec.get('status'), rec.get('body') or '')
        elif t == 'netbody':
            self.set_network_body(self.key_of(rec.get('url'), rec.get('viewport')),
                                  rec.get('token'), rec.get('body') or '')
        elif t == 'merge':
            self.merge_unknown(rec.get('url'), rec.get('viewport'))
        elif t == 'meta':
            self.set_meta(rec.get('aid'), rec.get('note') or u'',
                          rec.get('refs') or [], rec.get('expected') or u'',
                          rec.get('priority') or u'')
        elif t == 'closing':
            self.set_closing(rec.get('text') or u'')
        elif t == 'order':
            self.set_order(self.key_of(rec.get('url'), rec.get('viewport')),
                           rec.get('ids') or [])
        elif t == 'porder':
            self.set_page_order(rec.get('keys') or [])
        elif t == 'corder':
            self.set_child_order(rec.get('aid'), rec.get('ids') or [])
        elif t == 'layout':
            self.set_layout({'url': rec.get('url'), 'viewport': rec.get('viewport'),
                             'layout': rec.get('layout')})
        elif t == 'attach':
            self._set_attach(rec.get('aid'), rec.get('files') or [])
        elif t == 'capture':
            key = self.key_of(rec.get('url'), rec.get('viewport'))
            self.set_capture(key, rec.get('files') or [], rec.get('partial'),
                             rec.get('reason') or u'', rec.get('pins') or {},
                             rec.get('css_w'), rec.get('css_h'), rec.get('dpr'))
            if rec.get('at'):
                self.captures[key]['at'] = rec['at']
        # 모르는 t 는 무시한다 - 옛 기록(skip 등)과 앞으로의 확장 양쪽을 위해.

    def replay(self):
        """프로그램을 다시 켰을 때 현재 회차의 이벤트를 재생해 목록을 되살린다.

        ★되돌리기(skip)는 event id 로 DB 가 미리 걸러 준다 - 옛 jsonl 처럼 두 바퀴 돌 필요가 없다."""
        with self.lock:
            self._replaying = True
            try:
                for _eid, rec in self.db.events(self.round_id):
                    if rec:
                        self._apply_event(rec)
            finally:
                self._replaying = False
            self.write_md()
            return self.counts()

    # ── 되돌리기 · 지난 기록 ──────────────────────────────────
    def undoable(self):
        """되돌릴 수 있는 마지막 파괴적 이벤트. (event_id, 설명) 또는 None."""
        evs = self.db.events(self.round_id)         # skip 된 것은 이미 빠져 있다
        for eid, rec in reversed(evs):
            if not rec or rec.get('t') != 'annotation':
                continue
            pay = rec.get('payload') or {}
            kind = pay.get('kind')
            if kind == 'clear':
                return eid, u'전체 지우기 (%s)' % (pay.get('url') or u'')
            if kind == 'delete':
                n = len(pay.get('annotations') or [])
                return eid, u'주석 %d건 삭제 (%s)' % (n, pay.get('url') or u'')
        return None

    def undo(self):
        """마지막 지우기를 취소한다. 되살린 주석 수를 돌려준다.

        ★과거 이벤트를 고치지 않는다. skip 에 그 event id 를 넣고 전체를 다시 재생한다 -
          원본이 증거이기 때문이다. 줄 번호가 아니라 id 라서 회차를 가져와도 어긋나지 않는다."""
        self._writing()
        hit = self.undoable()
        if not hit:
            return 0
        eid, _desc = hit
        before = self.counts()[1]
        with self.lock:
            self.db.add_skip(self.round_id, eid)
        self._reload()
        return self.counts()[1] - before

    def _clear_memory(self):
        self.pages.clear()
        self.meta.clear()
        self.order.clear()
        self.child_order.clear()
        self.moved.clear()
        self.attach.clear()     # 파일은 그대로 - 'attach' 이벤트가 replay 로 되살린다
        self.captures.clear()
        self.closing = u''

    def _reload(self):
        """인메모리 상태를 기록에서 다시 만든다(skip 을 반영)."""
        with self.lock:
            self._clear_memory()
            self.replay()

    def load_archive(self, path):
        """옛 jsonl(1.1 의 archive 파일)을 **닫힌 회차로 가져온다**(프5, 1.2 에서 의미 변경).

        ★현재 목록에 합치지 않는다. 1.1 까지는 현재 기록 뒤에 이어붙였는데, 그러면 총평·
          순서·메타가 옛 값으로 덮이고 skip(줄 번호)이 어긋났다 - "이전 내역을 찾아가면
          값이 변한다" 의 원인이었다. 가져온 회차는 지난 회차 창에서 읽기 전용으로 본다.
        ★옆의 '<stamp>-attach' 폴더가 있으면 그 회차 첨부로 함께 복사한다.
        돌려주는 값: (round_id, 넣은 이벤트 수)."""
        base = os.path.basename(path)
        when, stem = qadb._stamp_of(base)
        adir = None
        if stem:
            cand = os.path.join(os.path.dirname(path), stem + '-attach')
            if os.path.isdir(cand):
                adir = cand
        with self.lock:
            rid, n, _cp = qadb.import_jsonl(
                self.db, path, self.label, status='closed', source='import',
                attach_from=adir, attach_root=os.path.join(self.out_dir, 'attach'),
                started_at=when, closed_at=when or _now(), reason=u'가져오기: %s' % base)
            # 개수를 채워 두면 회차 창에서 재생 없이 보인다.
            try:
                view = RoundView(self.out_dir, self.db, rid)
                pages, total = view.counts()
                self.db.set_round_counts(rid, pages, total)
            except Exception:
                pass
            self._spots = None
        return rid, n

    def export(self, dest_path, text=None):
        """현재 내용을 파일로 저장한 뒤 회차를 닫는다. 저장이 실패하면 회차를 닫지 않는다.

        text 를 주면 그것을 그대로 쓴다 - 사람이 '저장 전 확인' 창에서 고친 내용이다(프7).
        ★첨부·캡처가 있으면 dest_path 가 .zip 이어야 한다 - 문서(.md)와 그림(첨부/·캡처/)을
          한 파일로 묶어야 "폴더째 넘겨야 그림이 간다" 를 피할 수 있다(고르는 것은 app.py)."""
        self._writing()
        with self.lock:
            if text is None:
                text = self.render()
            self.write_bundle(dest_path, text)
            self.reset(keep_archive=True, reason=u'추출', export_path=dest_path)
            return dest_path

    def write_bundle(self, dest_path, text):
        """문서(+첨부·캡처)를 파일로 쓴다. 회차는 건드리지 않는다(지난 회차 다시 보내기도 쓴다)."""
        with self.lock:
            if dest_path.lower().endswith('.zip'):
                import zipfile
                md_name = os.path.splitext(os.path.basename(dest_path))[0] + '.md'
                with zipfile.ZipFile(dest_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr(md_name, text)
                    for names in self.attach.values():
                        for fn in names:
                            fpath = os.path.join(self.attach_dir, fn)
                            if os.path.exists(fpath):
                                zf.write(fpath, u'첨부/' + fn)
                    for _key, fpath in self.all_capture_paths():
                        if os.path.exists(fpath):
                            zf.write(fpath, u'캡처/' + os.path.basename(fpath))
            else:
                with io.open(dest_path, 'w', encoding='utf-8') as f:
                    f.write(text)
            return dest_path

    def reset(self, keep_archive=True, reason=u'비우기', export_path=''):
        """목록을 비운다 = 현재 회차를 닫고 새 회차를 연다. 기록·첨부·캡처는 닫힌 회차에 그대로
        남는다(지난 회차 창에서 읽기 전용으로 본다). keep_archive 는 호환용 인자 - 1.2 부터는
        어느 경우에도 지우지 않는다."""
        self._writing()
        with self.lock:
            self._close_and_reopen(reason, export_path)
            self._clear_memory()
            self.write_md()


class RoundView(Store):
    """지난(닫힌) 회차를 읽기 전용으로 되살린 Store. render/tree_rows/첨부·캡처 목록만 쓴다.

    ★쓰기는 전부 예외(ReadOnlyRound). 무시하지 않는다 - 무시하면 화면과 기록이 어긋난 채
      지나가서 "고쳤는데 다음에 또 원래대로" 가 된다. latest.md 도 덮지 않는다."""
    readonly = True

    def __init__(self, out_dir, db, round_id):
        Store.__init__(self, out_dir, db=db, round_id=round_id)
        self.replay()

    def write_md(self):
        return
