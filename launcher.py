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
import json
import os
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

    def __init__(self, sid, target_id, ttype):
        self.sid = sid
        self.target_id = target_id
        self.type = ttype
        self.url = ''
        self.page_key = None        # (주소, 해상도) - 콘솔·네트워크를 붙일 화면
        self.req_urls = {}          # requestId -> url
        self.contexts = {}          # frameId -> executionContextId (프레임마다 하나)


PROFILE_BUSY_MSG = '\n'.join([
    '이 도구 전용 브라우저가 이미 열려 있는데, 그 창에 다시 붙지 못했습니다.',
    '',
    '열려 있는 그 브라우저 창을 닫고 [QA 시작] 을 다시 눌러 주세요.',
    '(평소 쓰는 브라우저는 닫지 않아도 됩니다 - 전용 프로필이라 서로 무관합니다)',
])


class Launcher(object):
    def __init__(self, inject_js, store, profile_dir, log=None, state_path=None):
        self.inject_js = inject_js
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
        # ★타깃 설정은 반드시 별도 스레드에서 한다.
        #   Target.attachedToTarget 은 CDP 수신 스레드에서 불리는데, 그 안에서 다시
        #   응답을 기다리면 수신이 멈춰 데드락이 된다(실측: Page.enable 응답 시간 초과).
        self._jobs = queue.Queue()
        self._worker = threading.Thread(target=self._work, name='qa-setup')
        self._worker.daemon = True
        self._worker.start()

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
        c.on('Page.frameNavigated', self._on_navigated)
        c.on('Runtime.executionContextCreated', self._on_context)
        c.on('Runtime.executionContextsCleared', self._on_contexts_cleared)
        c.call('Target.setDiscoverTargets', {'discover': True})
        # wait=False: 이 명령의 응답보다 attachedToTarget 이벤트가 먼저 쏟아지므로
        # 응답을 기다리지 않는다(기다리면 첫 타깃 설정이 뒤로 밀린다).
        c.call('Target.setAutoAttach', {'autoAttach': True,
                                        'waitForDebuggerOnStart': True,
                                        'flatten': True}, wait=False)

    def _on_attached(self, params, _sid):
        info = params.get('targetInfo') or {}
        sid = params.get('sessionId')
        ttype = info.get('type')
        waiting = params.get('waitingForDebugger')
        if ttype not in ('page', 'iframe'):
            # 확장·서비스워커·데브툴 타깃은 건너뛴다
            self._jobs.put(lambda: self._resume(sid, waiting))
            return
        s = Session(sid, info.get('targetId'), ttype)
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
        c = self.cdp
        c.call('Page.enable', session_id=s.sid)
        c.call('Runtime.enable', session_id=s.sid)
        c.call('Network.enable', session_id=s.sid)
        c.call('Runtime.addBinding', {'name': BINDING}, session_id=s.sid)
        c.call('Page.addScriptToEvaluateOnNewDocument',
               {'source': self.inject_js}, session_id=s.sid)
        # 하위 프레임(별도 프로세스로 뜬 iframe)도 같은 처리를 받게 한다
        c.call('Target.setAutoAttach', {'autoAttach': True,
                                        'waitForDebuggerOnStart': True,
                                        'flatten': True}, session_id=s.sid, wait=False)
        # 이미 문서가 떠 있는 타깃에는 지금 한 번 넣는다(다음 문서부터는 위 등록이 처리).
        # ★프레임마다 넣어야 한다. Runtime.evaluate 를 contextId 없이 부르면 최상위
        #   프레임에만 들어가서, 셸처럼 콘텐츠가 iframe 안에 있는 화면은 "정작 주석을
        #   달아야 하는 영역에 툴바가 없는" 상태가 된다(실측: ww2 /shell 의 dashboard
        #   iframe 에 바인딩은 있는데 마운트 플래그가 없었다).
        if s.url and not s.url.startswith('about:'):
            time.sleep(0.4)                     # executionContextCreated 가 도착할 틈
            targets = list(s.contexts.values()) or [None]
            for ctx in targets:
                params = {'expression': self.inject_js, 'awaitPromise': False}
                if ctx is not None:
                    params['contextId'] = ctx
                try:
                    c.call('Runtime.evaluate', params, session_id=s.sid)
                except WSError:
                    pass

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
        #   두 번 들어가도 해가 없다 - 두 번째 인스턴스는 이미 뜬 툴바를 보고 물러난다.
        self._jobs.put(lambda: self._ensure_injected(sid, cid))

    def _ensure_injected(self, sid, cid, delay=0.4):
        time.sleep(delay)                   # 등록된 스크립트가 먼저 돌 틈을 준다
        if sid not in self.sessions:
            return
        try:
            # 표식이 있으면 끝. 없더라도 광고·트래킹용 초소형 프레임에는 546KB 를 넣지 않는다.
            probe = ('(function(){if(document.__qaDoc)return "have";'
                     'if(window.top!==window.self&&(innerWidth<200||innerHeight<200))'
                     'return "tiny";return "need";})()')
            r = self.cdp.call('Runtime.evaluate',
                              {'expression': probe, 'returnByValue': True,
                               'contextId': cid}, session_id=sid, timeout=5)
            if (r.get('result') or {}).get('value') != 'need':
                return
            self.cdp.call('Runtime.evaluate',
                          {'expression': self.inject_js, 'awaitPromise': False,
                           'contextId': cid}, session_id=sid, timeout=15)
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
        if payload.get('kind') == 'page':
            # 주석이 아니라 "이 탭은 지금 이 주소를 이 해상도로 본다" 는 통지
            key = self.store.touch_page(payload)
            self.store.merge_unknown(payload.get('url'), payload.get('viewport'))
            if s is not None and key:
                s.page_key = key
            self._maybe_clear_stale(payload.get('url'))
            return
        pages, total = self.store.apply(payload)
        if s is not None and payload.get('url'):
            s.page_key = self.store.key_of(payload.get('url'), payload.get('viewport'))
        self.log('주석 %s - 화면 %d개 · 주석 %d건' % (payload.get('kind'), pages, total))

    def _on_navigated(self, params, sid):
        frame = params.get('frame') or {}
        if frame.get('parentId'):
            return                              # 최상위 문서만 화면으로 센다
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
        if s is not None:
            s.req_urls[params.get('requestId')] = r.get('url') or ''
        if status < 400 or _is_noise(r.get('url')):
            return
        key = self._page_key(sid)
        if key:
            self.store.add_network(key, r.get('url') or '', status,
                                   r.get('statusText') or '')

    def _on_failed(self, params, sid):
        s = self.sessions.get(sid)
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
