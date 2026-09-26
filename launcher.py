# -*- coding: utf-8 -*-
"""브라우저를 띄우고 모든 문서에 주석 스크립트를 자동 주입한다.

왜 프로그램이 브라우저를 띄우는가
  북마크릿은 ①페이지를 옮기면 사람이 다시 눌러야 하고 ②페이지 안에서 실행되니
  https 화면에서 로컬로 결과를 되돌릴 수 없다(mixed content). 확장은 크롬 136+ 가
  자동 설치를 막는다. 프로그램이 브라우저를 띄워 CDP 로 주입하면 그 셋이 한꺼번에
  사라진다 - 브라우저에 설치되는 것이 하나도 없고, 페이지마다 누를 것도 없다.

주입 지점
  Page.addScriptToEvaluateOnNewDocument - 문서가 새로 뜰 때마다, 하위 프레임까지
  자동 실행된다. 이미 떠 있는 문서에는 Runtime.evaluate 로 한 번 넣는다.

수집 경로
  Runtime.addBinding('__qaPush') - 페이지에서 부르면 CDP 로 곧장 프로그램에 온다.
  네트워크를 쓰지 않으므로 https 여부와 무관하다.
"""
import base64
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import queue
import threading
import time
import urllib.request

from cdp import CDP, WSError

BINDING = '__qaPush'
HOST_ID = '__qa_annotator_host'

# 결함이 아니라 잡음인 요청. 이것만으로 '화면 1건' 이 결과에 끼면 읽는 사람이 헷갈린다.
NOISE_REQUESTS = ('/favicon.ico', '/apple-touch-icon', '/robots.txt',
                  '/browserconfig.xml', '/site.webmanifest')


def _is_noise(url):
    u = (url or '').split('?')[0].lower()
    return any(n in u for n in NOISE_REQUESTS)   # apple-touch-icon.png 처럼 확장자가 붙는다


BODY_CAP = 300          # 실패 응답에서 남길 길이. 사유는 한 문장이라 이만큼이면 넉넉하다.
API_BODY_MAX = 2048     # 성공 응답은 '이만큼보다 작을 때만' 담는다.
                        #   ★큰 목록 응답은 비어 있을 리가 없어 진단 가치가 없고, 전부 담으면
                        #     전달용 문서가 부풀어 붙여넣을 수 없게 된다. 작은 것만 담으면
                        #     '비었다' 를 앱이 판정하지 않고도 근거가 남는다.
API_TYPES = ('XHR', 'Fetch')            # 문서·이미지·스크립트는 담지 않는다


def _tidy_body(text):
    """실패 응답 본문을 한 줄로 줄인다.

    ★HTML 오류 페이지는 버린다. 톰캣·nginx 기본 페이지에는 사유가 없고 태그만 길어서,
      남기면 결과 문서가 읽을 수 없게 부푼다. 우리가 원하는 것은 서버가 적어 보낸
      JSON/텍스트 사유 한 줄이다."""
    t = ' '.join(str(text or '').split())
    if not t or t[:1] == '<':
        return ''
    return t[:BODY_CAP]

# 이 PC 에는 다 깔려 있어도 다른 PC 에는 없을 수 있다. 그래서 찾는 곳을 넓게 잡고,
# 못 찾으면 사람이 읽고 조치할 수 있는 문장으로 알린다(스택트레이스 금지).
BROWSERS = [
    ('크롬', 'chrome.exe', [
        r'%ProgramFiles%\Google\Chrome\Application\chrome.exe',
        r'%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe',
        r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe',
        r'%ProgramFiles%\Chromium\Application\chrome.exe',
    ]),
    ('엣지', 'msedge.exe', [
        r'%ProgramFiles%\Microsoft\Edge\Application\msedge.exe',
        r'%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe',
        r'%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe',
    ]),
]


def _from_registry(exe_name):
    """App Paths 레지스트리 - 비표준 경로에 설치된 경우까지 잡는다."""
    if os.name != 'nt':
        return None
    try:
        import winreg
    except ImportError:
        return None
    key = r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\%s' % exe_name
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (0, getattr(winreg, 'KEY_WOW64_32KEY', 0)):
            try:
                with winreg.OpenKey(root, key, 0, winreg.KEY_READ | view) as k:
                    path = winreg.QueryValue(k, None)
                    if path:
                        path = path.strip('"')
                        if os.path.exists(path):
                            return path
            except OSError:
                continue
    return None


def find_browser():
    """크롬 → 엣지 순으로 찾는다. (이름, 실행경로) 또는 (None, None).

    찾는 순서: 환경변수 지정 → 표준 설치 경로 → App Paths 레지스트리 → PATH.
    윈도우 10/11 이면 엣지는 기본 탑재라 보통 여기서 걸린다."""
    override = os.environ.get('QA_BROWSER')
    if override and os.path.exists(override):
        return ('지정된 브라우저', override)
    for name, exe_name, paths in BROWSERS:
        for p in paths:
            full = os.path.expandvars(p)
            if os.path.exists(full):
                return (name, full)
        found = _from_registry(exe_name)
        if found:
            return (name, found)
        found = shutil.which(exe_name)
        if found:
            return (name, found)
    return (None, None)


NO_BROWSER_MSG = (
    '크롬이나 엣지를 찾지 못했습니다.\n\n'
    '이 프로그램은 이미 설치된 브라우저를 빌려 씁니다(브라우저에 무엇을 설치하지는 않습니다).\n'
    '· 윈도우 10/11 이면 엣지가 기본으로 있습니다 - 시작 메뉴에서 "Edge" 를 한 번 실행해 보세요.\n'
    '· 없으면 크롬을 설치하거나,\n'
    '· 다른 위치에 있다면 환경변수 QA_BROWSER 에 실행파일 경로를 넣고 다시 실행하세요.'
)


def preflight():
    """다른 PC 에서 처음 돌릴 때 막힐 만한 것을 미리 점검한다. 문제 문장 목록을 돌려준다.

    파이썬·node·esbuild 는 점검하지 않는다 - 빌드할 때만 필요하고 exe 에는 이미 들어 있다."""
    problems = []
    name, exe = find_browser()
    if not exe:
        problems.append(NO_BROWSER_MSG)
    try:
        probe = free_port()
        if not probe:
            raise OSError('포트를 얻지 못했습니다')
    except Exception as e:
        problems.append('로컬 통신 포트를 열 수 없습니다(%s).\n'
                        '보안 프로그램이 127.0.0.1 접속을 막고 있는지 확인해 주세요.' % e)
    return problems


# 프록시를 타지 않는 오프너. 사내 프록시가 설정된 PC 에서 urlopen 이 127.0.0.1 까지
# 프록시로 보내 버리면 디버깅 포트 확인이 통째로 실패한다.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Session(object):
    """붙은 타깃 하나. 그 타깃의 최상위 문서 주소를 들고 있어야 콘솔·네트워크를 귀속할 수 있다."""

    def __init__(self, sid, target_id, ttype, parent=None):
        self.sid = sid
        self.target_id = target_id
        self.type = ttype
        # 이 타깃을 물고 온 세션. OOPIF(별도 프로세스 iframe)는 부모를 타고 올라가면
        # 그 탭의 page 세션이 나온다 - '같은 탭' 을 가리기 위한 유일한 단서다.
        self.parent = parent
        self.url = ''
        self.page_key = None        # (주소, 해상도) - 콘솔·네트워크를 붙일 화면
        self.req_urls = {}          # requestId -> url
        self.body_wait = {}         # requestId -> page_key (실패 응답 본문을 기다리는 중)
        self.api_wait = {}          # requestId -> (page_key, url, status) (성공 조회)
        self.contexts = {}          # frameId -> executionContextId (프레임마다 하나)


PROFILE_BUSY_MSG = '\n'.join([
    '이 도구 전용 브라우저가 이미 열려 있는데, 그 창에 다시 붙지 못했습니다.',
    '',
    '열려 있는 그 브라우저 창을 닫고 [QA 시작] 을 다시 눌러 주세요.',
    '(평소 쓰는 브라우저는 닫지 않아도 됩니다 - 전용 프로필이라 서로 무관합니다)',
])


class Launcher(object):
    def __init__(self, inject_js, store, profile_dir, log=None, state_path=None,
                 version=''):
        self.inject_js = inject_js
        self.version = version or ''      # 브10: 화면의 모드 표시에 같이 나간다
        # 브4: 툴바를 최상위 문서에 고정할지. 새 문서에는 주입 앞에 스위치를 얹는다.
        self.force_top = False
        self.store = store
        self.profile_dir = profile_dir
        # 우리가 띄운 포트를 우리 파일에 적어 둔다. 크롬의 DevToolsActivePort 는
        # 나중에 뜬(그리고 기존 창으로 넘어간) 프로세스가 지워 버려 믿을 수 없다(실측).
        self.state_path = state_path or os.path.join(profile_dir, '..', 'session.json')
        self.log = log or (lambda m: None)
        self.proc = None
        self.cdp = None
        self.sessions = {}          # sessionId -> Session
        self.browser_name = None
        self._stop = False
        self._started = False       # CDP 에 붙었는가(기동 중을 '닫힘' 으로 오판하지 않게)
        # ★지난 세션 잔재 정리. localStorage 는 '그 출처의 문서' 가 열려 있어야 지울 수 있어서
        #   연결 직후(about:blank)에는 지울 수 없다. 그래서 화면이 실제로 열릴 때 출처별로 한 번씩 지운다.
        self.clear_stale = False
        self._cleared_origins = set()
        # 탭(최상위 세션)별로 '지금 주석 모드인가'. 셸이 iframe 을 갈아끼우면
        # 새 문서는 항상 꺼진 채로 시작하므로, 붙는 즉시 여기 값으로 맞춰 준다.
        self._tab_mode = {}
        # ★타깃 설정은 반드시 별도 스레드에서 한다.
        #   Target.attachedToTarget 은 CDP 수신 스레드에서 불리는데, 그 안에서 다시
        #   응답을 기다리면 수신이 멈춰 데드락이 된다(실측: Page.enable 응답 시간 초과).
        self._jobs = queue.Queue()
        self._worker = threading.Thread(target=self._work, name='qa-setup')
        self._worker.daemon = True
        self._worker.start()
        # 1.2: 캡처·잘라 붙이기는 한 번에 하나만(우리 UI 를 숨겼다 되살리는 동안 겹치면 안 된다)
        self._busy = threading.Lock()
        # 보강 창이 부른 잘라 붙이기가 끝났을 때 알릴 곳: on_snip(aid, fname)
        self.on_snip = None

    def _work(self):
        while not self._stop:
            try:
                job = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                job()
            except Exception as e:
                self.log('타깃 설정 실패: %s' % e)

    # ── 기동 ──────────────────────────────────────────────────
    def _alive_port(self, port):
        if not port:
            return None
        try:
            with _LOCAL.open('http://127.0.0.1:%d/json/version' % int(port), timeout=2):
                return int(port)
        except Exception:
            return None

    def _save_state(self, port):
        try:
            with open(self.state_path, 'w', encoding='utf-8') as f:
                json.dump({'port': port, 'profile': self.profile_dir}, f)
        except Exception:
            pass

    def _active_port(self):
        """이미 떠 있는 우리 브라우저의 디버깅 포트를 찾는다. 없으면 None.

        ★프로그램을 다시 켰는데 브라우저가 아직 떠 있는 경우가 흔하다. 그때 같은
          프로필로 크롬을 또 띄우면 크롬은 "이미 브라우저 세션에서 열려 있습니다" 라며
          기존 창에 넘기고 스스로 종료한다 → 새 포트가 열리지 않아 시작이 실패한다(실측).
          그래서 먼저 기존 브라우저에 다시 붙어 본다.

        찾는 순서: 우리가 적어 둔 session.json → 크롬의 DevToolsActivePort.
        (DevToolsActivePort 는 넘어간 프로세스가 지워 버리는 경우가 있어 2순위다)"""
        try:
            with open(self.state_path, encoding='utf-8') as f:
                port = self._alive_port(json.load(f).get('port'))
                if port:
                    return port
        except Exception:
            pass
        try:
            with open(os.path.join(self.profile_dir, 'DevToolsActivePort'), encoding='utf-8') as fh:
                return self._alive_port((fh.readline() or '').strip())
        except Exception:
            return None

    def _profile_in_use(self):
        """그 프로필로 브라우저가 떠 있는지 - 크롬이 만드는 잠금 파일로 판단한다."""
        for name in ('lockfile', 'SingletonLock'):
            if os.path.exists(os.path.join(self.profile_dir, name)):
                return True
        return False

    def start(self, url=None):
        name, exe = find_browser()
        if not exe:
            raise RuntimeError(NO_BROWSER_MSG)
        self.browser_name = name
        if not os.path.isdir(self.profile_dir):
            os.makedirs(self.profile_dir)

        # ① 이미 떠 있는 브라우저가 있으면 그것에 다시 붙는다(새로 띄우지 않는다)
        port = self._active_port()
        if port:
            self.log('이미 열려 있는 브라우저에 다시 붙습니다 (포트 %d)' % port)
            self._connect(port)
            if url:
                self.navigate(url)
            return True

        port = free_port()

        # ★전용 --user-data-dir 는 선택이 아니라 필수 조건이다.
        #   크롬 136+ 는 기본 프로필에서 --remote-debugging-port 를 거부한다.
        args = [
            exe,
            '--remote-debugging-port=%d' % port,
            '--user-data-dir=%s' % self.profile_dir,
            '--no-first-run',
            '--no-default-browser-check',
            'about:blank',
        ]
        self.log('%s 실행 (전용 프로필 · 디버깅 포트 %d)' % (name, port))
        creation = 0x08000000 if os.name == 'nt' else 0      # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(args, creationflags=creation)
        self._save_state(port)

        try:
            ws_url = self._wait_ws(port, timeout=30)
        except RuntimeError:
            # ② 기동 직후 프로세스가 사라졌다면 기존 창으로 넘어간 경우다 - 그 창에 붙는다.
            existing = self._active_port()
            if not existing:
                if self._profile_in_use():
                    raise RuntimeError(PROFILE_BUSY_MSG)
                raise
            self.log('기존 브라우저 창으로 넘어갔습니다 - 그 창에 붙습니다 (포트 %d)' % existing)
            self._connect(existing)
            if url:
                self.navigate(url)
            return True
        self._connect_ws(ws_url)
        if url:
            self.navigate(url)
        return True

    def _connect(self, port):
        with _LOCAL.open('http://127.0.0.1:%d/json/version' % port, timeout=5) as r:
            ws_url = json.loads(r.read().decode('utf-8'))['webSocketDebuggerUrl']
        self._connect_ws(ws_url)

    def _connect_ws(self, ws_url):
        self.cdp = CDP(ws_url, on_log=self.log)
        self._started = True
        self._wire_browser()
        self.log('연결됨 - 이제 이 창에서 어떤 주소로 가도 주석 툴바가 자동으로 뜹니다.')

    def _wait_ws(self, port, timeout=30):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                with _LOCAL.open('http://127.0.0.1:%d/json/version' % port, timeout=2) as r:
                    return json.loads(r.read().decode('utf-8'))['webSocketDebuggerUrl']
            except Exception as e:
                last = e
            # 브라우저가 이미 죽었으면 더 기다릴 이유가 없다(기존 창으로 넘어간 경우)
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError('브라우저가 곧바로 종료됐습니다(기존 창으로 넘어감): %s' % last)
            time.sleep(0.25)
        raise RuntimeError('브라우저 디버깅 포트가 열리지 않았습니다: %s' % last)

    # ── 타깃 부착 ──────────────────────────────────────────────
    def _wire_browser(self):
        c = self.cdp
        c.on('Target.attachedToTarget', self._on_attached)
        c.on('Target.detachedFromTarget', self._on_detached)
        c.on('Runtime.bindingCalled', self._on_binding)
        c.on('Runtime.exceptionThrown', self._on_exception)
        c.on('Runtime.consoleAPICalled', self._on_console)
        c.on('Network.responseReceived', self._on_response)
        c.on('Network.loadingFailed', self._on_failed)
        c.on('Network.loadingFinished', self._on_finished)
        c.on('Page.frameNavigated', self._on_navigated)
        c.on('Runtime.executionContextCreated', self._on_context)
        c.on('Runtime.executionContextsCleared', self._on_contexts_cleared)
        c.call('Target.setDiscoverTargets', {'discover': True})
        # wait=False: 이 명령의 응답보다 attachedToTarget 이벤트가 먼저 쏟아지므로
        # 응답을 기다리지 않는다(기다리면 첫 타깃 설정이 뒤로 밀린다).
        c.call('Target.setAutoAttach', {'autoAttach': True,
                                        'waitForDebuggerOnStart': True,
                                        'flatten': True}, wait=False)

    def _on_attached(self, params, parent_sid):
        info = params.get('targetInfo') or {}
        sid = params.get('sessionId')
        ttype = info.get('type')
        waiting = params.get('waitingForDebugger')
        if ttype not in ('page', 'iframe'):
            # 확장·서비스워커·데브툴 타깃은 건너뛴다
            self._jobs.put(lambda: self._resume(sid, waiting))
            return
        s = Session(sid, info.get('targetId'), ttype, parent=parent_sid)
        s.url = info.get('url') or ''
        self.sessions[sid] = s

        def job():
            try:
                self._setup(s)
            finally:
                self._resume(sid, waiting)
        self._jobs.put(job)

    def _resume(self, sid, waiting):
        if not waiting or not sid:
            return
        try:
            self.cdp.call('Runtime.runIfWaitingForDebugger', session_id=sid, wait=False)
        except Exception:
            pass

    def _setup(self, s):
        def step(method, params=None, **kw):
            """★한 명령이 실패해도 나머지는 계속한다.

            예전에는 첫 줄(Page.enable)이 한 번 시간 초과되면 예외가 그대로 올라가서
            그 뒤의 addBinding·addScriptToEvaluateOnNewDocument 까지 통째로 건너뛰었다.
            그 타깃은 다시 시도하는 길이 없어 그 세션 내내 툴바가 뜨지 않는다 -
            셸의 콘텐츠 iframe 이 이렇게 되면 화면에는 최상위 툴바만 남아
            "gnb·lnb 만 잡힌다" 가 된다."""
            try:
                return self.cdp.call(method, params, session_id=s.sid, **kw)
            except Exception as e:
                self.log('%s 실패(%s): %s' % (method, s.type, e))
                return None

        # ★주입에 필요한 둘(바인딩·새 문서 등록)을 먼저 건다. 뒤가 늦어도 툴바는 뜬다.
        step('Runtime.enable')
        step('Runtime.addBinding', {'name': BINDING})
        step('Page.enable')
        step('Page.addScriptToEvaluateOnNewDocument', {'source': self._source()})
        step('Network.enable')
        # 하위 프레임(별도 프로세스로 뜬 iframe)도 같은 처리를 받게 한다
        step('Target.setAutoAttach', {'autoAttach': True,
                                      'waitForDebuggerOnStart': True,
                                      'flatten': True}, wait=False)
        # 이미 문서가 떠 있는 타깃에는 지금 한 번 넣는다(다음 문서부터는 위 등록이 처리).
        # ★프레임마다 넣어야 한다. Runtime.evaluate 를 contextId 없이 부르면 최상위
        #   프레임에만 들어가서, 셸처럼 콘텐츠가 iframe 안에 있는 화면은 "정작 주석을
        #   달아야 하는 영역에 툴바가 없는" 상태가 된다(실측: ww2 /shell 의 dashboard
        #   iframe 에 바인딩은 있는데 마운트 플래그가 없었다).
        # ★단 '이미 들어가 있는지' 를 먼저 본다. 브라우저를 열어 둔 채 프로그램만 다시
        #   켜면(흔한 사용법) 여기서 두 번째 인스턴스가 통째로 들어가, 한 문서에 리스너와
        #   타이머가 두 벌씩 돌았다(실측: 재접속 뒤 __qaInjected=2 · document.__qaDoc=2).
        if s.url and not s.url.startswith('about:'):
            time.sleep(0.4)                     # executionContextCreated 가 도착할 틈
            for ctx in (list(s.contexts.values()) or [None]):
                self._inject_if_missing(s.sid, ctx)

    def _on_context(self, params, sid):
        ctx = params.get('context') or {}
        aux = ctx.get('auxData') or {}
        s = self.sessions.get(sid)
        if s is None or not aux.get('isDefault'):
            return
        fid = aux.get('frameId')
        cid = ctx.get('id')
        if fid:
            s.contexts[fid] = cid
        # ★문서가 생길 때마다 확인해서 없으면 직접 넣는다.
        #   Page.addScriptToEvaluateOnNewDocument 가 iframe 의 실제 문서에는 걸리지
        #   않는 경우가 있다(실측: ww2 /shell 을 새로 띄우면 iframe 주입 1회 = 빈 문서
        #   뿐이고 스타일 0개, 툴바 없음). 그래서 등록에만 의존하지 않는다.
        self._later(0.4, lambda: self._inject_if_missing(sid, cid))

    def _later(self, delay, fn):
        """작업 스레드를 재우지 않고 나중에 시킨다.

        ★예전에는 큐를 잡은 채 time.sleep 을 했다. 새로 붙는 타깃은 디버거를 기다리며
          '멈춘 채' 그 큐를 기다리므로, 프레임이 여럿인 화면에서는 툴바가 그만큼 늦게
          뜨거나 그 사이 컨텍스트가 사라져 주입이 통째로 없어졌다(간헐 증상의 한 갈래)."""
        t = threading.Timer(delay, lambda: self._jobs.put(fn))
        t.daemon = True
        t.start()

    def _inject_if_missing(self, sid, cid, tries=0):
        """그 문서에 스크립트가 없으면 넣는다. 이미 있으면 설정만 다시 얹는다."""
        if sid not in self.sessions:
            return
        base = {'returnByValue': True}
        if cid is not None:
            base['contextId'] = cid
        try:
            # 표식이 있으면 끝. 없더라도 광고·트래킹용 초소형 프레임에는 546KB 를 넣지 않는다.
            # ★'표식이 있다' 와 '살아 있다' 는 다르다. 번들은 표식을 먼저 찍고 본체는
            #   DOM 준비 뒤에 도는데, 그 사이에 죽으면 표식만 남는다(스타일 없는 툴바가
            #   이 경우였다). 문서가 이미 준비됐는데 우리 함수가 없으면 죽은 것이다.
            probe = ('(function(){if(document.__qaDoc){'
                     'return (document.readyState!=="loading"&&'
                     'typeof window.__qaSetMode!=="function")?"broken":"have";}'
                     'if(window.top!==window.self&&(innerWidth<200||innerHeight<200))'
                     'return "tiny";return "need";})()')
            r = self.cdp.call('Runtime.evaluate', dict(base, expression=probe),
                              session_id=sid, timeout=5)
            v = (r.get('result') or {}).get('value')
            if v == 'broken' and tries == 0:
                # 죽은 인스턴스 위에 한 번만 다시 넣는다(반복하면 두 벌이 될 수 있다).
                self.log('주입이 중간에 죽은 문서를 다시 살립니다.')
            elif v == 'have' or v == 'broken':
                # ★두 벌로 만들지 않는다. 브라우저를 열어 둔 채 프로그램만 다시 켜면
                #   여기로 다시 오는데, 예전에는 그때마다 인스턴스가 통째로 하나 더
                #   들어갔다(실측: 재접속 뒤 __qaInjected=2 · document.__qaDoc=2).
                #   설정(툴바 위치·판 번호)만 다시 얹는다.
                self.cdp.call('Runtime.evaluate', dict(base, expression=self._head()),
                              session_id=sid, timeout=5)
                return
            if v == 'tiny':
                # ★영영 건너뛰지 않는다. 셸은 iframe 을 작게(또는 0×0 으로) 만들어 두고
                #   자료가 온 뒤에 키운다 - 그때 다시 보지 않으면 그 문서에는 툴바가
                #   영원히 뜨지 않는다(= 본문을 고를 수 없다).
                #   ★간격을 늘려 가며 25초까지 본다. 느린 조회를 기다렸다 커지는 화면이
                #     있어서 6초로는 짧았다(광고 프레임은 계속 작아서 몇 번 헛보고 끝난다).
                waits = (1.5, 2.5, 4.0, 7.0, 10.0)
                if tries < len(waits):
                    self._later(waits[tries],
                                lambda: self._inject_if_missing(sid, cid, tries + 1))
                return
            self.cdp.call('Runtime.evaluate',
                          {'expression': self._source(), 'awaitPromise': False,
                           'contextId': cid} if cid is not None else
                          {'expression': self._source(), 'awaitPromise': False},
                          session_id=sid, timeout=15)
        except WSError:
            pass                            # 컨텍스트가 이미 사라졌다 - 다음 문서에서 처리

    def _on_contexts_cleared(self, _params, sid):
        s = self.sessions.get(sid)
        if s is not None:
            s.contexts.clear()

    def _on_detached(self, params, _sid):
        self.sessions.pop(params.get('sessionId'), None)

    # ── 이벤트 → 저장소 ────────────────────────────────────────
    def _on_binding(self, params, sid):
        if params.get('name') != BINDING:
            return
        try:
            payload = json.loads(params.get('payload') or '{}')
        except Exception:
            return
        s = self.sessions.get(sid)
        if payload.get('kind') == 'mode':
            self._on_mode(sid, payload.get('output'))
            return
        if payload.get('kind') == 'page':
            # 주석이 아니라 "이 탭은 지금 이 주소를 이 해상도로 본다" 는 통지
            key = self.store.touch_page(payload)
            self.store.merge_unknown(payload.get('url'), payload.get('viewport'))
            if s is not None and key:
                s.page_key = key
            self._maybe_clear_stale(payload.get('url'))
            return
        if payload.get('kind') == 'layout':
            # 브3: 레이아웃 모드에서 옮긴 것. 주석이 아니므로 store.apply 로
            #   보내지 않는다(빈 annotations 로 들어가면 '주석 이벤트' 로 잘못 남는다).
            self.store.set_layout(payload)
            return
        if payload.get('kind') == 'snip':
            # 1.2: 사람이 화면에서 고른 영역. 찍는 것은 여기(CDP) - ★수신 스레드에서 기다리면
            #   안 되므로 작업 큐로 넘긴다.
            ctx = params.get('executionContextId')
            self._jobs.put(lambda: self._do_snip(sid, ctx, payload))
            return
        # 브8: 메모창에 붙인 그림. ★jsonl 에 base64 를 남기지 않는다 - store.apply
        #   전에 떼어내고 store.add_images() 로 파일로만 저장한다.
        images = payload.pop('images', None)
        pages, total = self.store.apply(payload)
        if images:
            anns = payload.get('annotations') or []
            aid = anns[0].get('id') if anns else None
            if aid:
                added = self.store.add_images(aid, images)
                if added:
                    self.log('그림 %d장을 첨부했습니다 - %s' % (len(added), aid))
        if s is not None and payload.get('url'):
            s.page_key = self.store.key_of(payload.get('url'), payload.get('viewport'))
        self.log('주석 %s - 화면 %d개 · 주석 %d건' % (payload.get('kind'), pages, total))

    # ── 주석 모드를 같은 탭의 모든 문서에 맞춘다 ───────────────
    def _root_sid(self, sid):
        """이 세션이 속한 탭(최상위 page 세션)."""
        seen = set()
        while sid and sid in self.sessions:
            if sid in seen:
                break
            seen.add(sid)
            parent = self.sessions[sid].parent
            if not parent or parent not in self.sessions:
                break
            sid = parent
        return sid

    def _on_mode(self, sid, want):
        """화면에서 온 통지. 'ask' 는 갓 붙은 문서가 '이 탭이 지금 어떤가' 를 묻는 것."""
        root = self._root_sid(sid)
        if want == 'ask':
            hit = self._tab_mode.get(root)
            if hit and hit[0]:
                self._broadcast_mode(root, True)
            return
        if want in ('sync-on', 'sync-off'):
            # ★주기 보고. 탭이 기억하는 값과 다르면 그 값으로 되돌린다(어긋남 자동 교정).
            #   여기서 프레임의 값을 채택하지 않는다 - 기준은 사람이 마지막으로 정한 값이다.
            hit = self._tab_mode.get(root)
            if hit is not None and hit[0] != (want == 'sync-on'):
                self._broadcast_mode(root, hit[0])
            return
        on = (want == 'on')
        top = self.sessions.get(root)
        # 어느 주소에서 켠 모드인지 함께 적어 둔다(아래 _on_navigated 가 쓴다).
        self._tab_mode[root] = (on, (top.url if top else '') or '')
        self._broadcast_mode(root, on)

    def _broadcast_mode(self, root, on):
        """★이것이 "gnb·lnb 만 클릭된다" 의 고침이다.

        셸 구조에서 상단바·좌측 메뉴(최상위 문서)와 본문(iframe)은 서로 다른 문서라
        툴바가 둘이고, 교차출처면 서로를 JS 로 부를 수 없어 모드가 따로 논다.
        Esc 를 받은 쪽만 켜지므로, 그때 포커스가 어디 있었느냐에 따라 gnb·lnb 만
        잡히거나 본문만 잡혔다(재현: 셸+교차출처 iframe - 최상위 mode=on,
        iframe mode=off, 본문을 더블클릭해도 메모창이 열리지 않았다).
        교차출처를 넘는 다리는 우리(CDP)뿐이라 프로그램이 전달한다.

        ★같은 탭에만 보낸다. 보고 있지 않은 탭까지 주석 모드가 되면 그 탭은 클릭을
          받지 못한다(모드가 켜진 동안 페이지 클릭을 막는 것이 이 도구의 정책이다).
        ★응답을 기다리지 않는다(wait=False). 이 함수는 CDP 수신 스레드에서 불리므로
          여기서 기다리면 수신이 멈춘다."""
        expr = ('(function(){try{return typeof window.__qaSetMode==="function"'
                '?window.__qaSetMode(%s):null;}catch(e){return null;}})()'
                % ('true' if on else 'false'))
        for s in list(self.sessions.values()):
            if s.type not in ('page', 'iframe'):
                continue
            if self._root_sid(s.sid) != root:
                continue
            for ctx in (list(s.contexts.values()) or [None]):
                params = {'expression': expr}
                if ctx is not None:
                    params['contextId'] = ctx
                try:
                    self.cdp.call('Runtime.evaluate', params, session_id=s.sid, wait=False)
                except Exception:
                    continue

    def _on_navigated(self, params, sid):
        frame = params.get('frame') or {}
        if frame.get('parentId'):
            return                              # 최상위 문서만 화면으로 센다
        # ★'다른 화면으로 옮겼으면' 그 탭의 주석 모드 기억을 버린다. 모드가 켜진 동안
        #   페이지는 클릭을 받지 못하므로, 새 화면이 켜진 채로 뜨면 '먹통' 으로 보인다.
        #   ★단 같은 주소로 다시 부른 것(새로고침)은 버리지 않는다. 세션 유지 때문에
        #     스스로 주기적으로 새로고침하는 셸이 있어서, 버리면 QA 중에 모드가 자꾸
        #     혼자 꺼진다(실측: 11초마다 새로고침하는 셸에서 그 뒤로 계속 꺼진 채였다).
        root = self._root_sid(sid)
        hit = self._tab_mode.get(root)
        if hit and hit[1] != (frame.get('url') or ''):
            self._tab_mode.pop(root, None)
        s = self.sessions.get(sid)
        if s:
            s.url = frame.get('url') or ''
            s.req_urls.clear()
            # 해상도는 아직 모른다. 주입 스크립트의 page 통지가 오면 그 화면으로 합쳐진다.
            s.page_key = self.store.key_of(s.url, None)

    def _page_key(self, sid):
        s = self.sessions.get(sid)
        if s is None:
            return None
        return s.page_key or (self.store.key_of(s.url, None) if s.url else None)

    def _on_exception(self, params, sid):
        d = (params.get('exceptionDetails') or {})
        text = d.get('text') or ''
        ex = d.get('exception') or {}
        desc = ex.get('description') or ex.get('value') or ''
        key = self._page_key(sid)
        if key:
            self.store.add_console(key, ('%s %s' % (text, desc)).strip())

    def _on_console(self, params, sid):
        if params.get('type') != 'error':
            return
        parts = []
        for a in params.get('args') or []:
            parts.append(str(a.get('value', a.get('description', ''))))
        key = self._page_key(sid)
        if key:
            self.store.add_console(key, ' '.join(p for p in parts if p))

    def _on_response(self, params, sid):
        r = params.get('response') or {}
        status = r.get('status') or 0
        s = self.sessions.get(sid)
        rid = params.get('requestId')
        if s is not None:
            s.req_urls[rid] = r.get('url') or ''
        if status < 400:
            # 성공한 조회. 응답이 작을 때만 담는다 - 크기는 loadingFinished 에서 안다.
            if (200 <= status < 300 and params.get('type') in API_TYPES
                    and not _is_noise(r.get('url')) and s is not None and rid):
                if len(s.api_wait) > 200:
                    s.api_wait.clear()
                key = self._page_key(sid)
                if key:
                    s.api_wait[rid] = (key, r.get('url') or '', status)
            return
        if _is_noise(r.get('url')):
            return
        key = self._page_key(sid)
        if key:
            self.store.add_network(key, r.get('url') or '', status,
                                   r.get('statusText') or '', token=rid)
            # 본문은 아직 오지 않았다. loadingFinished 를 기다렸다가 붙인다.
            if s is not None and rid:
                if len(s.body_wait) > 200:      # 끝나지 않은 요청이 쌓이면 버린다
                    s.body_wait.clear()
                s.body_wait[rid] = key

    def _on_finished(self, params, sid):
        """응답 본문을 받아 기록에 붙인다 - 실패한 요청, 그리고 짧은 성공 조회.

        ★getResponseBody 는 응답을 기다리는 호출이다. CDP 수신 스레드에서 그대로 부르면
          수신이 멈춰 데드락이 된다(_work 주석 참조). 반드시 작업 큐로 넘긴다.
        ★성공 조회는 여기서 크기를 보고 거른다 - encodedDataLength 는 이 이벤트에만 있다."""
        s = self.sessions.get(sid)
        if s is None:
            return
        rid = params.get('requestId')
        key = s.body_wait.pop(rid, None)
        if key is not None:
            self._jobs.put(lambda: self._attach_body(sid, rid, key))
            return
        hit = s.api_wait.pop(rid, None)
        if hit is None:
            return
        size = params.get('encodedDataLength') or 0
        try:
            small = float(size) <= API_BODY_MAX
        except Exception:
            small = False
        if small:
            self._jobs.put(lambda: self._attach_api(sid, rid, hit))

    def _attach_api(self, sid, rid, hit):
        key, url, status = hit
        body = _tidy_body(self._body_of(sid, rid))
        if body:
            self.store.add_api(key, url, status, body)

    def _body_of(self, sid, rid):
        try:
            res = self.cdp.call('Network.getResponseBody', {'requestId': rid},
                                session_id=sid, timeout=5) or {}
        except Exception:
            return ''                           # 본문이 이미 버려졌다 - 없는 대로 둔다
        if res.get('base64Encoded'):
            return ''                           # 이진 응답은 읽어도 쓸모가 없다
        return res.get('body') or ''

    def _attach_body(self, sid, rid, key):
        body = _tidy_body(self._body_of(sid, rid))
        if body:
            self.store.set_network_body(key, rid, body)

    def _on_failed(self, params, sid):
        s = self.sessions.get(sid)
        if s is not None:
            s.body_wait.pop(params.get('requestId'), None)
            s.api_wait.pop(params.get('requestId'), None)
        req = (s.req_urls.pop(params.get('requestId'), '') if s else '')
        key = self._page_key(sid)
        if _is_noise(req):
            return
        if key and params.get('type') in ('Document', 'XHR', 'Fetch', 'Script', 'Stylesheet'):
            self.store.add_network(key, req or '(주소 미상)', '실패',
                                   params.get('errorText') or '')

    # ── 조작 ──────────────────────────────────────────────────
    def _wait_page(self, timeout=15.0):
        """첫 탭이 붙기를 기다린다.

        타깃 부착은 비동기(작업 스레드)라서, 기동 직후 바로 이동을 시도하면
        "열린 탭을 찾지 못했습니다" 가 난다 - [QA 시작] 을 누르는 바로 그 경로다."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for s in list(self.sessions.values()):
                if s.type == 'page':
                    return s
            time.sleep(0.15)
        return None

    def navigate(self, url):
        """첫 탭을 주어진 주소로 보낸다."""
        low = url.lower()
        if low.startswith(('chrome://', 'edge://', 'devtools://', 'view-source:')):
            raise RuntimeError('브라우저 내부 페이지에는 주석을 달 수 없습니다.'
                               '검사할 사이트 주소를 넣어 주세요.')
        if not url.startswith(('http://', 'https://', 'file://', 'about:')):
            url = 'http://' + url
        s = self._wait_page()
        if s is None:
            raise RuntimeError('브라우저 탭이 준비되지 않았습니다. 잠시 후 다시 시도해 주세요.')
        self.cdp.call('Page.navigate', {'url': url}, session_id=s.sid)
        return url

    def _maybe_clear_stale(self, url):
        """프로그램 목록이 비어 있다면, 브라우저에 남은 지난 세션 주석도 지운다.

        ★사용자가 겪은 것: "실행기 목록에는 없는데 브라우저를 띄우면 마커와 주석이 남아 있음".
          프로그램이 이미 추출/비우기 한 것이므로 브라우저에도 남아 있을 이유가 없다.
          출처(origin)별로 한 번씩만 한다 - 매번 지우면 방금 남긴 주석까지 날아간다."""
        if not self.clear_stale or not url:
            return
        try:
            from urllib.parse import urlparse
            u = urlparse(url)
            origin = '%s://%s' % (u.scheme, u.netloc)
        except Exception:
            return
        if not u.scheme.startswith('http') or origin in self._cleared_origins:
            return
        self._cleared_origins.add(origin)

        def job():
            n = self.clear_browser_annotations()
            if n:
                self.log('%s 에 남아 있던 지난 주석 %d건을 지웠습니다.' % (origin, n))
        self._jobs.put(job)

    def clear_browser_annotations(self):
        """브라우저에 남은 주석 이력까지 비운다. 지운 키 수를 돌려준다.

        ★이력이 두 곳에 있다는 것이 사용자가 "왜 계속 이력이 남아 있지" 라고 물은
          원인이다(프로그램 파일은 비웠는데 하단 툴바에 옛 주석이 계속 보였다).
          [비우기]/[추출] 은 한 번에 양쪽을 비워야 한다."""
        total = 0
        for s in list(self.sessions.values()):
            if s.type not in ('page', 'iframe'):
                continue
            ctxs = list(s.contexts.values()) or [None]
            for ctx in ctxs:
                params = {'expression': 'typeof window.__qaClear==="function"?window.__qaClear():0',
                          'returnByValue': True}
                if ctx is not None:
                    params['contextId'] = ctx
                try:
                    r = self.cdp.call('Runtime.evaluate', params, session_id=s.sid, timeout=8)
                    v = (r.get('result') or {}).get('value')
                    if isinstance(v, (int, float)):
                        total += int(v)
                except Exception:
                    continue
        return total

    def rearrange_count(self):
        """레이아웃 모드에서 바꾼 개수를 센다(브9).

        agentation 은 배치 변경을 `agentation-rearrange-<경로>` 로 저장한다.
        주석 모드에 있으면 그 개수가 화면에 안 보여서 "내가 몇 개 바꿨지" 를 알 수 없다는
        실사용 보고가 있었다. 프로그램 창이 대신 보여 준다."""
        expr = ('(function(){var n=0;try{'
                'for(var i=0;i<localStorage.length;i++){var k=localStorage.key(i);'
                'if(!k||k.indexOf("agentation-rearrange-")!==0)continue;'
                'var v=null;try{v=JSON.parse(localStorage.getItem(k));}catch(e){}'
                'if(Array.isArray(v))n+=v.length;'
                'else if(v&&typeof v==="object")n+=Object.keys(v).length;'
                'else n+=1;}}catch(e){}return n;})()')
        total = 0
        for s in list(self.sessions.values()):
            if s.type not in ('page', 'iframe'):
                continue
            ctxs = list(s.contexts.values()) or [None]
            for ctx in ctxs:
                params = {'expression': expr, 'returnByValue': True}
                if ctx is not None:
                    params['contextId'] = ctx
                try:
                    r = self.cdp.call('Runtime.evaluate', params, session_id=s.sid, timeout=5)
                    v = (r.get('result') or {}).get('value')
                    if isinstance(v, (int, float)):
                        total += int(v)
                        break            # 한 타깃에서 한 번만 센다(프레임마다 같은 저장소)
                except Exception:
                    continue
        return total

    # ── 1.2: 화면 단위 캡처 · 영역 잘라 붙이기 ─────────────────────────
    # ★실측(크롬 151): Page.captureScreenshot 의 clip 은 '문서 좌표' 다(뷰포트가 아니다).
    #   captureBeyondViewport 전체 캡처에서도 position:fixed 는 현재 스크롤 위치에 찍힌다 -
    #   그래서 전체 캡처는 문서를 맨 위로 올린 뒤 찍는다(inject 의 begin()).
    TILE_BASE = 15000           # 한 장의 최대 픽셀 높이(GPU 텍스처 상한 16384 아래). CSS 높이는 /dpr
    MAX_TILES = 6

    def _eval(self, sid, ctx, expr, timeout=8):
        """그 문서(컨텍스트)에서 식을 평가해 값을 돌려준다. 예외는 RuntimeError."""
        params = {'expression': expr, 'returnByValue': True, 'awaitPromise': True}
        if ctx is not None:
            params['contextId'] = ctx
        r = self.cdp.call('Runtime.evaluate', params, session_id=sid, timeout=timeout) or {}
        if r.get('exceptionDetails'):
            d = r['exceptionDetails']
            raise RuntimeError((d.get('exception') or {}).get('description') or d.get('text') or 'JS 오류')
        return (r.get('result') or {}).get('value')

    def find_documents(self):
        """떠 있는 모든 문서의 위치 정보. [{sid, ctx, frame_id, root, url, vp, top, dpr, sw, sh, offset}]"""
        out = []
        probe = ('(function(){try{return window.__qaCapture?window.__qaCapture.where():null;}'
                 'catch(e){return null;}})()')
        for s in list(self.sessions.values()):
            if s.type not in ('page', 'iframe'):
                continue
            for fid, ctx in (list(s.contexts.items()) or [(None, None)]):
                try:
                    w = self._eval(s.sid, ctx, probe, timeout=5)
                except Exception:
                    continue
                if not w:
                    continue
                w.update({'sid': s.sid, 'ctx': ctx, 'frame_id': fid, 'root': self._root_sid(s.sid)})
                out.append(w)
        return out

    def find_document(self, url, viewport):
        """(문서, 못 찾은 이유). 화면 키 (주소, 해상도) 와 정확히 맞는 문서를 찾는다."""
        same_url = []
        for d in self.find_documents():
            if d.get('url') != url:
                continue
            if not viewport or viewport == '?' or d.get('vp') == viewport:
                return d, ''
            same_url.append(d.get('vp'))
        if same_url:
            return None, u'같은 주소가 다른 해상도(%s)로 열려 있습니다' % u'·'.join(same_url)
        return None, u'그 화면이 열려 있지 않습니다'

    def _shot(self, sid, x, y, w, h, beyond=True):
        """PNG 바이트. clip 은 문서 좌표(CSS px), scale 1 = 기기 픽셀."""
        params = {'format': 'png', 'fromSurface': True, 'captureBeyondViewport': bool(beyond),
                  'clip': {'x': float(x), 'y': float(y), 'width': float(w), 'height': float(h),
                           'scale': 1}}
        r = self.cdp.call('Page.captureScreenshot', params, session_id=sid, timeout=90) or {}
        if not r.get('data'):
            raise RuntimeError('캡처 응답이 비었습니다')
        return base64.b64decode(r['data'])

    def _frame_owner(self, sid, ctx):
        """(소유 문서의 세션, <iframe> 요소 objectId). 교차출처 iframe 의 요소를 부모 문서에서 잡는다.

        ★두 경우가 있다. OOPIF(별도 프로세스)면 부모 '세션' 에서, 같은 프로세스의 교차출처
          프레임(file:// 끼리 · 사이트 격리가 꺼진 경우)이면 **같은 세션** 의 DOM 에서 찾는다 -
          한 세션이 프레임 트리 전체를 갖고 있기 때문이다."""
        s = self.sessions.get(sid)
        if s is None:
            return None, None
        fids = [f for f, c in s.contexts.items() if c == ctx] or list(s.contexts.keys())
        if not fids:
            return None, None
        cands = []
        if s.parent and s.parent in self.sessions:
            cands.append(s.parent)
        cands.append(sid)
        for osid in cands:
            try:
                owner = self.cdp.call('DOM.getFrameOwner', {'frameId': fids[0]},
                                      session_id=osid, timeout=8)
                node = self.cdp.call('DOM.resolveNode', {'backendNodeId': owner['backendNodeId']},
                                     session_id=osid, timeout=8)
                oid = (node.get('object') or {}).get('objectId')
                if oid:
                    return osid, oid
            except Exception:
                continue
        return None, None

    # <iframe> 요소의 자리(그 요소가 사는 문서의 뷰포트 기준) + 같은 출처 조상 프레임 오프셋까지
    _OWNER_POS_JS = ('function(){var r=this.getBoundingClientRect();'
                     'var x=r.left+(this.clientLeft||0),y=r.top+(this.clientTop||0);'
                     'try{var w=this.ownerDocument.defaultView;'
                     'while(w&&w!==w.parent){var fe=w.frameElement;if(!fe)break;'
                     'var rr=fe.getBoundingClientRect();x+=rr.left+(fe.clientLeft||0);'
                     'y+=rr.top+(fe.clientTop||0);w=w.parent;}}catch(e){}'
                     'return [x,y];}')

    def _frame_offset_oopif(self, sid, ctx):
        """교차출처 iframe 문서의 뷰포트 좌표를 최상위 뷰포트 좌표로 옮기는 오프셋. 못 구하면 None."""
        x = y = 0.0
        cur, cctx = sid, ctx
        for _ in range(4):                          # 중첩은 몇 단만
            psid, oid = self._frame_owner(cur, cctx)
            if not oid:
                return None if (cur == sid) else {'x': x, 'y': y}
            try:
                r = self.cdp.call('Runtime.callFunctionOn', {
                    'objectId': oid, 'returnByValue': True,
                    'functionDeclaration': self._OWNER_POS_JS}, session_id=psid, timeout=8)
                v = (r.get('result') or {}).get('value') or [0, 0]
                x += float(v[0])
                y += float(v[1])
            except Exception:
                return None
            if psid == cur:
                break                               # 같은 세션 안에서 찾았다 - 최상위까지 왔다
            cur, cctx = psid, None
            if not (self.sessions.get(cur) and self.sessions[cur].parent):
                break
        return {'x': x, 'y': y}

    def _expand_oopif(self, sid, ctx, height):
        """교차출처 iframe 의 높이를 부모 문서에서 늘린다. 되돌릴 정보 또는 None."""
        psid, oid = self._frame_owner(sid, ctx)
        if not oid:
            return None
        try:
            r = self.cdp.call('Runtime.callFunctionOn', {
                'objectId': oid, 'returnByValue': True, 'arguments': [{'value': int(height)}],
                'functionDeclaration': 'function(h){var o={h:this.style.height,mx:this.style.maxHeight,'
                                       'mn:this.style.minHeight};this.style.height=h+"px";'
                                       'this.style.maxHeight="none";this.style.minHeight="0";return o;}'},
                session_id=psid, timeout=8)
            return {'sid': psid, 'oid': oid, 'saved': (r.get('result') or {}).get('value') or {}}
        except Exception:
            return None

    def _restore_oopif(self, info):
        try:
            self.cdp.call('Runtime.callFunctionOn', {
                'objectId': info['oid'], 'arguments': [{'value': info['saved']}],
                'functionDeclaration': 'function(o){this.style.height=o.h||"";'
                                       'this.style.maxHeight=o.mx||"";this.style.minHeight=o.mn||"";}'},
                session_id=info['sid'], timeout=8)
        except Exception:
            pass

    @staticmethod
    def capture_basename(screen_no, url, viewport):
        """'02_admin-content-list_1920x1080' - 파일만 봐도 어느 화면인지 읽히게."""
        try:
            from urllib.parse import urlsplit
            path = urlsplit(url or '').path or '/'
        except Exception:
            path = '/'
        slug = re.sub(r'[^A-Za-z0-9가-힣]+', '-', path).strip('-')[:40] or 'root'
        return '%02d_%s_%s' % (int(screen_no), slug, (viewport or '?').replace('x', 'x'))

    def capture_screen(self, target, dest_dir):
        """화면 하나를 통째로 찍는다(핀 오버레이 포함). 화면당 1벌.

        target = {key, screen, url, viewport, pins}.
        돌려주는 값: {'ok': True, files, partial, reason, css_w, css_h, dpr} 또는 {'ok': False, reason}.
        ★Store.lock 을 잡지 않은 채 부른다(CDP 응답을 기다린다). 결과 저장은 부르는 쪽이."""
        doc, why = self.find_document(target['url'], target['viewport'])
        if not doc:
            return {'ok': False, 'reason': why}
        sid, ctx, root = doc['sid'], doc['ctx'], doc['root']
        if not os.path.isdir(dest_dir):
            os.makedirs(dest_dir)
        base = self.capture_basename(target.get('screen') or 0, target['url'], target['viewport'])
        pins_js = json.dumps(target.get('pins') or {}, ensure_ascii=False)
        with self._busy:
            begun = expanded = False
            oopif = None
            root_scroll = None
            try:
                info = self._eval(sid, ctx, 'window.__qaCapture.begin(%s)' % pins_js, timeout=15) or {}
                begun = True
                partial, reason = False, u''
                if not doc.get('top'):
                    # iframe 화면: 프레임을 문서 높이만큼 늘려야 아래쪽까지 찍힌다.
                    if self._eval(sid, ctx, 'window.__qaCapture.expand(%d)' % int(info.get('sh') or 0),
                                  timeout=8):
                        expanded = True
                    else:
                        oopif = self._expand_oopif(sid, ctx, info.get('sh') or 0)
                        if not oopif:
                            partial, reason = True, u'iframe 확장 불가'
                    # 최상위 문서도 맨 위로(고정 요소가 현재 스크롤 자리에 찍힌다 - 위 실측)
                    try:
                        root_scroll = self._eval(root, None, '(function(){var s=[scrollX,scrollY];'
                                                             'scrollTo(0,0);return s;})()', timeout=5)
                    except Exception:
                        root_scroll = None
                    time.sleep(0.35)                  # 레이아웃 반영
                else:
                    time.sleep(0.15)
                metrics = self.cdp.call('Page.getLayoutMetrics', session_id=root, timeout=10) or {}
                cs = metrics.get('cssContentSize') or metrics.get('contentSize') or {}
                css_w = int(math.ceil(cs.get('width') or info.get('sw') or 0))
                css_h = int(math.ceil(cs.get('height') or info.get('sh') or 0))
                if doc.get('top') and info.get('sh'):
                    css_h = max(css_h, int(info['sh']))
                dpr = float(info.get('dpr') or doc.get('dpr') or 1)
                tile = max(1000, int(self.TILE_BASE // dpr))
                n = max(1, int(math.ceil(css_h / float(tile))))
                if n > self.MAX_TILES:
                    n = self.MAX_TILES
                    partial = True
                    reason = (reason + u' · ' if reason else u'') + u'페이지가 너무 길어 %d장까지만' % n
                files = []
                y = 0
                for i in range(n):
                    h = min(tile, css_h - y)
                    if h <= 0:
                        break
                    try:
                        png = self._shot(root, 0, y, css_w, h)
                        parts = [(png, h)]
                    except Exception:
                        # 한 번만 절반으로 나눠 다시 찍는다(텍스처 상한·메모리)
                        half = int(math.ceil(h / 2.0))
                        parts = [(self._shot(root, 0, y, css_w, half), half),
                                 (self._shot(root, 0, y + half, css_w, h - half), h - half)]
                    for png, _ph in parts:
                        k = len(files) + 1
                        fn = '%s-%d.png' % (base, k) if (n > 1 or len(parts) > 1) else '%s.png' % base
                        with open(os.path.join(dest_dir, fn), 'wb') as f:
                            f.write(png)
                        files.append(fn)
                    y += h
                # 옛 타일이 남지 않게(장수가 줄어든 경우)
                for fn in os.listdir(dest_dir):
                    if fn.startswith(base) and fn not in files and fn.endswith('.png'):
                        try:
                            os.remove(os.path.join(dest_dir, fn))
                        except Exception:
                            pass
                return {'ok': True, 'files': files, 'partial': partial, 'reason': reason,
                        'css_w': css_w, 'css_h': css_h, 'dpr': dpr,
                        'pins': dict((aid, p.get('label')) for aid, p in (target.get('pins') or {}).items())}
            finally:
                if begun:
                    try:
                        self._eval(sid, ctx, 'window.__qaCapture.end()', timeout=8)
                    except Exception:
                        pass
                if expanded:
                    try:
                        self._eval(sid, ctx, 'window.__qaCapture.restore()', timeout=8)
                    except Exception:
                        pass
                if oopif:
                    self._restore_oopif(oopif)
                if root_scroll:
                    try:
                        self._eval(root, None, 'scrollTo(%d,%d)' % (int(root_scroll[0]), int(root_scroll[1])),
                                   timeout=5)
                    except Exception:
                        pass

    def request_snip(self, aid, url, viewport, path='', box=None, fixed=False):
        """보강 창에서 부른 '화면에서 잘라 붙이기'. 그 화면이 열려 있으면 선택 오버레이를 띄운다.
        (시작 여부, 이유). 결과는 나중에 binding('snip') 으로 온다."""
        doc, why = self.find_document(url, viewport)
        if not doc:
            return False, why
        opts = {'target': aid, 'path': path or '', 'fixed': bool(fixed)}
        if isinstance(box, dict):
            opts['box'] = box
        token = 'a%d' % int(time.time() * 1000)
        try:
            ok = self._eval(doc['sid'], doc['ctx'],
                            'window.__qaSnip(%s, %s)' % (json.dumps(token), json.dumps(opts, ensure_ascii=False)),
                            timeout=8)
        except Exception as e:
            return False, str(e)
        return bool(ok), u'' if ok else u'그 문서에서 선택을 시작하지 못했습니다'

    def _do_snip(self, sid, ctx, payload):
        """binding('snip') - 사람이 고른 영역을 그 탭의 최상위 세션에서 찍는다(작업 스레드)."""
        token = payload.get('token') or ''
        target = payload.get('target') or 'popup'
        rect = payload.get('rect') or {}
        root = self._root_sid(sid)

        def done(ok, data_url=None):
            try:
                self._eval(sid, ctx, 'window.__qaSnipDone(%s, %s, %s)'
                           % (json.dumps(token), 'true' if ok else 'false',
                              json.dumps(data_url) if data_url else 'null'), timeout=15)
            except Exception:
                pass

        try:
            with self._busy:
                off = payload.get('offset')
                if off is None and not payload.get('top'):
                    off = self._frame_offset_oopif(sid, ctx)
                    if off is None:
                        self.log('잘라 붙이기: iframe 위치를 구하지 못해 그대로 찍습니다.')
                        off = {'x': 0, 'y': 0}
                elif off is None:
                    off = {'x': 0, 'y': 0}
                # 뷰포트 좌표 → 문서 좌표(최상위 스크롤을 더한다). clip 은 문서 좌표다(실측).
                metrics = self.cdp.call('Page.getLayoutMetrics', session_id=root, timeout=10) or {}
                lv = metrics.get('cssLayoutViewport') or {}
                px = float(lv.get('pageX') or 0)
                py = float(lv.get('pageY') or 0)
                x = float(rect.get('x') or 0) + float(off.get('x') or 0) + px
                y = float(rect.get('y') or 0) + float(off.get('y') or 0) + py
                w = max(1.0, float(rect.get('w') or 0))
                h = max(1.0, float(rect.get('h') or 0))
                png = self._shot(root, x, y, w, h, beyond=False)
        except Exception as e:
            self.log('잘라 붙이기 실패: %s' % e)
            done(False)
            return
        if target == 'popup':
            done(True, 'data:image/png;base64,' + base64.b64encode(png).decode('ascii'))
            self.log('화면 일부를 잘라 메모창에 붙였습니다 (%dx%d).' % (int(w), int(h)))
            return
        try:
            fname = self.store.add_image_bytes(target, png, '.png')
        except Exception as e:
            self.log('잘라 붙이기 저장 실패: %s' % e)
            done(False)
            return
        done(True)
        self.log('화면 일부를 잘라 첨부했습니다 - %s (%dx%d)' % (fname, int(w), int(h)))
        if self.on_snip:
            try:
                self.on_snip(target, fname)
            except Exception:
                pass

    def _head(self):
        """주입 앞에 얹는 설정 스위치. 이미 주입된 문서에는 이것만 다시 얹는다."""
        head = 'window.__qaForceTop=%s;\n' % ('true' if self.force_top else 'false')
        # 브10: 어느 판으로 띄운 화면인지 툴바에서 바로 읽히게 한다.
        #   ★같은 이름으로 exe 를 덮어써서 "이전에는 됐는데" 를 재현조차 못 한 일이 있었다.
        head += 'window.__qaVer=%s;\n' % json.dumps(self.version)
        return head

    def _source(self):
        """주입할 소스 = 설정 스위치 + 번들."""
        return self._head() + self.inject_js

    def set_force_top(self, on):
        """툴바 위치 설정을 지금 떠 있는 문서에도 적용한다(브4).

        ★__qaClear 를 쓰지 않는다. 그것은 저장된 주석까지 지운다 - 설정을 바꾸다가
          작업을 잃으면 안 된다. 그래서 __qaRemount 를 따로 뒀다."""
        self.force_top = bool(on)
        expr = ('(function(){window.__qaForceTop=%s;'
                'return typeof window.__qaRemount==="function"?window.__qaRemount():null;})()'
                % ('true' if self.force_top else 'false'))
        touched = 0
        for s in list(self.sessions.values()):
            if s.type not in ('page', 'iframe'):
                continue
            ctxs = list(s.contexts.values()) or [None]
            for ctx in ctxs:
                params = {'expression': expr, 'returnByValue': True}
                if ctx is not None:
                    params['contextId'] = ctx
                try:
                    self.cdp.call('Runtime.evaluate', params, session_id=s.sid, timeout=8)
                    touched += 1
                except Exception:
                    continue
        return touched

    def check_injected(self):
        """툴바가 뜬 탭 수. 사람이 눈으로 "떴나?" 확인하지 않게 프로그램이 스스로 본다.

        ★프레임 전체를 봐야 한다. 셸 구조에서는 툴바가 최상위 문서가 아니라 콘텐츠
          iframe 에 뜬다(그 문서를 지적하려는 것이므로 의도된 동작이다). 최상위만
          보면 정상인데도 배지가 "주입 대기" 로 남아 사용자가 고장으로 읽는다(실측)."""
        # ★"호스트 div 가 있다" 로는 부족하다. 스타일이 안 들어간 문서에서는 요소만
        #   렌더되고 position:static 으로 문서 맨 아래에 깔려 보이지 않는데도 초록불이
        #   떴다(실측). 그래서 툴바가 실제로 고정 배치돼 있는지까지 본다.
        expr = ('(function(){'
                'if(!document.getElementById("%s"))return false;'
                'var els=document.querySelectorAll("[class*=toolbar]");'
                'for(var i=0;i<els.length;i++){'
                'var s=getComputedStyle(els[i]);'
                'if(s.position==="fixed"&&s.visibility!=="hidden"&&s.display!=="none")return true;}'
                'return false;})()' % HOST_ID)
        ok = 0
        for s in list(self.sessions.values()):
            if s.type != 'page':
                continue
            found = False
            ctxs = list(s.contexts.values()) or [None]
            for ctx in ctxs:
                params = {'expression': expr, 'returnByValue': True}
                if ctx is not None:
                    params['contextId'] = ctx
                try:
                    r = self.cdp.call('Runtime.evaluate', params,
                                      session_id=s.sid, timeout=5)
                    if (r.get('result') or {}).get('value') is True:
                        found = True
                        break
                except Exception:
                    continue
            if found:
                ok += 1
        return ok

    def alive(self):
        """우리가 띄운 프로세스가 아니라 CDP 연결이 기준이다.
        기존 창에 다시 붙은 경우에는 self.proc 이 없다.

        ★기동 중(아직 CDP 에 붙기 전)은 살아 있는 것으로 본다. 그러지 않으면 창의
          주기 갱신이 곧바로 '브라우저 닫힘' 으로 판단해 기동을 취소해 버린다(실측)."""
        if not self._started:
            return True
        if self.cdp is None or self.cdp.closed:
            return False
        if self.proc is not None:
            return self.proc.poll() is None
        return True

    def stop(self):
        self._stop = True
        if self.cdp:
            try:
                self.cdp.close()
            except Exception:
                pass
        self.cdp = None
