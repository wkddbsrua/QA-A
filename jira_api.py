# -*- coding: utf-8 -*-
"""Jira Cloud 에 사람마다 자기 토큰으로 올린다(1.2). 표준 라이브러리만(urllib·ctypes·json·base64).

왜 토큰 경로가 따로 필요한가
  1.1 의 '이슈에 올리기' 는 브라우저 세션을 빌려 댓글칸을 채우고 [등록] 은 사람이 눌렀다.
  그 길은 그대로 둔다(토큰 없는 사람용). 그러나 그림·캡처·동영상 첨부는 리치 편집기 모사가
  실측되지 않았고, 새 이슈 생성은 아예 안 된다 - 그래서 REST 를 쓴다.

토큰 보관
  Windows DPAPI(CryptProtectData) 로 **그 사용자·그 PC** 에서만 풀리게 암호화해 HOME\\jira.json 에
  둔다. 다른 계정·다른 PC 에 파일을 복사해도 복호가 안 된다(그래서 사람마다 자기 토큰을 넣는다).
  파일이 없으면 환경변수 JIRA_API_TOKEN 을 폴백으로 읽는다(개발 PC 용).

본문 형식
  REST **v2** + wiki markup. v3 는 ADF(JSON 트리)가 필수라 변환기가 무겁다. v2 의 description 과
  comment.body 는 문자열(wiki markup)을 받는다(Atlassian 문서). md_to_wiki() 가 결과 문서(.md)를
  줄 단위로 옮긴다 - 완벽한 변환이 아니라 '읽히는' 변환이다.
"""
import base64
import ctypes
import io
import json
import mimetypes
import os
import re
import uuid
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_SITE = 'https://syworks.atlassian.net'
# ★기본 이슈 유형. 사내 프로젝트는 거의 전부 '에픽·작업·자료·QA' 만 있고 'Bug' 는 없다(2026-09-14 실측).
#   'Bug' 를 기본으로 두면 새 이슈 만들기가 첫 시도부터 실패한다.
DEFAULT_ISSUE_TYPE = u'QA'
TOKEN_URL = 'https://id.atlassian.com/manage-profile/security/api-tokens'
VIDEO_WARN_BYTES = 100 * 1024 * 1024        # 이 크기부터 경고(진짜 상한은 사이트 uploadLimit)


class JiraError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, u'%s (HTTP %s)' % (message, status) if status else message)
        self.status = status
        self.message = message


# ── DPAPI ─────────────────────────────────────────────────────
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', ctypes.c_uint32), ('pbData', ctypes.POINTER(ctypes.c_char))]


CRYPTPROTECT_UI_FORBIDDEN = 0x01


def _blob_bytes(blob):
    buf = ctypes.create_string_buffer(blob.cbData)
    ctypes.memmove(buf, blob.pbData, blob.cbData)
    return buf.raw


def dpapi_protect(raw):
    """bytes → 암호화 bytes(현재 사용자 범위). 실패하면 OSError."""
    if os.name != 'nt':
        raise OSError('DPAPI 는 Windows 에서만')
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_DATA_BLOB), ctypes.c_wchar_p, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                         ctypes.POINTER(_DATA_BLOB)]
    crypt32.CryptProtectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    src = ctypes.create_string_buffer(raw, len(raw))
    inp = _DATA_BLOB(len(raw), ctypes.cast(src, ctypes.POINTER(ctypes.c_char)))
    out = _DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(inp), u'qa-annotator jira token', None, None, None,
                                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError('CryptProtectData 실패 (%d)' % ctypes.GetLastError())
    try:
        return _blob_bytes(out)
    finally:
        kernel32.LocalFree(out.pbData)


def dpapi_unprotect(enc):
    if os.name != 'nt':
        raise OSError('DPAPI 는 Windows 에서만')
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(ctypes.c_wchar_p),
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_uint32, ctypes.POINTER(_DATA_BLOB)]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    src = ctypes.create_string_buffer(enc, len(enc))
    inp = _DATA_BLOB(len(enc), ctypes.cast(src, ctypes.POINTER(ctypes.c_char)))
    out = _DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None,
                                      CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError('CryptUnprotectData 실패 (%d) - 다른 사용자·PC 에서 저장한 토큰입니다'
                      % ctypes.GetLastError())
    try:
        return _blob_bytes(out)
    finally:
        kernel32.LocalFree(out.pbData)


# ── 설정 ──────────────────────────────────────────────────────
class JiraConfig(object):
    FIELDS = ('site', 'email', 'project', 'issue_type')

    def __init__(self, path):
        self.path = path
        self.site = DEFAULT_SITE
        self.email = u''
        self.project = u''
        self.issue_type = DEFAULT_ISSUE_TYPE
        self.token = u''
        self.token_source = u''             # 'file' | 'env' | ''
        self.load()

    def load(self):
        data = {}
        try:
            with io.open(self.path, encoding='utf-8') as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
        for k in self.FIELDS:
            v = data.get(k)
            if v:
                setattr(self, k, v)
        self.token = u''
        self.token_source = u''
        enc = data.get('token_dpapi')
        if enc:
            try:
                self.token = dpapi_unprotect(base64.b64decode(enc)).decode('utf-8')
                self.token_source = 'file'
            except Exception:
                self.token = u''
                self.token_source = 'locked'    # 파일은 있는데 이 계정·PC 에서는 못 푼다
        if not self.token and os.environ.get('JIRA_API_TOKEN'):
            self.token = os.environ['JIRA_API_TOKEN']
            self.token_source = 'env'
        return self

    def save(self, token=None):
        """token 을 주면 DPAPI 로 암호화해 함께 저장한다. None 이면 기존 암호문을 유지한다."""
        data = {}
        try:
            with io.open(self.path, encoding='utf-8') as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
        for k in self.FIELDS:
            data[k] = getattr(self, k) or u''
        if token is not None:
            token = token.strip()
            if token:
                data['token_dpapi'] = base64.b64encode(dpapi_protect(token.encode('utf-8'))).decode('ascii')
                self.token = token
                self.token_source = 'file'
            else:
                data.pop('token_dpapi', None)
                self.token = u''
                self.token_source = u''
        d = os.path.dirname(self.path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with io.open(self.path, 'w', encoding='utf-8') as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))

    def ready(self):
        return bool(self.site and self.email and self.token)

    def client(self):
        return JiraClient(self.site, self.email, self.token)


# ── 마크다운 → wiki markup ─────────────────────────────────────
_INLINE = [
    (re.compile(r'\*\*(.+?)\*\*'), r'*\1*'),          # 굵게
    (re.compile(r'`([^`]+)`'), r'{{\1}}'),            # 코드
]


def _inline(s):
    for pat, rep in _INLINE:
        s = pat.sub(rep, s)
    return s


def md_to_wiki(md, attach_names=None):
    """결과 문서(.md)를 Jira wiki markup 으로. '읽히게' 옮기는 것이 목표다.

    - 첨부/x.png · 캡처/y.png 줄은 그 이슈에 올린 첨부를 가리키게 바꾼다:
      그림은 !name|thumbnail! (인라인 미리보기), 동영상·기타는 [^name].
    - 코드블록 안은 치환하지 않는다. 표(|)는 그대로 둔다(Jira 도 | 표를 읽는다)."""
    attach_names = set(attach_names or [])
    out = []
    in_code = False
    quote = []

    def flush_quote():
        if quote:
            out.append('{quote}')
            out.extend(quote)
            out.append('{quote}')
            del quote[:]

    for raw in (md or u'').splitlines():
        line = raw.rstrip('\n')
        if line.strip().startswith('```'):
            flush_quote()
            out.append('{code}')
            in_code = not in_code
            continue
        if in_code:
            out.append(line)
            continue
        if line.startswith('> '):
            quote.append(_inline(line[2:]))
            continue
        flush_quote()
        m = re.match(r'^(#{1,6})\s+(.*)$', line)
        if m:
            out.append('h%d. %s' % (min(len(m.group(1)), 6), _inline(m.group(2))))
            continue
        if line.strip() == '---':
            out.append('----')
            continue
        m = re.match(r'^(\s*)- (.*)$', line)
        if m:
            depth = len(m.group(1)) // 2 + 1
            body = m.group(2)
            am = re.match(r'^(첨부|캡처): (첨부|캡처)/(\S+)(.*)$', body)
            if am:
                name = am.group(3)
                tail = am.group(4) or u''
                ext = os.path.splitext(name)[1].lower()
                if ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
                    ref = '!%s|thumbnail!' % name
                else:
                    ref = '[^%s]' % name
                body = u'%s: %s%s' % (am.group(1), ref, tail)
            out.append('%s %s' % ('*' * depth, _inline(body)))
            continue
        m = re.match(r'^(\s*)(\d+)\. (.*)$', line)
        if m:
            depth = len(m.group(1)) // 3 + 1
            out.append('%s %s' % ('#' * depth, _inline(m.group(3))))
            continue
        if line.startswith('  ') and out and (out[-1].startswith('*') or out[-1].startswith('#')):
            out[-1] += u' ' + _inline(line.strip())     # 목록 항목의 이어지는 줄
            continue
        out.append(_inline(line))
    flush_quote()
    if in_code:
        out.append('{code}')
    return u'\n'.join(out)


def summary_of(md, fallback=u'화면 주석'):
    """이슈 제목: 문서 첫 제목 줄에서. '# 화면 주석 - 2026-09-11 10:00' → 그대로."""
    for line in (md or u'').splitlines():
        s = line.strip()
        if s.startswith('#'):
            return s.lstrip('#').strip()[:200] or fallback
    return fallback


# ── 클라이언트 ─────────────────────────────────────────────────
class JiraClient(object):
    def __init__(self, site, email, token, timeout=60):
        self.site = (site or DEFAULT_SITE).rstrip('/')
        self.email = email or u''
        self.token = token or u''
        self.timeout = timeout
        # 사내 프록시가 127.0.0.1 만 막는 것과 달리 외부는 시스템 프록시를 그대로 탄다.
        self._opener = urllib.request.build_opener()

    def _auth(self):
        raw = (u'%s:%s' % (self.email, self.token)).encode('utf-8')
        return 'Basic ' + base64.b64encode(raw).decode('ascii')

    def _request(self, method, path, body=None, headers=None, raw=False):
        url = self.site + path
        data = None
        hdr = {'Authorization': self._auth(), 'Accept': 'application/json'}
        if body is not None and not raw:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            hdr['Content-Type'] = 'application/json'
        elif raw:
            data = body
        hdr.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=hdr)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                text = r.read().decode('utf-8', 'replace')
                return r.status, (json.loads(text) if text.strip() else {})
        except urllib.error.HTTPError as e:
            text = u''
            try:
                text = e.read().decode('utf-8', 'replace')
            except Exception:
                pass
            raise JiraError(e.code, self._message(e.code, text))
        except urllib.error.URLError as e:
            raise JiraError(0, u'연결 실패: %s' % (e.reason,))

    @staticmethod
    def _message(code, text):
        try:
            j = json.loads(text)
            msgs = list(j.get('errorMessages') or [])
            for k, v in (j.get('errors') or {}).items():
                msgs.append(u'%s: %s' % (k, v))
            if msgs:
                return u' / '.join(str(m) for m in msgs)[:500]
        except Exception:
            pass
        if code == 401:
            return u'인증 실패 - 이메일 또는 API 토큰이 맞지 않습니다'
        if code == 403:
            return u'권한 없음 - 이 프로젝트에 쓸 수 없는 계정입니다'
        if code == 404:
            return u'없는 이슈 또는 프로젝트입니다'
        return (text or u'').strip()[:300] or (u'HTTP %s' % code)

    # ── 조회 ──
    def myself(self):
        _s, j = self._request('GET', '/rest/api/2/myself')
        return j

    def server_info(self):
        _s, j = self._request('GET', '/rest/api/2/serverInfo')
        return j

    def attachment_meta(self):
        """{'enabled': bool, 'uploadLimit': bytes}"""
        _s, j = self._request('GET', '/rest/api/2/attachment/meta')
        return j

    def issue(self, key):
        _s, j = self._request('GET', '/rest/api/2/issue/%s?fields=summary,project,issuetype'
                              % urllib.parse.quote(key))
        return j

    def projects(self):
        _s, j = self._request('GET', '/rest/api/2/project?expand=issueTypes')
        return j if isinstance(j, list) else []

    def creatable(self):
        """이 계정이 **이슈를 만들 수 있는** 프로젝트와 그 이슈 유형(하위 작업 제외).

        /project 는 볼 수 있는 프로젝트 전부(90여 개)를 주지만 그중 생성 권한이 있는 곳은 일부다
        (TEST 프로젝트도 403 이었다). createmeta 는 생성 가능한 것만 준다 - 보내기 창의 목록은 이것.
        돌려주는 값: [{'key', 'name', 'types': [이름…], 'required': {유형: [필수필드 이름…]}}] (키 순)"""
        _s, j = self._request('GET', '/rest/api/2/issue/createmeta?expand=projects.issuetypes.fields')
        out = []
        for pj in (j.get('projects') or []):
            types, required = [], {}
            for it in pj.get('issuetypes') or []:
                if it.get('subtask'):
                    continue
                name = (it.get('name') or u'').strip()
                if not name:
                    continue
                types.append(name)
                req = [v.get('name') or k for k, v in (it.get('fields') or {}).items()
                       if v.get('required') and not v.get('hasDefaultValue')
                       and k not in ('project', 'issuetype', 'summary', 'description')]
                if req:
                    required[name] = req
            out.append({'key': pj.get('key'), 'name': pj.get('name') or u'', 'types': types, 'required': required})
        out.sort(key=lambda d: d['key'] or u'')
        return out

    # ── 쓰기 ──
    def create_issue(self, project_key, issue_type, summary, description):
        body = {'fields': {'project': {'key': project_key}, 'issuetype': {'name': issue_type},
                           'summary': summary, 'description': description}}
        _s, j = self._request('POST', '/rest/api/2/issue', body)
        return j.get('key'), j.get('id')

    def add_comment(self, key, body_text):
        _s, j = self._request('POST', '/rest/api/2/issue/%s/comment' % urllib.parse.quote(key),
                              {'body': body_text})
        return j.get('id')

    def attach(self, key, path):
        """파일 하나를 올린다(multipart 를 직접 조립). 올라간 첨부의 파일 이름."""
        boundary = uuid.uuid4().hex
        name = os.path.basename(path)
        ctype = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        with open(path, 'rb') as f:
            data = f.read()
        ascii_name = re.sub(r'[^A-Za-z0-9._-]', '_', name) or 'file'
        head = (u'--%s\r\n'
                u'Content-Disposition: form-data; name="file"; filename="%s"; '
                u"filename*=UTF-8''%s\r\n"
                u'Content-Type: %s\r\n\r\n'
                % (boundary, ascii_name, urllib.parse.quote(name), ctype)).encode('utf-8')
        tail = (u'\r\n--%s--\r\n' % boundary).encode('utf-8')
        body = head + data + tail
        _s, j = self._request('POST', '/rest/api/2/issue/%s/attachments' % urllib.parse.quote(key),
                              body, raw=True,
                              headers={'Content-Type': 'multipart/form-data; boundary=%s' % boundary,
                                       'X-Atlassian-Token': 'no-check'})
        if isinstance(j, list) and j:
            return j[0].get('filename') or name
        return name

    def issue_url(self, key):
        return u'%s/browse/%s' % (self.site, key)


def send(cfg, mode, md_text, files, project=None, issue_type=None, issue_key=None,
         summary=None, log=None, upload_limit=None):
    """새 이슈(mode='new') 또는 기존 이슈 댓글(mode='comment') 로 문서+파일을 올린다.

    돌려주는 값: {'key', 'url', 'uploaded': [name…], 'skipped': [(name, 이유)…], 'comment_id'}
    ★부분 성공을 숨기지 않는다. 새 이슈는 만들자마자 key 를 돌려주므로(예외에도 result 에 담아
      raise), 첨부 실패 시 재시도는 **그 이슈에 첨부만** 다시 하면 된다(중복 생성 금지)."""
    log = log or (lambda m: None)
    cli = cfg.client()
    files = list(files or [])
    names = [os.path.basename(p) for p in files]
    wiki = md_to_wiki(md_text, names)
    result = {'key': None, 'url': None, 'uploaded': [], 'skipped': [], 'comment_id': None}
    if upload_limit is None:
        try:
            upload_limit = int((cli.attachment_meta() or {}).get('uploadLimit') or 0) or None
        except Exception:
            upload_limit = None

    def upload_all(key):
        for p in files:
            name = os.path.basename(p)
            try:
                size = os.path.getsize(p)
            except Exception:
                result['skipped'].append((name, u'파일 없음'))
                continue
            if upload_limit and size > upload_limit:
                result['skipped'].append((name, u'사이트 상한 %dMB 초과' % (upload_limit // 1048576)))
                continue
            try:
                cli.attach(key, p)
                result['uploaded'].append(name)
                log(u'첨부 올림 - %s' % name)
            except JiraError as e:
                result['skipped'].append((name, e.message))

    if mode == 'new':
        key, _id = cli.create_issue(project or cfg.project, issue_type or cfg.issue_type,
                                    summary or summary_of(md_text), wiki)
        result['key'] = key
        result['url'] = cli.issue_url(key)
        log(u'이슈 생성 - %s' % key)
        upload_all(key)
    else:
        key = (issue_key or u'').strip()
        result['key'] = key
        result['url'] = cli.issue_url(key)
        upload_all(key)                     # 댓글이 첨부를 가리키므로 먼저 올린다
        result['comment_id'] = cli.add_comment(key, wiki)
        log(u'댓글 등록 - %s' % key)
    return result
