# -*- coding: utf-8 -*-
"""단일 exe 를 만든다.  →  release/화면주석-QA.exe  (+ 바탕화면 복사)

배포되는 PC 에는 파이썬도, node 도, esbuild 도 없다. 그래서 exe 안에는
'이미 만들어진 산출물' 만 들어간다 - 실행 시점에 무언가를 빌드하지 않는다.
그걸 보장하려고 빌드 전에 산출물을 검사하고, 없거나 낡았으면 여기서 멈춘다.

빌드 순서
  1) python build_ko.py       한글 사본 (게이트 통과해야 함)
  2) python build_inject.py   주입 번들 (DOM 준비 후 실행하도록 감싼 것)
  3) python build_exe.py      이 파일
"""
import io
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
NAME = '화면주석-QA'
INJECT = os.path.join(ROOT, 'dist', 'inject.js')
HELP = os.path.join(ROOT, 'docs', '사용법.html')
VENDOR = os.path.join(ROOT, 'vendor', 'agentation.ko.mjs')
RELEASE = os.path.join(ROOT, 'release')
WORK = os.path.join(ROOT, 'build_work')


def check():
    """다른 PC 에서 '왜 안 되지' 가 되기 전에 여기서 잡는다."""
    problems = []
    if not os.path.exists(os.path.join(ROOT, 'version.py')):
        problems.append('version.py 없음 - 판 번호를 붙일 수 없다')
    if not os.path.exists(VENDOR):
        problems.append('vendor/agentation.ko.mjs 없음 → python build_ko.py')
    if not os.path.exists(HELP):
        problems.append('docs/사용법.html 없음 - 사용자에게 줄 문서가 빠진다')
    if not os.path.exists(INJECT):
        problems.append('dist/inject.js 없음 → python build_inject.py')
    else:
        head = open(INJECT, encoding='utf-8').read(400)
        if '__qaRun' not in head:
            problems.append('dist/inject.js 가 감싸지지 않은 번들이다(문서 생성 시점에 죽는다) '
                            '→ python build_inject.py 로 다시 만들 것')
        if os.path.getmtime(INJECT) < os.path.getmtime(VENDOR):
            problems.append('dist/inject.js 가 한글 사본보다 낡았다 → python build_inject.py')
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        problems.append('PyInstaller 없음 → pip install pyinstaller (빌드 PC 에만 필요)')
    return problems


def bump():
    """version.py 의 BUILD 를 +1 하고 (VERSION, BUILD) 를 돌려준다.

    ★빌드 '전에' 올린다 - exe 안에 박히는 번호(창 머리·툴바·전달 문서)와 파일 이름의
      번호가 같아야 한다. 빌드가 실패하면 그 번호는 버려진다(번호는 싸고, 어긋난
      번호는 비싸다 - 같은 이름으로 덮어써서 어느 exe 인지 못 가린 일이 있었다)."""
    p = os.path.join(ROOT, 'version.py')
    src = io.open(p, encoding='utf-8').read()
    m = re.search(r'^BUILD = (\d+)', src, re.M)      # 뒤에 주석이 붙어도 찾는다
    v = re.search(r"^VERSION = '([^']+)'$", src, re.M)
    if not m or not v:
        return None
    nxt = int(m.group(1)) + 1
    src = src[:m.start(1)] + str(nxt) + src[m.end(1):]
    io.open(p, 'w', encoding='utf-8', newline='').write(src)
    return v.group(1), nxt


def main():
    problems = check()
    if problems:
        print('빌드 전제가 갖춰지지 않았습니다:')
        for p in problems:
            print('  · %s' % p)
        return 1

    bumped = bump()
    if not bumped:
        print('version.py 에서 VERSION·BUILD 를 찾지 못했습니다.')
        return 1
    vstr, build = bumped
    tag = 'v%s_%03d' % (vstr, build)
    print('판 v%s (%03d) 로 빌드합니다.' % (vstr, build))

    sep = ';' if os.name == 'nt' else ':'
    cmd = [sys.executable, '-m', 'PyInstaller',
           '--noconfirm', '--clean', '--onefile', '--noconsole',
           '--name', NAME,
           '--distpath', RELEASE,
           '--workpath', WORK,
           '--specpath', WORK,
           '--add-data', '%s%s%s' % (INJECT, sep, 'dist'),
           '--add-data', '%s%s%s' % (HELP, sep, 'docs'),
           os.path.join(ROOT, 'app.py')]
    print('빌드 중... (1~2분)')
    r = subprocess.run(cmd)
    if r.returncode != 0:
        return r.returncode

    ext = '.exe' if os.name == 'nt' else ''
    exe = os.path.join(RELEASE, NAME + ext)
    if not os.path.exists(exe):
        print('산출물을 찾지 못했습니다: %s' % exe)
        return 1
    # ★판 번호를 파일 이름에 넣는다. 같은 이름으로 덮어쓰면 "이전 exe 에서는 됐는데"
    #   라는 보고를 어느 판과 비교해야 하는지 알 수 없다(지난 exe 를 남겨 둔다).
    final = os.path.join(RELEASE, '%s_%s%s' % (NAME, tag, ext))
    try:
        if os.path.exists(final):
            os.remove(final)        # 같은 번호로 다시 빌드한 경우(빌드 실패 후 재시도)
        os.replace(exe, final)
        exe = final
    except OSError as e:
        print('번호 붙이기 실패(무시 가능): %s' % e)
    print('\n%s  (%.1f MB)' % (exe, os.path.getsize(exe) / 1048576.0))

    desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
    if os.path.isdir(desktop):
        try:
            shutil.copy2(exe, desktop)
            print('바탕화면에 복사: %s' % os.path.join(desktop, os.path.basename(exe)))
        except Exception as e:
            print('바탕화면 복사 실패(무시 가능): %s' % e)

    print('\n서명이 없는 exe 라 처음 실행할 때 SmartScreen 경고가 날 수 있습니다.')
    print('  → [추가 정보] → [실행]')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    sys.exit(main())
