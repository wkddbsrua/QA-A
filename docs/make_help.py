# -*- coding: utf-8 -*-
"""사용법 문서를 만든다.  docs/사용법.src.html + docs/icons/*.png → docs/사용법.html

그림은 **실제 툴바를 캡처한 것**이고, data URI 로 문서 안에 박는다.
  · 왜 data URI 인가: [사용법] 버튼이 exe 안에서 HTML 한 개만 꺼내 열기 때문에,
    바깥 파일을 참조하면 그림이 깨진다. 한 파일로 자립해야 한다.
  · 그림을 다시 뽑는 방법은 README '사용자용 문서' 절에 적어 두었다.

문서를 고칠 때는 사용법.src.html 을 고치고 이 스크립트를 다시 돌린다.
"""
import base64
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, '사용법.src.html')
DST = os.path.join(ROOT, '사용법.html')
ICONS = os.path.join(ROOT, 'icons')

PLACEHOLDER = re.compile(r'\{\{img:([A-Za-z0-9_.-]+)\}\}')


def main():
    if not os.path.exists(SRC):
        print('원본이 없습니다: %s' % SRC)
        return 1
    html = io.open(SRC, encoding='utf-8').read()

    used, missing = [], []

    def sub(m):
        name = m.group(1)
        p = os.path.join(ICONS, name)
        if not os.path.exists(p):
            missing.append(name)
            return ''
        with open(p, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('ascii')
        used.append((name, len(b64)))
        return 'data:image/png;base64,' + b64

    out = PLACEHOLDER.sub(sub, html)
    if missing:
        print('그림이 없습니다: %s' % ', '.join(sorted(set(missing))))
        print('  → scratchpad 의 capture_icons2.py 로 다시 뽑아 docs/icons 에 넣으세요.')
        return 1

    io.open(DST, 'w', encoding='utf-8').write(out)
    print('사용법.html  %.0f KB  (그림 %d개 박음)' % (os.path.getsize(DST) / 1024.0, len(used)))
    for n, ln in used:
        print('    %-16s %5.1f KB' % (n, ln / 1024.0))
    left = PLACEHOLDER.findall(out)
    if left:
        print('치환되지 않은 자리: %s' % left)
        return 1
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    sys.exit(main())
