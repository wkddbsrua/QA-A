# -*- coding: utf-8 -*-
"""화면 주석 QA - 프로그램 창.

쓰는 법은 두 단계다.
  1. 주소를 붙여넣고 [QA 시작]  → 브라우저가 열린다
  2. 화면을 클릭해 메모를 남긴다 → 이 창에 화면별로 쌓인다. 끝나면 [클립보드 복사]

브라우저에 설치하는 것은 없다(북마크·확장·인증서 없음). 페이지를 옮겨도
그 화면에서 다시 누를 것이 없다. 어떤 도메인에서나 그대로 동작한다.
"""
import ctypes
import io
import json
import os
import queue
import subprocess
import sys
import threading
import traceback
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import launcher as L
from store import Store, PRIORITIES, PRIORITY_LABEL

APP_NAME = '화면 주석 QA'
RES = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))


def pick_home():
    """쓸 수 있는 저장 위치를 고른다. PC 마다 폴더 위치가 다르다는 것을 전제한다.

    ★exe 옆에는 절대 쓰지 않는다 - USB·읽기전용 공유·다운로드 폴더에서 실행될 수 있다.
      %LOCALAPPDATA% 가 없거나(드문 구성) 로밍/네트워크로 리다이렉트돼 쓸 수 없는 PC 도
      있으므로, 실제로 파일을 써 보고 되는 곳을 고른다. 첫 후보가 되면 거기서 끝난다."""
    seen = []
    for base in (os.environ.get('LOCALAPPDATA'), os.environ.get('APPDATA'),
                 os.environ.get('TEMP'), os.path.expanduser('~')):
        if not base or base in seen:
            continue
        seen.append(base)
        cand = os.path.join(base, 'qa-annotator')
        try:
            if not os.path.isdir(cand):
                os.makedirs(cand)
            probe = os.path.join(cand, '.write-test')
            with open(probe, 'w', encoding='utf-8') as f:
                f.write('ok')
            os.remove(probe)
            return cand
        except Exception:
            continue
    # 마지막 수단: 현재 작업 폴더(여기도 안 되면 어차피 아무것도 못 한다)
    return os.path.join(os.getcwd(), 'qa-annotator')


HOME = pick_home()
OUT_DIR = os.path.join(HOME, 'out')
PROFILE_DIR = os.path.join(HOME, 'profile')
SETTINGS = os.path.join(HOME, 'settings.json')
SESSION = os.path.join(HOME, 'session.json')
INJECT_JS = os.path.join(RES, 'dist', 'inject.js')
HELP_HTML = os.path.join(RES, 'docs', '사용법.html')

INK = '#111111'
MUTED = '#707070'
LINE = '#e0e0e0'
RED = '#e1251b'


def ensure_home():
    for d in (HOME, OUT_DIR):
        if not os.path.isdir(d):
            os.makedirs(d)


def enable_dpi_awareness():
    """고해상도·배율 화면에서 창이 흐릿하게 확대되지 않게 한다.

    tkinter 는 기본적으로 DPI 를 모른다. 이 PC 가 100% 라도 다른 PC 는 125·150·200%
    가 흔하고(고해상도 노트북·4K), 그 화면에서는 창 전체가 비트맵 확대되어 뭉개진다.
    반드시 창을 만들기 전에 불러야 한다."""
    if os.name != 'nt':
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # PROCESS_SYSTEM_DPI_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()           # 구형 윈도우 폴백
    except Exception:
        pass


_MUTEX = None


def acquire_single_instance():
    """이 프로그램은 한 번에 하나만 돈다. 이미 돌고 있으면 False.

    ★여러 개가 동시에 뜨면 각자 브라우저에 주입을 등록하고 같은 결과 파일에 쓴다
      → 툴바가 겹치고 목록이 뒤섞인다(실측: 사용자 PC 에 4개가 떠 있었다).
      사람들은 아이콘을 여러 번 누른다는 것을 전제로 막아야 한다."""
    global _MUTEX
    if os.name != 'nt':
        return True
    ERROR_ALREADY_EXISTS = 183
    try:
        k = ctypes.windll.kernel32
        _MUTEX = k.CreateMutexW(None, False, 'Local\\qa-annotator-single')
        return k.GetLastError() != ERROR_ALREADY_EXISTS
    except Exception:
        return True                     # 잠금을 못 걸면 막지 않는다(기능이 우선)


def focus_existing_window():
    """이미 떠 있는 창을 앞으로 가져온다 - "이미 실행 중" 이라는 말만 하면 불친절하다."""
    if os.name != 'nt':
        return False
    try:
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, APP_NAME)
        if not hwnd:
            return False
        u.ShowWindow(hwnd, 9)           # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def set_clipboard(text):
    """윈도우 클립보드에 직접 쓴다. 성공하면 True.

    ★tkinter 의 clipboard_append 는 Tk 가 클립보드를 '소유' 하는 방식이라
      프로그램을 닫으면 내용이 사라진다(실측: 종료 후 붙여넣기 = 빈 값).
      QA 는 복사한 뒤 프로그램을 닫고 붙여넣는다 - 그러면 다 잃는다."""
    if os.name != 'nt':
        return False
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    try:
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        if not u32.OpenClipboard(None):
            return False
        try:
            u32.EmptyClipboard()
            k32.GlobalAlloc.restype = ctypes.c_void_p
            h = k32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not h:
                return False
            k32.GlobalLock.restype = ctypes.c_void_p
            k32.GlobalLock.argtypes = [ctypes.c_void_p]
            dst = k32.GlobalLock(h)
            if not dst:
                return False
            ctypes.memmove(dst, buf, size)
            k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            k32.GlobalUnlock(h)
            u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
            if not u32.SetClipboardData(CF_UNICODETEXT, h):
                return False
            return True
        finally:
            u32.CloseClipboard()
    except Exception:
        return False


def pick_font(root, candidates, fallback='TkDefaultFont'):
    """설치된 것 중 첫 번째를 고른다. 한국어 Windows 가 아닌 PC 에는 맑은 고딕이 없다."""
    try:
        from tkinter import font as tkfont
        have = set(tkfont.families(root))
    except Exception:
        return fallback
    for c in candidates:
        if c in have:
            return c
    return fallback


class App(tk.Tk):
    def __init__(self):
        tk.Tk.__init__(self)
        ensure_home()
        self.title(APP_NAME)
        self.configure(bg='#ffffff')

        # 이 PC 기준으로 크기를 박지 않는다 - 실제 화면에 맞춰 잡고 가운데 놓는다.
        self.ui_font = pick_font(self, ['맑은 고딕', 'Malgun Gothic', 'Segoe UI', 'Noto Sans KR'])
        self.mono_font = pick_font(self, ['Consolas', 'D2Coding', 'Courier New'])
        self._fit_window(980, 660, 820, 520)

        self.store = Store(OUT_DIR)
        self.launcher = None
        self._starting = False      # [QA 시작] 연타로 브라우저가 두 번 뜨지 않게
        self.settings = self.load_settings()
        # ★작업 스레드는 tkinter 를 직접 만지지 않는다.
        #   after() 조차 다른 스레드에서 부르면 "main thread is not in main loop" 로
        #   죽는다(실측 - 이 때문에 [QA 시작] 이 실패했다). 메시지만 큐에 넣고
        #   그리는 일은 메인 스레드의 _drain() 이 한다.
        self._msgq = queue.Queue()

        # 기본값을 박아 두지 않는다 - 어떤 프로젝트에서든 그대로 쓰려면 빈 칸이어야 한다.
        self.target_url = tk.StringVar(value=self.settings.get('last_url', ''))
        self.status_text = tk.StringVar(value='대기')
        self.count_text = tk.StringVar(value='화면 0개 · 주석 0건')

        self._style()
        self._head()
        self._bar()
        self._list()
        self._log()
        self._foot()

        self._drain()
        pages, total = self.store.replay()
        if total:
            self.log('지난 기록을 복원했습니다 - 화면 %d개 · 주석 %d건' % (pages, total))
        self.refresh()

        problems = L.preflight()
        if problems:
            self.set_status(False, '점검 필요')
            for p in problems:
                self.log(p.replace('\n', ' '))
            messagebox.showwarning('시작하기 전에', '\n\n'.join(problems))
        else:
            name, _exe = L.find_browser()
            self.log('%s 를 사용합니다. 주소를 넣고 [QA 시작] 을 누르세요.' % name)

        self.protocol('WM_DELETE_WINDOW', self.on_close)

        # 실행 인자로 주소를 주면 그대로 시작한다(바로가기·명령줄용, 그리고 빌드 검증용).
        #   화면주석-QA.exe https://example.com/…
        if len(sys.argv) > 1 and sys.argv[1].strip() and not problems:
            self.target_url.set(sys.argv[1].strip())
            self.after(400, self.start_qa)

    def _fit_window(self, want_w, want_h, min_w, min_h):
        """원하는 크기를 화면 안으로 접어 넣는다(작업표시줄 여유 포함) + 가운데 정렬."""
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
        except Exception:
            sw, sh = want_w, want_h
        w = max(min(want_w, sw - 80), min(min_w, sw - 20))
        h = max(min(want_h, sh - 140), min(min_h, sh - 40))
        x = max((sw - w) // 2, 0)
        y = max((sh - h) // 3, 0)
        self.geometry('%dx%d+%d+%d' % (w, h, x, y))
        self.minsize(min(min_w, w), min(min_h, h))

    # ── 설정 ──────────────────────────────────────────────────
    def load_settings(self):
        try:
            with io.open(SETTINGS, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def save_settings(self):
        try:
            self.settings['last_url'] = self.target_url.get().strip()
            with io.open(SETTINGS, 'w', encoding='utf-8') as f:
                f.write(json.dumps(self.settings, ensure_ascii=False, indent=2))
        except Exception:
            pass

    # ── 화면 구성 ──────────────────────────────────────────────
    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use('clam')
        except Exception:
            pass
        f = self.ui_font
        st.configure('.', background='#ffffff', foreground=INK, font=(f, 10))
        st.configure('H1.TLabel', font=(f, 15, 'bold'))
        st.configure('Muted.TLabel', foreground=MUTED, font=(f, 9))
        st.configure('TButton', padding=(12, 6))
        st.configure('Go.TButton', padding=(18, 7), font=(f, 10, 'bold'))
        st.configure('Treeview', rowheight=24, fieldbackground='#ffffff')
        st.configure('Treeview.Heading', font=(f, 9, 'bold'))

    def _head(self):
        head = ttk.Frame(self, padding=(18, 16, 18, 4))
        head.pack(fill='x')
        ttk.Label(head, text=APP_NAME, style='H1.TLabel').pack(side='left')
        ttk.Label(head, text='  화면을 클릭해 남긴 지적을 셀렉터·좌표가 붙은 데이터로 넘긴다',
                  style='Muted.TLabel').pack(side='left', pady=(5, 0))
        self.badge = tk.Label(head, textvariable=self.status_text, bg='#e6e6e6', fg='#555555',
                              font=(self.ui_font, 9, 'bold'), padx=10, pady=3)
        self.badge.pack(side='right')
        ttk.Button(head, text='사용법', command=self.open_help).pack(side='right', padx=(0, 10))

    def _bar(self):
        wrap = ttk.Frame(self, padding=(18, 6, 18, 8))
        wrap.pack(fill='x')
        card = tk.Frame(wrap, bg='#ffffff', highlightbackground=LINE, highlightthickness=1)
        card.pack(fill='x')

        r1 = tk.Frame(card, bg='#ffffff')
        r1.pack(fill='x', padx=12, pady=(12, 6))
        tk.Label(r1, text='테스트할 주소', bg='#ffffff', fg=INK,
                 font=(self.ui_font, 10, 'bold')).pack(side='left')
        e = tk.Entry(r1, textvariable=self.target_url, font=(self.ui_font, 11),
                     relief='solid', bd=1)
        e.pack(side='left', fill='x', expand=True, padx=10)
        e.bind('<Return>', lambda _e: self.start_qa())
        ttk.Button(r1, text='QA 시작', style='Go.TButton', command=self.start_qa).pack(side='left')

        r2 = tk.Frame(card, bg='#ffffff')
        r2.pack(fill='x', padx=12, pady=(0, 12))
        tk.Label(r2, bg='#ffffff', fg=MUTED, font=(self.ui_font, 9), justify='left',
                 text='브라우저에 설치하는 것은 없습니다. 열린 창에서 주소를 바꿔 돌아다녀도 '
                      '화면마다 툴바가 자동으로 뜹니다.\n'
                      '전용 프로필이라 처음 한 번만 로그인하면 그다음부터 유지됩니다.'
                 ).pack(side='left')

    def _list(self):
        wrap = ttk.Frame(self, padding=(18, 0, 18, 8))
        wrap.pack(fill='both', expand=True)

        bar = ttk.Frame(wrap)
        bar.pack(fill='x', pady=(0, 6))
        ttk.Label(bar, textvariable=self.count_text).pack(side='left')
        ttk.Button(bar, text='결과 폴더', command=lambda: self.open_path(OUT_DIR)).pack(side='right')
        ttk.Button(bar, text='지난 기록', command=self.do_load_archive
                   ).pack(side='right', padx=(0, 8))
        ttk.Button(bar, text='되돌리기', command=self.do_undo).pack(side='right', padx=(0, 8))
        ttk.Button(bar, text='비우기 (보관)', command=self.do_reset).pack(side='right', padx=(0, 8))
        ttk.Button(bar, text='추출 (저장 후 삭제)', command=self.do_export).pack(side='right', padx=(0, 8))
        ttk.Button(bar, text='클립보드 복사', command=self.do_copy).pack(side='right', padx=(0, 8))

        cols = ('no', 'title', 'vp', 'url', 'ann', 'con', 'net')
        # ★화면 밑에 주석을 펼친다(show 에 'tree' 를 넣어야 펼침 화살표가 생긴다).
        #   주석 줄을 더블클릭하면 보충 메모·연결을 넣는 창이 열린다 - 버튼을 늘리지 않는다.
        self.tree = ttk.Treeview(wrap, columns=cols, show='tree headings', height=11)
        self.tree.column('#0', width=26, minwidth=26, stretch=False)
        self.tree.bind('<Double-1>', self.on_row_open)
        # 프3: 화면 줄에서 그 주소로 브라우저를 보낸다. 프4: 주석 순서를 옮긴다.
        self.tree.bind('<Button-3>', self.on_row_menu)
        self.tree.bind('<Control-Up>', lambda e: self.move_row(-1))
        self.tree.bind('<Control-Down>', lambda e: self.move_row(1))
        self._tree_sig = None
        self._menu = tk.Menu(self, tearoff=0)
        # 해상도는 화면 정체성의 일부다(같은 주소라도 해상도가 다르면 다른 줄).
        for key, label, width, anchor in (
                ('no', '화면', 44, 'center'), ('title', '제목', 190, 'w'),
                ('vp', '해상도', 92, 'center'), ('url', '주소', 360, 'w'),
                ('ann', '주석', 52, 'center'),
                ('con', '콘솔에러', 66, 'center'), ('net', '실패요청', 66, 'center')):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor=anchor)
        self.tree.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(wrap, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')

    def _log(self):
        wrap = ttk.Frame(self, padding=(18, 0, 18, 6))
        wrap.pack(fill='x')
        ttk.Label(wrap, text='진행 상황', style='Muted.TLabel').pack(anchor='w')
        self.logbox = tk.Text(wrap, height=6, font=(self.mono_font, 9), bg='#1e2433',
                              fg='#d7dbe3', relief='flat', wrap='word')
        self.logbox.pack(fill='x')
        self.logbox.configure(state='disabled')

    def _foot(self):
        foot = ttk.Frame(self, padding=(18, 0, 18, 12))
        foot.pack(fill='x')
        ttk.Label(foot, text='QA 전용 · 어떤 도메인에서나 사용 · 사이트에 아무것도 설치하지 않는다',
                  style='Muted.TLabel').pack(side='left')
        ttk.Label(foot, text=HOME, style='Muted.TLabel').pack(side='right')

    # ── 로그 · 상태 (스레드 안전) ───────────────────────────────
    def log(self, msg):
        """어느 스레드에서 불러도 안전하다 - 큐에만 넣는다."""
        self._msgq.put(('log', msg))

    def _drain(self):
        """메인 스레드에서만 돈다. 큐에 쌓인 것을 실제로 그린다."""
        try:
            while True:
                kind, payload = self._msgq.get_nowait()
                if kind == 'log':
                    self._write_log(payload)
                elif kind == 'status':
                    self.set_status(payload[0], payload[1])
                elif kind == 'dialog':
                    box = {'error': messagebox.showerror,
                           'warn': messagebox.showwarning,
                           'info': messagebox.showinfo}.get(payload[0], messagebox.showinfo)
                    box(payload[1], payload[2])
        except queue.Empty:
            pass
        except Exception:
            pass
        self.after(150, self._drain)

    def _write_log(self, msg):
        self.logbox.configure(state='normal')
        self.logbox.insert('end', '[%s] %s\n' % (datetime.now().strftime('%H:%M:%S'), msg))
        self.logbox.see('end')
        self.logbox.configure(state='disabled')

    def post_status(self, ok, text):
        self._msgq.put(('status', (ok, text)))

    def post_dialog(self, kind, title, msg):
        self._msgq.put(('dialog', (kind, title, msg)))

    def set_status(self, ok, text):
        self.status_text.set(text)
        self.badge.configure(bg='#e8f5e9' if ok else '#f3f3f3',
                             fg='#1b5e20' if ok else '#555555')

    # ── 동작 ──────────────────────────────────────────────────
    def start_qa(self):
        if self._starting:
            self.log('이미 시작하는 중입니다. 잠시만 기다려 주세요.')
            return
        url = self.target_url.get().strip()
        self.save_settings()
        if self.launcher and self.launcher.alive():
            if url:
                try:
                    self.log('이동: %s' % self.launcher.navigate(url))
                except Exception as e:
                    messagebox.showerror('이동 실패', str(e))
            else:
                self.log('브라우저가 이미 열려 있습니다.')
            return

        if not os.path.exists(INJECT_JS):
            messagebox.showerror('파일 없음',
                                 '주입 스크립트가 없습니다:\n%s\n\n'
                                 '개발 폴더라면 build_ko.py 와 esbuild 번들을 먼저 만들어야 합니다.'
                                 % INJECT_JS)
            return
        with io.open(INJECT_JS, encoding='utf-8') as f:
            inject = f.read()

        self.set_status(False, '브라우저 실행 중')
        self.launcher = L.Launcher(inject, self.store, PROFILE_DIR, log=self.log,
                                   state_path=SESSION)
        # 목록이 비어 있다면(= 이미 추출/비우기 했다면) 브라우저에 남은 지난 주석도 정리한다.
        # 그러지 않으면 브라우저를 띄울 때마다 옛 마커가 다시 보인다.
        self.launcher.clear_stale = (self.store.counts()[1] == 0)
        self._starting = True

        def run():
            try:
                self.launcher.start(url or None)
            except Exception as e:
                self.launcher = None
                self._starting = False
                self.log('실패: %s' % e)
                self.post_dialog('error', '시작하지 못했습니다', str(e))
                self.post_status(False, '실패')
                return
            self._starting = False
            self._watch()
        threading.Thread(target=run, daemon=True).start()

    def _watch(self):
        """주입 상태를 스스로 확인해 배지에 올린다(사람이 눈으로 "떴나?" 하지 않게).

        CDP 호출은 응답을 기다리므로 메인 스레드에서 돌리면 창이 잠깐 멈춘다 → 여기서 돈다."""
        import time as _t
        while self.launcher and self.launcher.alive():
            try:
                n = self.launcher.check_injected()
            except Exception:
                n = 0
            try:
                moved = self.launcher.rearrange_count()
            except Exception:
                moved = 0
            # 브9: 레이아웃 모드에서 바꾼 개수를 주석 모드에서도 알 수 있게 한다.
            label = ('주입됨 (탭 %d)' % n) if n else '주입 대기'
            if moved:
                label += ' · 레이아웃 변경 %d' % moved
            self.post_status(bool(n), label)
            _t.sleep(3)

    def refresh(self):
        pages, total = self.store.counts()
        self.count_text.set('화면 %d개 · 주석 %d건' % (pages, total))
        self._fill_tree()
        if self.launcher and not self.launcher.alive():
            self.set_status(False, '브라우저 닫힘')
            self.launcher = None
        self.after(1000, self.refresh)

    # ── 목록(화면 > 주석) ─────────────────────────────────────
    @staticmethod
    def _screen_iid(row):
        # 화면 정체성은 (주소, 해상도) 다 - 번호가 아니라 그걸로 줄을 식별해야
        # 새 화면이 끼어들어도 펼친 상태가 딴 줄로 옮겨가지 않는다.
        return 'p|%s|%s' % (row['url'], row['vp'])

    def _fill_tree(self):
        rows = self.store.tree_rows()
        # ★1초마다 통째로 다시 그리면 펼친 것이 접히고 선택이 풀린다.
        #   내용이 그대로면 손대지 않는다.
        sig = repr([(r['no'], r['title'], r['vp'], r['url'], r['ann'], r['con'], r['net'],
                     [(a['aid'], a['comment'], a['note'], a['refs']) for a in r['anns']])
                    for r in rows])
        if sig == self._tree_sig:
            return
        self._tree_sig = sig
        opened = set()
        for iid in self.tree.get_children(''):
            if self.tree.item(iid, 'open'):
                opened.add(iid)
        sel = self.tree.selection()
        for iid in self.tree.get_children(''):
            self.tree.delete(iid)
        for r in rows:
            pid = self._screen_iid(r)
            self.tree.insert('', 'end', iid=pid, open=(pid in opened),
                             values=(r['no'], r['title'], r['vp'], r['url'],
                                     r['ann'], r['con'], r['net']))
            for a in r['anns']:
                mark = ''
                if a.get('priority'):
                    mark += ' [%s]' % PRIORITY_LABEL[a['priority']]
                if a.get('expected'):
                    mark += ' →기대'
                if a['note']:
                    mark += ' ✎'                    # 보충 메모가 있다
                if a['refs']:
                    mark += ' ↔%d' % a['refs']      # 연결이 있다
                memo = (a['comment'] or '(메모 없음)').replace('\n', ' ')
                if len(memo) > 60:
                    memo = memo[:60] + '…'
                self.tree.insert(pid, 'end', iid='a|%s' % a['aid'],
                                 values=('%d.' % a['no'],
                                         '%s — %s%s' % (a['element'], memo, mark),
                                         '', '', '', '', ''))
        for iid in sel:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)

    def on_row_open(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if not iid.startswith('a|'):
            # 화면 줄이면 펼치기/접기만 한다.
            self.tree.item(iid, open=not self.tree.item(iid, 'open'))
            return
        self.open_annotation(iid[2:])

    def _row_key(self, iid):
        """화면 줄 iid('p|url|vp')에서 store 키를 되돌린다."""
        if not iid.startswith('p|'):
            return None
        body = iid[2:]
        i = body.rfind('|')
        return (body[:i], body[i + 1:]) if i > 0 else None

    def on_row_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self._menu.delete(0, 'end')
        if iid.startswith('p|'):
            key = self._row_key(iid)
            url = key[0] if key else ''
            self._menu.add_command(label='이 화면으로 이동',
                                   command=lambda u=url: self.goto_url(u))
            self._menu.add_command(label='주소 복사',
                                   command=lambda u=url: self.copy_text(u))
        else:
            aid = iid[2:]
            self._menu.add_command(label='보강 창 열기',
                                   command=lambda a=aid: self.open_annotation(a))
            self._menu.add_separator()
            self._menu.add_command(label='위로 (Ctrl+↑)', command=lambda: self.move_row(-1))
            self._menu.add_command(label='아래로 (Ctrl+↓)', command=lambda: self.move_row(1))
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def goto_url(self, url):
        """프3: 목록의 그 화면으로 브라우저를 보낸다."""
        if not url:
            return
        if not (self.launcher and self.launcher.alive()):
            messagebox.showinfo('브라우저가 없습니다',
                                '[QA 시작] 으로 브라우저를 먼저 열어 주세요.')
            return
        try:
            self.launcher.navigate(url)
            self.log('그 화면으로 이동 - %s' % url)
        except Exception as e:
            messagebox.showerror('이동 실패', str(e))

    def copy_text(self, text):
        if set_clipboard(text or ''):
            self.log('클립보드로 복사 - %s' % (text or '')[:80])

    def move_row(self, delta):
        """프4: 같은 화면 안에서 주석 순서를 옮긴다."""
        sel = self.tree.selection()
        if not sel or not sel[0].startswith('a|'):
            return
        iid = sel[0]
        aid = iid[2:]
        key = self._row_key(self.tree.parent(iid))
        if not key:
            return
        if self.store.move_annotation(key, aid, delta) is None:
            return
        self._tree_sig = None
        self._fill_tree()
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)
        self.log('주석 순서를 옮겼습니다.')

    def do_undo(self):
        """브7: 실수로 지운 것을 되살린다."""
        hit = self.store.undoable()
        if not hit:
            messagebox.showinfo('되돌릴 것이 없습니다',
                                '지우기·삭제를 한 적이 없거나 이미 다 되돌렸습니다.')
            return
        _idx, desc = hit
        if not messagebox.askyesno('되돌리기', '%s\n\n이 작업을 되돌릴까요?' % desc):
            return
        n = self.store.undo()
        self._tree_sig = None
        self._fill_tree()
        # ★브라우저 핀은 되돌아오지 않는다(역방향 경로가 없다). 그 사실을 숨기지 않는다.
        self.log('되돌렸습니다 - 주석 %d건 복구 (%s). 브라우저의 핀은 복구되지 않습니다.'
                 % (n, desc))
        messagebox.showinfo('되돌렸습니다',
                            '주석 %d건을 목록에 되살렸습니다.\n\n'
                            '결과 문서에는 바로 반영됩니다.\n'
                            '브라우저 화면의 핀은 되살아나지 않습니다.' % n)

    def do_load_archive(self):
        """프5: 추출·비우기 때 보관한 기록을 다시 불러온다."""
        arch = os.path.join(OUT_DIR, 'archive')
        path = filedialog.askopenfilename(
            title='불러올 지난 기록을 고르세요',
            initialdir=arch if os.path.isdir(arch) else OUT_DIR,
            filetypes=[('주석 기록', '*.jsonl'), ('모든 파일', '*.*')])
        if not path:
            return
        try:
            added, (pages, total) = self.store.load_archive(path)
        except Exception as e:
            messagebox.showerror('불러오기 실패', str(e))
            return
        self._tree_sig = None
        self._fill_tree()
        self.log('지난 기록을 합쳤습니다 - %s (%d줄) → 화면 %d개 · 주석 %d건'
                 % (os.path.basename(path), added, pages, total))
        messagebox.showinfo('불러왔습니다',
                            '%s\n\n현재 목록에 합쳤습니다.\n화면 %d개 · 주석 %d건'
                            % (os.path.basename(path), pages, total))

    def open_annotation(self, aid):
        """주석 하나에 보충 메모와 연결을 넣는다.

        ★원본 메모(comment)는 건드리지 않는다. 그건 브라우저(agentation)가 소유하고,
          사용자가 핀을 눌러 고치면 그 값으로 덮어써진다. 우리가 적는 것은 별도로 둔다."""
        info = None
        for x in self.store.all_annotations():
            if x['aid'] == aid:
                info = x
                break
        if info is None:
            messagebox.showinfo('없는 주석', '목록이 갱신된 것 같습니다. 다시 골라 주세요.')
            return
        meta = self.store.get_meta(aid)
        f = self.ui_font

        win = tk.Toplevel(self)
        win.title('주석 보강 — [화면 %d] %d번' % (info['screen'], info['no']))
        win.configure(bg='#ffffff')
        win.transient(self)
        win.geometry('620x720')
        pad = {'padx': 16}

        tk.Label(win, bg='#ffffff', font=(f, 11, 'bold'), anchor='w',
                 text='[화면 %d] %d번  %s' % (info['screen'], info['no'], info['element'])
                 ).pack(fill='x', pady=(14, 2), **pad)
        tk.Label(win, bg='#ffffff', fg=MUTED, font=(f, 9), anchor='w', justify='left',
                 text='%s  (%s)' % (info['title'], info['vp'])).pack(fill='x', **pad)

        tk.Label(win, bg='#ffffff', fg=MUTED, font=(f, 9), anchor='w',
                 text='브라우저에서 적은 메모 (여기서는 고치지 않습니다)'
                 ).pack(fill='x', pady=(12, 2), **pad)
        ro = tk.Text(win, height=3, font=(f, 10), bg='#f4f5f7', relief='flat', wrap='word')
        ro.insert('1.0', info['comment'] or '(메모 없음)')
        ro.configure(state='disabled')
        ro.pack(fill='x', **pad)

        # ── 우선순위 · 기대 ──
        row = tk.Frame(win, bg='#ffffff')
        row.pack(fill='x', pady=(14, 0), **pad)
        tk.Label(row, bg='#ffffff', font=(f, 10, 'bold'), text='우선순위').pack(side='left')
        prio = tk.StringVar(value=meta['priority'])
        tk.Radiobutton(row, text='미지정', value='', variable=prio, bg='#ffffff',
                       font=(f, 9), anchor='w').pack(side='left', padx=(10, 0))
        for code, label in PRIORITIES:
            tk.Radiobutton(row, text=label, value=code, variable=prio, bg='#ffffff',
                           font=(f, 9), anchor='w').pack(side='left', padx=(6, 0))

        tk.Label(win, bg='#ffffff', font=(f, 10, 'bold'), anchor='w',
                 text='기대 (이렇게 되어야 합니다)').pack(fill='x', pady=(12, 2), **pad)
        tk.Label(win, bg='#ffffff', fg=MUTED, font=(f, 9), anchor='w', justify='left',
                 text='위 메모가 "현재", 여기가 "기대" 입니다. 받는 사람이 되묻지 않게 나눠 적습니다.'
                 ).pack(fill='x', **pad)
        expected = tk.Text(win, height=3, font=(f, 10), relief='solid', bd=1, wrap='word')
        expected.insert('1.0', meta['expected'])
        expected.pack(fill='x', **pad)

        tk.Label(win, bg='#ffffff', font=(f, 10, 'bold'), anchor='w',
                 text='보충 메모').pack(fill='x', pady=(14, 2), **pad)
        tk.Label(win, bg='#ffffff', fg=MUTED, font=(f, 9), anchor='w', justify='left',
                 text='앞뒤 조작·재현 조건처럼 나중에 덧붙일 설명을 적습니다. 결과 문서의 "보충" 줄이 됩니다.'
                 ).pack(fill='x', **pad)
        note = tk.Text(win, height=5, font=(f, 10), relief='solid', bd=1, wrap='word')
        note.insert('1.0', meta['note'])
        note.pack(fill='x', **pad)

        tk.Label(win, bg='#ffffff', font=(f, 10, 'bold'), anchor='w',
                 text='관련 주석 (원인이 다른 화면에 있을 때)').pack(fill='x', pady=(14, 2), **pad)
        tk.Label(win, bg='#ffffff', fg=MUTED, font=(f, 9), anchor='w', justify='left',
                 text='연결하면 양쪽 결과에 서로를 가리키는 줄이 함께 들어갑니다.'
                 ).pack(fill='x', **pad)
        box = tk.Frame(win, bg='#ffffff')
        box.pack(fill='both', expand=True, pady=(2, 0), **pad)
        lst = tk.Listbox(box, font=(f, 10), height=5, relief='solid', bd=1,
                         activestyle='none', exportselection=False)
        lst.pack(side='left', fill='both', expand=True)
        side = tk.Frame(box, bg='#ffffff')
        side.pack(side='right', fill='y', padx=(8, 0))

        refs = list(meta['refs'])
        label_of = {}
        for x in self.store.all_annotations():
            memo = (x['comment'] or '')[:34]
            label_of[x['aid']] = '[화면 %d] %d번 %s%s' % (
                x['screen'], x['no'], x['element'], ' — "%s"' % memo if memo else '')

        def redraw():
            lst.delete(0, 'end')
            for r in refs:
                lst.insert('end', label_of.get(r, '(삭제된 주석)'))

        def add_ref():
            pick = tk.Toplevel(win)
            pick.title('연결할 주석 고르기')
            pick.configure(bg='#ffffff')
            pick.transient(win)
            pick.geometry('640x420')
            tk.Label(pick, bg='#ffffff', fg=MUTED, font=(f, 9), anchor='w', justify='left',
                     text='이 주석의 원인이 되는(또는 관련된) 주석을 고릅니다.'
                     ).pack(fill='x', padx=14, pady=(12, 4))
            pl = tk.Listbox(pick, font=(f, 10), relief='solid', bd=1, exportselection=False)
            pl.pack(fill='both', expand=True, padx=14)
            cand = [x for x in self.store.all_annotations()
                    if x['aid'] != aid and x['aid'] not in refs]
            for x in cand:
                pl.insert('end', label_of[x['aid']])
            if not cand:
                pl.insert('end', '(연결할 다른 주석이 없습니다)')

            def take():
                i = pl.curselection()
                if i and cand:
                    refs.append(cand[i[0]]['aid'])
                    redraw()
                pick.destroy()
            bar = tk.Frame(pick, bg='#ffffff')
            bar.pack(fill='x', padx=14, pady=12)
            ttk.Button(bar, text='취소', command=pick.destroy).pack(side='right')
            ttk.Button(bar, text='연결', command=take).pack(side='right', padx=(0, 8))
            pl.bind('<Double-1>', lambda e: take())

        def del_ref():
            i = lst.curselection()
            if i:
                refs.pop(i[0])
                redraw()

        ttk.Button(side, text='연결 추가', command=add_ref).pack(fill='x')
        ttk.Button(side, text='연결 제거', command=del_ref).pack(fill='x', pady=(6, 0))
        redraw()

        def save():
            self.store.set_meta(aid, note.get('1.0', 'end').strip(), refs,
                                expected.get('1.0', 'end').strip(), prio.get())
            self._tree_sig = None               # 다음 갱신에서 다시 그리게 한다
            self.log('주석 보강 저장 - [화면 %d] %d번 (연결 %d건%s%s)'
                     % (info['screen'], info['no'], len(refs),
                        ' · 기대' if expected.get('1.0', 'end').strip() else '',
                        ' · %s' % PRIORITY_LABEL[prio.get()] if prio.get() else ''))
            win.destroy()

        bar = tk.Frame(win, bg='#ffffff')
        bar.pack(fill='x', pady=14, **pad)
        ttk.Button(bar, text='취소', command=win.destroy).pack(side='right')
        ttk.Button(bar, text='저장', style='Go.TButton', command=save).pack(side='right', padx=(0, 8))
        note.focus_set()

    def ask_closing(self):
        """총평(마지막 코멘트)을 받는다. 취소하면 None - 그때는 복사·추출을 하지 않는다.

        결과 문서 맨 앞(요약 바로 밑)에 들어간다 - 받는 개발자가 먼저 읽는 자리다."""
        f = self.ui_font
        win = tk.Toplevel(self)
        win.title('총평 (마지막 코멘트)')
        win.configure(bg='#ffffff')
        win.transient(self)
        win.grab_set()
        win.geometry('600x330')

        tk.Label(win, bg='#ffffff', font=(f, 11, 'bold'), anchor='w',
                 text='마지막으로 덧붙일 말이 있나요?').pack(fill='x', padx=16, pady=(16, 2))
        tk.Label(win, bg='#ffffff', fg=MUTED, font=(f, 9), anchor='w', justify='left',
                 text='결과 문서 맨 앞에 "총평" 으로 들어갑니다. 없으면 [건너뛰기].'
                 ).pack(fill='x', padx=16)
        txt = tk.Text(win, height=7, font=(f, 10), relief='solid', bd=1, wrap='word')
        txt.insert('1.0', self.store.closing)
        txt.pack(fill='both', expand=True, padx=16, pady=(8, 0))

        out = {'ok': False}

        def ok():
            out['ok'] = True
            self.store.set_closing(txt.get('1.0', 'end').strip())
            win.destroy()

        def skip():
            out['ok'] = True                    # 진행은 한다. 총평만 그대로 둔다.
            win.destroy()

        bar = tk.Frame(win, bg='#ffffff')
        bar.pack(fill='x', padx=16, pady=14)
        ttk.Button(bar, text='취소', command=win.destroy).pack(side='right')
        ttk.Button(bar, text='건너뛰기', command=skip).pack(side='right', padx=(0, 8))
        ttk.Button(bar, text='확인', style='Go.TButton', command=ok).pack(side='right', padx=(0, 8))
        txt.focus_set()
        self.wait_window(win)
        return out['ok']

    def do_copy(self):
        pages, total = self.store.counts()
        if not total:
            messagebox.showinfo('내용 없음', '아직 주석이 없습니다.')
            return
        if not self.ask_closing():
            return
        text = self.store.render()
        if set_clipboard(text):
            self.log('클립보드로 복사 - 화면 %d개 · 주석 %d건 (창을 닫아도 유지됩니다)'
                     % (pages, total))
        else:
            # 네이티브가 실패하면 tk 로 폴백. 이 경우엔 창을 닫으면 사라진다.
            try:
                self.clipboard_clear()
                self.clipboard_append(text)
                self.update()
                self.log('클립보드 복사(폴백) - 붙여넣기 전에는 이 창을 닫지 마세요.')
            except Exception as e:
                messagebox.showerror('복사 실패', str(e))
                return
        messagebox.showinfo('복사했습니다',
                            '화면 %d개 · 주석 %d건을 클립보드에 담았습니다.\n'
                            '메일·메신저·이슈에 그대로 붙여넣으세요.' % (pages, total))

    def do_export(self):
        pages, total = self.store.counts()
        if not total:
            messagebox.showinfo('내용 없음', '아직 주석이 없습니다.')
            return
        if not self.ask_closing():
            return
        default = '화면주석-%s.md' % datetime.now().strftime('%Y%m%d-%H%M')
        path = filedialog.asksaveasfilename(
            title='어디에 저장할까요?', initialfile=default,
            defaultextension='.md', filetypes=[('마크다운', '*.md'), ('모든 파일', '*.*')])
        if not path:
            return
        try:
            self.store.export(path)
        except Exception as e:
            # 저장이 실패하면 목록을 지우지 않는다(잃는 것보다 중복이 낫다).
            messagebox.showerror('저장 실패', '%s\n\n목록은 그대로 두었습니다.' % e)
            return
        self._tree_sig = None
        n = self.clear_browser_side()
        self.log('추출 완료 - %s (화면 %d개 · 주석 %d건). 목록을 비웠습니다%s.'
                 % (path, pages, total, ' · 브라우저 이력 %d건도 비움' % n if n else ''))
        messagebox.showinfo('저장했습니다', path)

    def do_reset(self):
        pages, total = self.store.counts()
        if not total:
            # ★목록이 비어 있어도 브라우저 쪽은 남아 있을 수 있다(agentation 자체 저장, 7일).
            #   여기서 그냥 나가 버려서 "목록엔 없는데 브라우저엔 마커가 남는" 상태가 됐다.
            self.store.reset()
            n = self.clear_browser_side()
            self.log('목록은 이미 비어 있었습니다%s.'
                     % (' · 브라우저 이력 %d건 비움' % n if n else
                        ' (브라우저가 열려 있으면 그쪽 이력도 함께 비웁니다)'))
            return
        if not messagebox.askyesno('비우기',
                                   '화면 %d개 · 주석 %d건을 목록에서 비웁니다.\n'
                                   '원본 기록은 결과 폴더의 archive 에 남습니다.\n\n계속할까요?'
                                   % (pages, total)):
            return
        self.store.reset()
        self._tree_sig = None
        n = self.clear_browser_side()
        self.log('목록을 비웠습니다(원본은 archive 에 보관)%s.'
                 % (' · 브라우저 이력 %d건도 비움' % n if n else ''))

    def clear_browser_side(self):
        """브라우저(localStorage)에 남은 주석 이력도 함께 비운다.

        이력이 두 곳에 있어서, 프로그램만 비우면 그 화면에 다시 갔을 때 하단 툴바에
        옛 주석이 그대로 보인다. 사용자에게는 [비우기] 한 번이어야 한다."""
        if not (self.launcher and self.launcher.alive() and self.launcher.cdp):
            return 0
        try:
            return self.launcher.clear_browser_annotations()
        except Exception:
            return 0

    def open_help(self):
        """사용법 문서를 기본 브라우저로 연다.

        exe 안(_MEIPASS)에 들어 있으므로 그대로는 열 수 없다. 쓸 수 있는 곳으로
        한 번 꺼내 놓고 연다 - 다음부터는 그 파일을 그대로 쓴다."""
        target = os.path.join(HOME, '사용법.html')
        try:
            if os.path.exists(HELP_HTML):
                src = io.open(HELP_HTML, encoding='utf-8').read()
                if (not os.path.exists(target)
                        or io.open(target, encoding='utf-8').read() != src):
                    io.open(target, 'w', encoding='utf-8').write(src)
            if not os.path.exists(target):
                messagebox.showinfo('사용법', '사용법 문서를 찾지 못했습니다.')
                return
            self.open_path(target)
            self.log('사용법 문서를 열었습니다 - %s' % target)
        except Exception as e:
            messagebox.showerror('열 수 없습니다', str(e))

    def open_path(self, path):
        try:
            if os.name == 'nt':
                os.startfile(path)
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception as e:
            messagebox.showerror('열 수 없습니다', str(e))

    def on_close(self):
        self.save_settings()
        if self.launcher:
            try:
                self.launcher.stop()
            except Exception:
                pass
        self.destroy()


def main():
    try:
        enable_dpi_awareness()
        if not acquire_single_instance():
            focus_existing_window()
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(APP_NAME, '\n'.join([
                '이미 실행 중입니다.',
                '작업표시줄에 있는 기존 창을 쓰세요.',
                '',
                '(여러 개를 띄우면 브라우저 주입이 겹치고 목록이 섞입니다)',
            ]))
            return
        App().mainloop()
    except Exception:
        # 다른 PC 에서 처음 돌릴 때 콘솔 없는 exe 라 예외가 조용히 사라진다.
        # 그래서 파일로 남기고 사람이 읽을 문장을 띄운다.
        ensure_home()
        crash = os.path.join(HOME, 'crash.log')
        try:                                # 무한히 커지지 않게 - 오래된 것은 버린다
            if os.path.getsize(crash) > 512 * 1024:
                os.remove(crash)
        except Exception:
            pass
        with io.open(crash, 'a', encoding='utf-8') as f:
            f.write('\n=== %s ===\n%s' % (datetime.now(), traceback.format_exc()))
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror('실행하지 못했습니다',
                                 '오류 내용을 아래 파일에 적었습니다.\n\n%s' % crash)
        except Exception:
            pass
        raise


if __name__ == '__main__':
    main()
