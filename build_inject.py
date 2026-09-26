# -*- coding: utf-8 -*-
"""주입 스크립트를 번들한다.  src/inject.jsx → dist/inject.js

★번들 전체를 DOM 준비 후로 미룬다.
  이 스크립트는 CDP Page.addScriptToEvaluateOnNewDocument 로 등록되므로 문서가
  만들어지는 순간(document.body 도 document.head 도 아직 없는 시점)에 실행된다.
  agentation 번들은 모듈 초기화 때 스타일을 head 에 넣으므로 그대로 두면
  "Cannot read properties of null (reading 'appendChild')" 로 죽는다(실측).
  그래서 esbuild 산출물을 함수 안에 넣고 DOMContentLoaded 이후에 부른다.

node·esbuild 는 이 빌드에만 필요하다 - 배포되는 exe 에는 산출물만 들어간다.
"""
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, 'src', 'inject.jsx')
RAW = os.path.join(ROOT, 'dist', 'inject.raw.js')
OUT = os.path.join(ROOT, 'dist', 'inject.js')
ESBUILD = os.path.join(ROOT, 'node_modules', '.bin',
                       'esbuild.cmd' if os.name == 'nt' else 'esbuild')

WRAP_HEAD = (
    '/* 화면 주석 - 주입 번들 (자동 생성물. src/inject.jsx 와 build_inject.py 를 고칠 것) */\n'
    '(function(){\n'
    # 표식은 window 가 아니라 document 에 남긴다. iframe 은 빈 문서(about:blank) 시절의
    # window 를 재사용하므로, window 표식으로는 "이 문서에 스크립트가 들어왔는가" 를
    # 구분할 수 없다. 프로그램이 이 표식을 보고 빠진 문서에 직접 넣는다.
    # __qaInjected/__qaReady 는 진단용(숫자로 원인을 가른다).
    'try{document.__qaDoc=(document.__qaDoc||0)+1;'
    'window.__qaInjected=(window.__qaInjected||0)+1;'
    'window.__qaReady=document.readyState;}catch(e){}\n'
    'function __qaRun(){\n'
)
WRAP_TAIL = (
    '\n}\n'
    'try{\n'
    '  if (document.readyState === "loading") {\n'
    '    document.addEventListener("DOMContentLoaded", __qaRun, { once: true });\n'
    '  } else { __qaRun(); }\n'
    '}catch(e){ if (window.console) console.warn("[화면주석] 시작 실패", e); }\n'
    '})();\n'
)


def main():
    if not os.path.exists(ESBUILD):
        print('esbuild 가 없습니다: %s' % ESBUILD)
        print('  → 이 폴더에서 npm install 이 필요합니다(빌드 전용. 배포 exe 에는 불필요).')
        return 1
    vendor = os.path.join(ROOT, 'vendor', 'agentation.ko.mjs')
    if not os.path.exists(vendor):
        print('한글 사본이 없습니다. 먼저 build_ko.py 를 실행하세요.')
        return 1

    dist = os.path.join(ROOT, 'dist')
    if not os.path.isdir(dist):
        os.makedirs(dist)

    cmd = [ESBUILD, SRC, '--bundle', '--format=iife', '--minify', '--jsx=automatic',
           '--define:process.env.NODE_ENV="production"', '--outfile=%s' % RAW]
    r = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(r.stdout or '')
    sys.stderr.write(r.stderr or '')
    if r.returncode != 0:
        print('번들 실패')
        return r.returncode

    body = io.open(RAW, encoding='utf-8').read()
    io.open(OUT, 'w', encoding='utf-8').write(WRAP_HEAD + body + WRAP_TAIL)
    os.remove(RAW)
    print('dist/inject.js  %.0f KB  (DOM 준비 후 실행하도록 감쌈)'
          % (os.path.getsize(OUT) / 1024.0))
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    sys.exit(main())
