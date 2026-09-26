# -*- coding: utf-8 -*-
"""회차(round) 단위 SQLite 저장소. 표준 라이브러리 sqlite3 만 쓴다(배포 PC 에 파이썬이 없다).

왜 SQLite 인가 - 형식 때문이 아니라 **구조** 때문이다.
  실사용 불만 "이전 내역을 찾아가면 값이 변한다·오염된다" 의 원인은 jsonl 이 아니었다.
    ① store.apply() 가 주석을 id 로 통째 덮어써서 나중 값만 남고,
    ② load_archive() 가 옛 jsonl 을 현재 뒤에 이어붙여 총평·순서·meta 가 옛 값으로 덮이고,
    ③ skip(되돌리기)이 '줄 번호' 라 이어붙이면 엉뚱한 줄을 무시했다.
  그래서 회차를 분리하고(지난 회차는 읽기 전용), 주석은 덮어쓰지 않고 버전을 append 하며,
  되돌리기는 줄 번호가 아니라 event id 로 가리킨다.

진실은 하나 - `events`. 화면·메타·순서·첨부·캡처는 전부 events 를 재생해서 만든다(옛 jsonl 과 같은
방식). 별도 테이블은 재생으로 얻기 비싼 것만 둔다: rounds(회차), skip(무시할 event), annotation_versions
(주석 버전 - '변경 이력' 과 지난 회차 재지적 대조에 쓴다).

★모든 접근은 Store.lock 안에서 일어난다(check_same_thread=False 는 그 전제 위에서만 안전하다).
"""
import io
import json
import os
import re
import shutil
import sqlite3
import threading
from datetime import datetime

SCHEMA = 1


class ReadOnlyRound(Exception):
    """닫힌 회차에 쓰려 했다. 조용히 무시하지 않는다 - 무시하면 화면과 기록이 어긋난 채 지나간다."""


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _page_id(url):
    """출처 + 경로(질의문자열·해시 제외). store._page_id 와 같은 규칙 - clear 의 범위다."""
    try:
        from urllib.parse import urlsplit
        u = urlsplit(url or '')
        return '%s://%s%s' % (u.scheme, u.netloc, u.path)
    except Exception:
        return (url or '').split('?')[0].split('#')[0]


def _roaming(path):
    """네트워크·로밍 경로면 WAL 을 쓰지 않는다(SMB 위 WAL 은 손상 사례가 있다)."""
    p = os.path.abspath(path).replace('/', '\\')
    if p.startswith('\\\\'):
        return True
    appdata = os.environ.get('APPDATA') or ''
    local = os.environ.get('LOCALAPPDATA') or ''
    if appdata and p.lower().startswith(appdata.lower()) and not (
            local and p.lower().startswith(local.lower())):
        return True
    return False


class DB(object):
    def __init__(self, path):
        self.path = path
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        self.lock = threading.RLock()
        # isolation_level=None: 자동 커밋. 여러 문장을 묶을 때만 BEGIN/COMMIT 을 직접 쓴다.
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute('PRAGMA journal_mode=%s' % ('DELETE' if _roaming(path) else 'WAL'))
        except Exception:
            pass
        try:
            self.conn.execute('PRAGMA synchronous=NORMAL')
        except Exception:
            pass
        self._schema()

    def _schema(self):
        c = self.conn
        c.executescript('''
            CREATE TABLE IF NOT EXISTS schema_version(
                v INTEGER NOT NULL, migrated_at TEXT);
            CREATE TABLE IF NOT EXISTS rounds(
                id INTEGER PRIMARY KEY, seq INTEGER NOT NULL, label TEXT,
                started_at TEXT, closed_at TEXT, status TEXT NOT NULL,
                export_path TEXT, closed_reason TEXT, source TEXT,
                n_pages INTEGER, n_anns INTEGER);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, round_id INTEGER NOT NULL, t TEXT NOT NULL,
                ts TEXT, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_events_round ON events(round_id, id);
            CREATE TABLE IF NOT EXISTS skip(
                round_id INTEGER NOT NULL, event_id INTEGER NOT NULL,
                PRIMARY KEY(round_id, event_id));
            CREATE TABLE IF NOT EXISTS annotation_versions(
                id INTEGER PRIMARY KEY, round_id INTEGER NOT NULL, aid TEXT NOT NULL,
                seq INTEGER NOT NULL, kind TEXT NOT NULL, url TEXT, viewport TEXT,
                payload TEXT, received_at TEXT, event_id INTEGER,
                UNIQUE(round_id, aid, seq));
            CREATE INDEX IF NOT EXISTS ix_ver_round ON annotation_versions(round_id, aid);
        ''')
        row = c.execute('SELECT v FROM schema_version').fetchone()
        if row is None:
            c.execute('INSERT INTO schema_version(v) VALUES (?)', (SCHEMA,))

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    # ── 회차 ──────────────────────────────────────────────────
    def current_round(self):
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()

    def round(self, rid):
        with self.lock:
            return self.conn.execute('SELECT * FROM rounds WHERE id=?', (rid,)).fetchone()

    def list_rounds(self, status=None):
        with self.lock:
            if status:
                return self.conn.execute(
                    'SELECT * FROM rounds WHERE status=? ORDER BY seq, id', (status,)).fetchall()
            return self.conn.execute('SELECT * FROM rounds ORDER BY seq, id').fetchall()

    def next_seq(self):
        with self.lock:
            row = self.conn.execute('SELECT MAX(seq) AS m FROM rounds').fetchone()
            return int(row['m'] or 0) + 1

    def open_round(self, label, seq=None, source='live', started_at=None):
        """새 회차를 연다. 열린 회차가 이미 있으면 그것을 돌려준다(둘이 열리지 않게)."""
        with self.lock:
            cur = self.current_round()
            if cur is not None:
                return cur
            if seq is None:
                seq = self.next_seq()
            self.conn.execute(
                'INSERT INTO rounds(seq, label, started_at, status, source) VALUES (?,?,?,?,?)',
                (seq, label or '', started_at or _now(), 'open', source))
            return self.current_round()

    def close_round(self, rid, reason='', export_path='', n_pages=None, n_anns=None,
                    closed_at=None):
        with self.lock:
            self.conn.execute(
                "UPDATE rounds SET status='closed', closed_at=?, closed_reason=?, export_path=?, "
                "n_pages=?, n_anns=? WHERE id=?",
                (closed_at or _now(), reason or '', export_path or '', n_pages, n_anns, rid))

    def set_round_counts(self, rid, n_pages, n_anns):
        with self.lock:
            self.conn.execute('UPDATE rounds SET n_pages=?, n_anns=? WHERE id=?',
                              (n_pages, n_anns, rid))

    # ── 이벤트 ────────────────────────────────────────────────
    def add_event(self, rid, rec, ts=None):
        """이벤트 하나를 붙이고 id 를 돌려준다. 주석 이벤트면 버전도 함께 남긴다."""
        with self.lock:
            cur = self.conn.execute(
                'INSERT INTO events(round_id, t, ts, payload) VALUES (?,?,?,?)',
                (rid, rec.get('t') or '', ts or _now(), json.dumps(rec, ensure_ascii=False)))
            eid = cur.lastrowid
            if rec.get('t') == 'annotation':
                self._add_versions(rid, eid, rec.get('payload') or {}, ts)
            return eid

    def events(self, rid, with_skipped=False):
        """[(event_id, rec)] - id 순. 기본으로 skip 된 것은 뺀다."""
        with self.lock:
            rows = self.conn.execute(
                'SELECT id, payload FROM events WHERE round_id=? ORDER BY id', (rid,)).fetchall()
            skipped = set() if with_skipped else self.skips(rid)
        out = []
        for r in rows:
            if r['id'] in skipped:
                continue
            try:
                out.append((r['id'], json.loads(r['payload'])))
            except Exception:
                continue
        return out

    def skips(self, rid):
        with self.lock:
            return set(r['event_id'] for r in self.conn.execute(
                'SELECT event_id FROM skip WHERE round_id=?', (rid,)).fetchall())

    def add_skip(self, rid, eid):
        with self.lock:
            self.conn.execute('INSERT OR IGNORE INTO skip(round_id, event_id) VALUES (?,?)',
                              (rid, eid))

    # ── 주석 버전 ─────────────────────────────────────────────
    def _next_ver(self, rid, aid):
        row = self.conn.execute(
            'SELECT MAX(seq) AS m FROM annotation_versions WHERE round_id=? AND aid=?',
            (rid, aid)).fetchone()
        return int(row['m'] or 0) + 1

    def _alive_aids(self, rid):
        """이 회차에서 지금 살아 있는 aid -> url (마지막 버전이 삭제·tombstone 이 아닌 것)."""
        rows = self.conn.execute(
            'SELECT aid, kind, url FROM annotation_versions WHERE round_id=? ORDER BY id',
            (rid,)).fetchall()
        alive = {}
        for r in rows:
            if r['kind'] in ('delete', 'clear'):
                alive.pop(r['aid'], None)
            else:
                alive[r['aid']] = r['url'] or ''
        return alive

    def _add_versions(self, rid, eid, payload, ts):
        """★덮어쓰지 않는다. 주석마다 (round, aid, seq+1) 행을 붙인다.
        delete 는 그 aid 에, clear 는 같은 경로의 살아 있는 모든 aid 에 tombstone 을 남긴다."""
        kind = payload.get('kind') or 'add'
        url = payload.get('url') or ''
        vp = payload.get('viewport') or ''
        anns = payload.get('annotations') or []
        when = ts or _now()
        if kind == 'clear':
            pid = _page_id(url)
            targets = [aid for aid, u in self._alive_aids(rid).items() if _page_id(u) == pid]
            for aid in targets:
                self.conn.execute(
                    'INSERT INTO annotation_versions(round_id, aid, seq, kind, url, viewport, '
                    'payload, received_at, event_id) VALUES (?,?,?,?,?,?,?,?,?)',
                    (rid, aid, self._next_ver(rid, aid), 'clear', url, vp, None, when, eid))
            return
        for a in anns:
            aid = a.get('id')
            if not aid:
                continue
            self.conn.execute(
                'INSERT INTO annotation_versions(round_id, aid, seq, kind, url, viewport, '
                'payload, received_at, event_id) VALUES (?,?,?,?,?,?,?,?,?)',
                (rid, str(aid), self._next_ver(rid, str(aid)), kind, url, vp,
                 None if kind == 'delete' else json.dumps(a, ensure_ascii=False), when, eid))

    def versions_of(self, rid, aid):
        """[{seq, kind, at, comment, element, url, viewport}] - 오래된 것부터."""
        with self.lock:
            rows = self.conn.execute(
                'SELECT seq, kind, url, viewport, payload, received_at, event_id '
                'FROM annotation_versions WHERE round_id=? AND aid=? ORDER BY seq',
                (rid, str(aid))).fetchall()
            skipped = self.skips(rid)
        out = []
        for r in rows:
            a = {}
            if r['payload']:
                try:
                    a = json.loads(r['payload'])
                except Exception:
                    a = {}
            out.append({'seq': r['seq'], 'kind': r['kind'], 'at': r['received_at'] or '',
                        'comment': a.get('comment') or '', 'element': a.get('element') or '',
                        'url': r['url'] or '', 'viewport': r['viewport'] or '',
                        'undone': r['event_id'] in skipped})
        return out

    def latest_spots(self, rid):
        """지난 회차 대조용 - 살아 있는 aid 의 (주소, elementPath). 주소는 store 쪽이 정규화한다."""
        with self.lock:
            rows = self.conn.execute(
                'SELECT aid, kind, url, payload, event_id FROM annotation_versions '
                'WHERE round_id=? ORDER BY id', (rid,)).fetchall()
            skipped = self.skips(rid)
        alive = {}
        for r in rows:
            if r['event_id'] in skipped:
                continue
            if r['kind'] in ('delete', 'clear'):
                alive.pop(r['aid'], None)
                continue
            try:
                a = json.loads(r['payload'] or '{}')
            except Exception:
                a = {}
            if a.get('elementPath'):
                alive[r['aid']] = (r['url'] or '', a['elementPath'])
        return list(alive.values())

    # ── 마이그레이션 표식 ─────────────────────────────────────
    def migrated_at(self):
        with self.lock:
            row = self.conn.execute('SELECT migrated_at FROM schema_version').fetchone()
            return row['migrated_at'] if row else None

    def mark_migrated(self):
        with self.lock:
            self.conn.execute('UPDATE schema_version SET migrated_at=?', (_now(),))


# ── 옛 jsonl 읽기 (마이그레이션 · 가져오기 공용) ───────────────
def read_jsonl(path):
    """물리 줄 그대로 - 빈 줄·깨진 줄은 None. 옛 skip 이 '줄 번호' 라 줄을 건너뛰면 안 된다."""
    out = []
    with io.open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                out.append(None)
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                out.append(None)
    return out


def _stamp_of(name):
    """'20260821-114544-annotations.jsonl' -> ('2026-08-21 11:45:44', '20260821-114544')"""
    m = re.match(r'(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})', name or '')
    if not m:
        return (None, None)
    y, mo, d, h, mi, s = m.groups()
    stem = name[:-len('-annotations.jsonl')] if name.endswith('-annotations.jsonl') else name
    return ('%s-%s-%s %s:%s:%s' % (y, mo, d, h, mi, s), stem)


def _insert_round(db, recs, label, status, source, seq, started_at, closed_at, reason):
    """회차 행 + 이벤트 + skip 을 넣는다. 트랜잭션은 부르는 쪽이 연다."""
    c = db.conn
    if seq is None:
        seq = db.next_seq()
    c.execute(
        'INSERT INTO rounds(seq, label, started_at, closed_at, status, source, '
        'closed_reason, export_path) VALUES (?,?,?,?,?,?,?,?)',
        (seq, label or '', started_at or _now(), closed_at if status == 'closed' else None,
         status, source, reason, ''))
    rid = c.execute('SELECT last_insert_rowid() AS i').fetchone()['i']
    line_to_eid = {}
    skips = []
    n = 0
    for i, rec in enumerate(recs):
        if rec is None:
            continue
        if rec.get('t') == 'skip':
            try:
                skips.append(int(rec.get('index')))
            except Exception:
                pass
            # skip 줄 자체도 events 에 남긴다(원본 보존) - 재생에서는 모르는 t 로 무시된다.
        ts = None
        pl = rec.get('payload') or {}
        if rec.get('t') == 'annotation' and pl.get('ts'):
            ts = str(pl.get('ts')).replace('T', ' ')[:19]
        eid = db.add_event(rid, rec, ts=ts)
        line_to_eid[i] = eid
        n += 1
    for ln in skips:
        eid = line_to_eid.get(ln)
        if eid is not None:
            c.execute('INSERT OR IGNORE INTO skip(round_id, event_id) VALUES (?,?)', (rid, eid))
    return rid, n


def _copy_attach(attach_from, attach_root, rid):
    """첨부 파일을 회차 폴더로 복사한다(DB 커밋 뒤에). 복사한 개수."""
    copied = 0
    if not (attach_from and attach_root and os.path.isdir(attach_from)):
        return 0
    dst_dir = os.path.join(attach_root, 'r%d' % rid)
    if not os.path.isdir(dst_dir):
        os.makedirs(dst_dir)
    for fn in os.listdir(attach_from):
        src = os.path.join(attach_from, fn)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(dst_dir, fn)
        if not os.path.exists(dst):
            try:
                shutil.copyfile(src, dst)
                copied += 1
            except Exception:
                pass
    return copied


def import_jsonl(db, path, label, status='closed', source='import', seq=None,
                 attach_from=None, attach_root=None, started_at=None, closed_at=None,
                 reason=''):
    """jsonl 하나를 회차 하나로 넣는다. (round_id, 넣은 이벤트 수, 옮긴 첨부 수).

    ★한 트랜잭션. 중간에 실패하면 회차 자체가 생기지 않는다.
    ★옛 {'t':'skip','index':N} 은 물리 줄 번호다 - 줄 번호 → event id 표로 환산해 skip 테이블에 넣는다.
    첨부 파일은 DB 커밋 **뒤에** 복사한다(복사가 실패해도 DB 는 유효하고, 다음에 다시 시도할 수 있다)."""
    recs = read_jsonl(path)
    with db.lock:
        c = db.conn
        c.execute('BEGIN')
        try:
            rid, n = _insert_round(db, recs, label, status, source, seq, started_at, closed_at,
                                   reason)
            c.execute('COMMIT')
        except Exception:
            c.execute('ROLLBACK')
            raise
    return rid, n, _copy_attach(attach_from, attach_root, rid)


def migrate_legacy(db, out_dir, export_seq=0, label='', log=None):
    """1.1 이하의 out/ (annotations.jsonl + archive/) 를 회차로 옮긴다. 한 번만.

    조건: schema_version.migrated_at 이 비어 있고, 옮길 파일이 하나라도 있을 때.
    ★전체가 한 트랜잭션이다. 실패하면 회차가 하나도 생기지 않고 원본 이름도 바꾸지 않는다
      → 다음 실행에 다시 시도한다(중간까지 들어간 회차가 남아 재시도 때 겹치는 일이 없다).
    성공하면 원본은 지우지 않고 이름만 바꾼다(annotations.jsonl.migrated · archive.migrated/).
    첨부 복사는 커밋 뒤에 한다.
    돌려주는 값: {'rounds': n, 'events': n, 'attach': n} 또는 None(할 일이 없었다)."""
    log = log or (lambda m: None)
    if db.migrated_at():
        return None
    jsonl = os.path.join(out_dir, 'annotations.jsonl')
    arch = os.path.join(out_dir, 'archive')
    attach_root = os.path.join(out_dir, 'attach')
    names = []
    if os.path.isdir(arch):
        names = sorted(n for n in os.listdir(arch) if n.endswith('-annotations.jsonl'))
    if not names and not os.path.exists(jsonl):
        db.mark_migrated()                      # 옮길 것이 없다 - 새 PC
        return None
    summary = {'rounds': 0, 'events': 0, 'attach': 0}
    copies = []                                 # (attach_from, rid) - 커밋 뒤에
    renames = []                                # (원본, 바꿀 이름) - 전부 성공한 뒤에만
    with db.lock:
        c = db.conn
        c.execute('BEGIN')
        try:
            seq = 0
            for name in names:
                when, stem = _stamp_of(name)
                seq += 1
                rid, n = _insert_round(db, read_jsonl(os.path.join(arch, name)), label,
                                       'closed', 'legacy', seq, when, when, '1.1 archive')
                summary['rounds'] += 1
                summary['events'] += n
                if stem:
                    copies.append((os.path.join(arch, stem + '-attach'), rid))
                log('지난 회차를 옮겼습니다 - %s → 회차 %d (이벤트 %d건)' % (name, seq, n))
            if os.path.exists(jsonl):
                seq = max(seq, int(export_seq or 0)) + 1
                rid, n = _insert_round(db, read_jsonl(jsonl), label, 'open', 'legacy', seq,
                                       None, None, '')
                summary['rounds'] += 1
                summary['events'] += n
                copies.append((attach_root, rid))       # 1.1 의 평평한 attach/ 파일들
                log('현재 기록을 옮겼습니다 - 회차 %d (이벤트 %d건)' % (seq, n))
                renames.append((jsonl, jsonl + '.migrated'))
            if names:
                renames.append((arch, arch + '.migrated'))
            db.mark_migrated()
            c.execute('COMMIT')
        except Exception:
            c.execute('ROLLBACK')
            raise
    for src, rid in copies:
        summary['attach'] += _copy_attach(src, attach_root, rid)
    for src, dst in renames:
        try:
            if os.path.exists(dst):
                dst = dst + datetime.now().strftime('.%Y%m%d%H%M%S')
            os.replace(src, dst)
        except Exception as e:
            log('원본 이름 바꾸기 실패(무시 가능): %s' % e)
    return summary
