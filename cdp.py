# -*- coding: utf-8 -*-
"""Chrome DevTools Protocol 클라이언트 - 표준 라이브러리만 쓴다.

websocket-client 를 쓰지 않는 이유: 이 프로그램은 파이썬도 없는 다른 PC 에서
단일 exe 로 돌아야 한다. 의존이 하나라도 붙으면 그 PC 에서 재현이 깨진다.
그래서 RFC6455 클라이언트 프레이밍을 필요한 만큼만(텍스트 프레임·마스킹·
분할 프레임·ping/pong·close) 직접 구현한다.
"""
import base64
import json
import os
import socket
import struct
import threading


class WSError(Exception):
    pass


class WebSocket(object):
    """CDP 용 최소 WebSocket 클라이언트. 텍스트 프레임만 주고받는다."""

    def __init__(self, url, timeout=30.0):
        if not url.startswith('ws://'):
            raise WSError('ws:// 주소만 지원합니다: %s' % url)
        rest = url[5:]
        netloc, _, path = rest.partition('/')
        host, _, port = netloc.partition(':')
        self._sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self._sock.settimeout(timeout)
        self._buf = b''
        self._lock = threading.Lock()
        self._handshake(host, port, '/' + path)
        # ★핸드셰이크가 끝나면 타임아웃을 없앤다.
        #   그대로 두면 QA 가 브라우저를 30초만 가만히 둬도 수신이 timed out 으로
        #   끊겨 그 뒤 주석이 수집되지 않는다(실측). 명령별 응답 대기는 소켓
        #   타임아웃이 아니라 call() 의 Event 로 따로 잡는다. 닫을 때는 소켓을
        #   직접 닫으므로 블로킹 recv 도 즉시 풀린다.
        self._sock.settimeout(None)

    # ── 핸드셰이크 ──────────────────────────────────────────────
    def _handshake(self, host, port, path):
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        req = (
            'GET %s HTTP/1.1\r\n'
            'Host: %s:%s\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            'Sec-WebSocket-Key: %s\r\n'
            'Sec-WebSocket-Version: 13\r\n'
            '\r\n' % (path, host, port or '80', key)
        )
        self._sock.sendall(req.encode('ascii'))
        head = b''
        while b'\r\n\r\n' not in head:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WSError('핸드셰이크 중 연결이 끊겼습니다.')
            head += chunk
        line = head.split(b'\r\n', 1)[0].decode('latin-1')
        if '101' not in line:
            raise WSError('업그레이드 거부: %s' % line)
        self._buf = head.split(b'\r\n\r\n', 1)[1]

    # ── 저수준 입출력 ───────────────────────────────────────────
    def _read(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WSError('연결이 끊겼습니다.')
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode, payload):
        head = struct.pack('!B', 0x80 | opcode)
        n = len(payload)
        if n < 126:
            head += struct.pack('!B', 0x80 | n)
        elif n < (1 << 16):
            head += struct.pack('!BH', 0x80 | 126, n)
        else:
            head += struct.pack('!BQ', 0x80 | 127, n)
        mask = os.urandom(4)
        masked = bytearray(payload)
        for i in range(n):
            masked[i] ^= mask[i & 3]
        with self._lock:
            self._sock.sendall(head + mask + bytes(masked))

    def send(self, text):
        self._send_frame(0x1, text.encode('utf-8'))

    def recv(self):
        """텍스트 메시지 하나를 문자열로 돌려준다. close 프레임이면 None."""
        data = b''
        opcode = None
        while True:
            b1, b2 = struct.unpack('!BB', self._read(2))
            fin = b1 & 0x80
            op = b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack('!H', self._read(2))[0]
            elif n == 127:
                n = struct.unpack('!Q', self._read(8))[0]
            payload = self._read(n) if n else b''

            if op == 0x8:                       # close
                return None
            if op == 0x9:                       # ping → pong
                self._send_frame(0xA, payload)
                continue
            if op == 0xA:                       # pong
                continue
            if op in (0x1, 0x2):
                opcode = op
                data = payload
            elif op == 0x0:                     # continuation
                data += payload
            if fin:
                if opcode == 0x1:
                    return data.decode('utf-8', 'replace')
                data, opcode = b'', None        # 바이너리는 버린다(CDP 는 텍스트만 쓴다)

    def close(self):
        try:
            self._send_frame(0x8, b'')
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


class CDP(object):
    """CDP 세션 다중화. 브라우저 하나에 붙어 여러 타깃(sessionId)을 함께 다룬다."""

    def __init__(self, ws_url, on_log=None):
        self.ws = WebSocket(ws_url)
        self._id = 0
        self._id_lock = threading.Lock()
        self._pending = {}                      # id -> (Event, [result])
        self._handlers = {}                     # method -> [cb(params, session_id)]
        self._on_log = on_log or (lambda m: None)
        self.closed = False
        self._reader = threading.Thread(target=self._pump, name='cdp-reader')
        self._reader.daemon = True
        self._reader.start()

    # ── 이벤트 구독 ────────────────────────────────────────────
    def on(self, method, cb):
        self._handlers.setdefault(method, []).append(cb)

    def _pump(self):
        while not self.closed:
            try:
                raw = self.ws.recv()
            except socket.timeout:
                continue                        # 방어용 - 정상 유휴는 끊지 않는다
            except Exception as e:
                if not self.closed:
                    self._on_log('CDP 수신 종료: %s' % e)
                break
            if raw is None:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mid = msg.get('id')
            if mid is not None:
                slot = self._pending.pop(mid, None)
                if slot:
                    slot[1].append(msg)
                    slot[0].set()
                continue
            method = msg.get('method')
            for cb in self._handlers.get(method, []):
                try:
                    cb(msg.get('params') or {}, msg.get('sessionId'))
                except Exception as e:
                    self._on_log('이벤트 처리 실패 %s: %s' % (method, e))
        self.closed = True

    # ── 명령 ──────────────────────────────────────────────────
    def call(self, method, params=None, session_id=None, timeout=20.0, wait=True):
        with self._id_lock:
            self._id += 1
            mid = self._id
        msg = {'id': mid, 'method': method}
        if params:
            msg['params'] = params
        if session_id:
            msg['sessionId'] = session_id
        ev = threading.Event()
        if wait:
            self._pending[mid] = (ev, [])
        self.ws.send(json.dumps(msg))
        if not wait:
            return None
        slot = self._pending.get(mid)
        if not ev.wait(timeout):
            self._pending.pop(mid, None)
            raise WSError('%s 응답 시간 초과' % method)
        res = slot[1][0]
        if 'error' in res:
            raise WSError('%s 실패: %s' % (method, res['error'].get('message')))
        return res.get('result') or {}

    def close(self):
        self.closed = True
        self.ws.close()
