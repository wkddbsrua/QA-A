# -*- coding: utf-8 -*-
"""agentation 번들의 화면 문구를 한글로 바꾼 사본을 만든다.

  node_modules/agentation/dist/index.mjs   (원본 — 읽기만 한다)
      → vendor/agentation.ko.mjs           (한글 사본 — src/inject.jsx 가 import)

번역표는 ko.strings.json 에 있고 이 스크립트는 적용만 한다.
게이트 3개 중 하나라도 0 이 아니면 종료코드 1 로 실패한다:
  ① 못 찾음      — 패키지 버전이 바뀌어 find 조각이 사라진 것
  ② 개수 불일치  — 문맥을 덜 못박아 의도보다 많이/적게 잡힌 것
  ③ 잔존 영문 UI — 치환 후에도 화면 문구로 보이는 영문이 남은 것(allow 목록 제외)

"꼼꼼히 봤다"는 지킨 척이 가능하지만 "잔존 0"은 파일을 조작하지 않으면 거짓말할 수 없다.
"""
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, 'node_modules', 'agentation', 'dist', 'index.mjs')
TABLE = os.path.join(ROOT, 'ko.strings.json')
DST_DIR = os.path.join(ROOT, 'vendor')
DST = os.path.join(DST_DIR, 'agentation.ko.mjs')

HANGUL = re.compile(u'[가-힣]')


def load_table():
    with io.open(TABLE, encoding='utf-8') as f:
        return json.load(f)


def build_allow(groups):
    """allow 목록을 (정확일치 set, 컴파일된 패턴 목록) 으로 만든다."""
    exact, pats = set(), []
    for g in groups:
        for e in g.get('exact', []):
            exact.add(e)
        for p in g.get('patterns', []):
            pats.append(re.compile(p))
    return exact, pats


def scan_leftover(text, exact, pats):
    """치환 후 남은 '화면 문구로 보이는 영문'을 센다.

    후보 판정: 한글이 없고, 영문자를 포함하며, ①공백을 포함한 두 낱말 이상이거나
              ②첫 글자가 대문자인 낱말. CSS/셀렉터/SVG 는 allow 패턴이 걸러낸다."""
    lits = re.findall(r'"((?:[^"\\\n]|\\.)*)"', text)
    left = {}
    for t in lits:
        if not t or len(t) > 120:
            continue
        if HANGUL.search(t):
            continue
        if not re.search(r'[A-Za-z]', t):
            continue
        two_words = bool(re.match(r'^[A-Za-z][^"]*\s[A-Za-z]', t))
        titled = bool(re.match(r'^[A-Z][a-z]{2,}$', t))
        if not (two_words or titled):
            continue
        if t in exact:
            continue
        if any(p.search(t) for p in pats):
            continue
        left[t] = left.get(t, 0) + 1
    return left


def main():
    if not os.path.exists(SRC):
        print('원본이 없습니다: %s' % SRC)
        print('  → npm install (agentation·react·react-dom·esbuild) 후 다시 실행')
        return 1

    table = load_table()
    rules = table['rules']
    exact, pats = build_allow(table['allow'])

    s = io.open(SRC, encoding='utf-8').read()
    missed, mismatch, applied = [], [], 0

    for r in rules:
        find, ko = r['find'], r['ko']
        want = r.get('count', 1)
        n = s.count(find)
        if n == 0:
            missed.append(find)
            continue
        if n != want:
            mismatch.append('%s → %d곳 (기대 %d)' % (find, n, want))
            continue
        s = s.replace(find, ko)
        applied += 1

    left = scan_leftover(s, exact, pats)

    if not os.path.isdir(DST_DIR):
        os.makedirs(DST_DIR)
    io.open(DST, 'w', encoding='utf-8').write(s)

    print('한글 사본: %s (%.0f KB)' % (DST, os.path.getsize(DST) / 1024.0))
    print('  치환         %d / %d' % (applied, len(rules)))
    print('  못 찾음      %d' % len(missed))
    print('  개수 불일치  %d' % len(mismatch))
    print('  잔존 영문 UI %d' % len(left))

    for label, items in (('못 찾음', missed), ('개수 불일치', mismatch)):
        if items:
            print('\n[%s]' % label)
            for i in items:
                print('  · %s' % i)
    if left:
        print('\n[잔존 영문 UI] — ko.strings.json 의 rules 에 넣거나, UI 가 아니면 allow 에 사유와 함께 등록')
        for t in sorted(left):
            print('  · "%s" (%d곳)' % (t, left[t]))

    bad = len(missed) + len(mismatch) + len(left)
    if bad:
        print('\n실패 — 위 %d건을 처리해야 통과다.' % bad)
        return 1
    print('\n통과 — 화면에 보이는 영문 0건.')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    sys.exit(main())
