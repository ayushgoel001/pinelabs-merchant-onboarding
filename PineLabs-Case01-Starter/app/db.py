"""SQLite configuration, schema and transaction ownership."""
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

PROVIDERS = ("PAN", "GST", "BANK", "RISK")

# Per-run timing (review I8/I9, M3 review I3). Every profile keeps the slowest normal provider reply at or below
# half the attempt timeout, so a loaded laptop cannot turn a happy path into expiries. "demo" is the live default
# (replies at 1/2/3/4 s against 10 s). "reference" keeps the 2/4/6/8 s timeline used by the deterministic
# injected-clock tests. "fast" drives the real-process tests.
TIMING = {
    "demo": dict(attempt_timeout_s=10.0, backoff_s=[2.0, 5.0], dispatch_retry_s=1.0, lease_s=15.0,
                 http_timeout_s=5.0, delay_scale=0.5, tick_s=0.5),
    "reference": dict(attempt_timeout_s=10.0, backoff_s=[2.0, 5.0], dispatch_retry_s=1.0, lease_s=15.0,
                      http_timeout_s=5.0, delay_scale=1.0, tick_s=0.5),
    "fast": dict(attempt_timeout_s=2.0, backoff_s=[0.2, 0.5], dispatch_retry_s=0.2, lease_s=3.0,
                 http_timeout_s=1.0, delay_scale=0.1, tick_s=0.1),
}


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def connect(path, shared=False):
    conn = sqlite3.connect(path, isolation_level=None, timeout=5, check_same_thread=not shared)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


# SQLite's busy handler is not fair: with many threads contending for the single writer lock, some starve until the
# busy timeout expires and fail even though the database is healthy (found by the M4-B stress test). Inside one
# process, writers therefore queue on a FIFO lock, so each waits its turn in arrival order and only one at a time
# contends with other processes (the worker). Python's own locks are not FIFO, and an unfair queue starved the
# provider callbacks behind a flood of replays, so the lock below hands over strictly in order.
WRITER_QUEUE_TIMEOUT_S = 30


class FairLock:
    """Re-entrant lock that grants ownership in arrival order."""
    def __init__(self):
        self._cond = threading.Condition()
        self._waiting = []
        self._owner = None
        self._depth = 0

    def acquire(self, timeout):
        me = threading.get_ident()
        with self._cond:
            if self._owner == me:
                self._depth += 1
                return True
            ticket = object()
            self._waiting.append(ticket)
            limit = time.monotonic() + timeout
            while self._owner is not None or self._waiting[0] is not ticket:
                remaining = limit - time.monotonic()
                if remaining <= 0 or not self._cond.wait(remaining):
                    if self._owner is not None or self._waiting[0] is not ticket:
                        self._waiting.remove(ticket)
                        self._cond.notify_all()
                        return False
            self._waiting.pop(0)
            self._owner, self._depth = me, 1
            return True

    def release(self):
        with self._cond:
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
                self._cond.notify_all()


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def writer_lock(path):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(path), FairLock())


# Capacity (final-audit I1, measured on the audit host class: Windows 11, Python 3.11, SQLite WAL, synchronous=FULL).
# Opening a SQLite connection costs 4 to 16 ms here (schema parse, WAL index) and closing it up to 4 ms more, and the
# old write_tx did both for every transaction while holding the in-process writer lock. That capped the whole
# system at 25 to 90 write transactions a second, so the paced 20-callbacks-a-second load sat on the edge and fell
# over whenever a few deadlines slipped (every expiry then adds a retry, which adds load). One long-lived writer
# connection per database file and process commits in about 3 ms, roughly 390 transactions a second, with the same
# synchronous=FULL durability. Only the thread that holds the FairLock ever touches that connection.
_WRITERS = {}
BEGIN_WAIT_S = 5.0
BEGIN_POLL_S = (0.0005, 0.004)


def writer_connection(path):
    conn = _WRITERS.get(str(path))
    if conn is None:
        conn = _WRITERS[str(path)] = connect(path, shared=True)
    return conn


def close_writers(path=None):
    """Close the cached writer connection of one database, or of all of them. Call with no transaction open."""
    for key in [str(path)] if path is not None else list(_WRITERS):
        conn = _WRITERS.pop(key, None)
        if conn is not None:
            conn.close()


def begin_immediate(conn):
    """Take SQLite's write lock without its built-in busy handler. That handler sleeps for 1 to 100 ms between tries
    and is not fair, so with a web process and a worker alternating it left the lock idle for about a third of the
    time while someone waited, and let one process starve the other for seconds. A short, bounded poll hands the
    lock over within a few milliseconds and keeps the five-second limit."""
    conn.execute("PRAGMA busy_timeout=0")
    try:
        limit = time.monotonic() + BEGIN_WAIT_S
        delay = BEGIN_POLL_S[0]
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if time.monotonic() >= limit or exc.sqlite_errorcode & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
            time.sleep(delay)
            delay = min(delay * 1.5, BEGIN_POLL_S[1])
    finally:
        conn.execute("PRAGMA busy_timeout=5000")


@contextmanager
def write_tx(path, reuse=True):
    lock = writer_lock(path)
    if not lock.acquire(timeout=WRITER_QUEUE_TIMEOUT_S):
        raise sqlite3.OperationalError("database is locked")
    conn = writer_connection(path) if reuse else connect(path)
    try:
        begin_immediate(conn)
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except BaseException:
        if conn.in_transaction and reuse:  # a connection that could not even roll back is never reused
            _WRITERS.pop(str(path), None)
            conn.close()
        raise
    finally:
        if not reuse:
            conn.close()
        lock.release()


JOBS = """CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, operation_key TEXT UNIQUE NOT NULL, attempt_id TEXT REFERENCES attempts,
    kind TEXT NOT NULL, status TEXT NOT NULL, due_at REAL NOT NULL, lease_token TEXT, lease_expires REAL,
    claims INTEGER NOT NULL DEFAULT 0, last_reason TEXT, case_id TEXT UNIQUE REFERENCES approvals(case_id),
    requeues INTEGER NOT NULL DEFAULT 0,
    CHECK ((kind='DISPATCH' AND attempt_id IS NOT NULL AND case_id IS NULL)
        OR (kind='ISSUE_MID' AND attempt_id IS NULL AND case_id IS NOT NULL AND operation_key='mid/' || case_id)))"""

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS applications (
       id TEXT PRIMARY KEY, submission_key TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL,
       submitter_hash TEXT NOT NULL, merchant TEXT NOT NULL, policy TEXT NOT NULL,
       data_revision INTEGER NOT NULL, state TEXT, created REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS documents (
       case_id TEXT NOT NULL REFERENCES applications, kind TEXT NOT NULL, revision INTEGER NOT NULL,
       payload TEXT, content_digest TEXT, PRIMARY KEY(case_id,kind,revision))""",
    """CREATE TABLE IF NOT EXISTS assessments (
       case_id TEXT NOT NULL REFERENCES applications, data_revision INTEGER NOT NULL,
       results TEXT NOT NULL, PRIMARY KEY(case_id,data_revision))""",
    """CREATE TABLE IF NOT EXISTS review_items (
       id INTEGER PRIMARY KEY, case_id TEXT NOT NULL REFERENCES applications, cause TEXT NOT NULL,
       subject TEXT NOT NULL, subject_revision INTEGER NOT NULL, owner TEXT NOT NULL,
       status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, reason TEXT NOT NULL)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS one_active_issue
       ON review_items(case_id,cause,subject) WHERE status <> 'RESOLVED'""",
    """CREATE TABLE IF NOT EXISTS checks (
       case_id TEXT NOT NULL REFERENCES applications, kind TEXT NOT NULL, input_revision INTEGER NOT NULL,
       PRIMARY KEY(case_id,kind))""",
    """CREATE TABLE IF NOT EXISTS attempts (
       id TEXT PRIMARY KEY, case_id TEXT NOT NULL, kind TEXT NOT NULL, input_revision INTEGER NOT NULL,
       generation INTEGER NOT NULL, status TEXT NOT NULL, inputs TEXT NOT NULL,
       sent_at REAL, deadline REAL, UNIQUE(case_id,kind,input_revision,generation),
       FOREIGN KEY(case_id,kind) REFERENCES checks(case_id,kind))""",
    """CREATE TABLE IF NOT EXISTS decisions (
       id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL REFERENCES review_items, issue_version INTEGER NOT NULL,
       case_id TEXT NOT NULL REFERENCES applications, subject_revision INTEGER NOT NULL,
       attempt_id TEXT REFERENCES attempts, action TEXT NOT NULL, actor TEXT NOT NULL,
       reason TEXT NOT NULL, at REAL NOT NULL, UNIQUE(issue_id,issue_version))""",
    """CREATE TABLE IF NOT EXISTS approvals (
       case_id TEXT PRIMARY KEY REFERENCES applications, approved_at REAL NOT NULL,
       receipt TEXT NOT NULL, receipt_digest TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS merchants (
       case_id TEXT PRIMARY KEY REFERENCES approvals(case_id), mid TEXT UNIQUE NOT NULL, received_at REAL NOT NULL)""",
    JOBS,
    # M4-A. An extraction is written once, by the server, from the exact image its OCR engine read. `files` holds
    # that normalized PNG so a reviewer can look at what was judged; neither table can be updated or deleted.
    """CREATE TABLE IF NOT EXISTS files (
       digest TEXT PRIMARY KEY, mime TEXT NOT NULL, data BLOB NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS extractions (
       id INTEGER PRIMARY KEY, case_id TEXT NOT NULL, kind TEXT NOT NULL, revision INTEGER NOT NULL,
       source_type TEXT NOT NULL, original_digest TEXT NOT NULL, render_digest TEXT NOT NULL REFERENCES files,
       engine TEXT NOT NULL, threshold REAL NOT NULL, min_confidence REAL NOT NULL, fields TEXT NOT NULL,
       created REAL NOT NULL, UNIQUE(case_id,kind,revision),
       FOREIGN KEY(case_id,kind,revision) REFERENCES documents(case_id,kind,revision))""",
    # What an operator's CLEAR_RISK_CHANGE decision set aside: the RISK input revision and the rejected applications
    # of the same legal entity (riskhistory.scope). A clearance is honoured only while that scope is still true.
    """CREATE TABLE IF NOT EXISTS risk_clearance_scope (
       decision_id INTEGER PRIMARY KEY REFERENCES decisions(id), scope TEXT NOT NULL)""",
    # A dismissed duplicate candidate stays dismissed only while both identities keep the same pair digest.
    """CREATE TABLE IF NOT EXISTS duplicate_dismissals (
       case_id TEXT NOT NULL REFERENCES applications, other_case_id TEXT NOT NULL REFERENCES applications,
       pair_digest TEXT NOT NULL, decision_id INTEGER NOT NULL REFERENCES decisions,
       PRIMARY KEY(case_id,other_case_id,pair_digest))""",
    # Backstop for "one Merchant ID per verified identity": one approval may claim a PAN.
    """CREATE TABLE IF NOT EXISTS identity_claims (
       pan TEXT PRIMARY KEY, case_id TEXT NOT NULL UNIQUE REFERENCES approvals(case_id))""",
    """CREATE TABLE IF NOT EXISTS events (
       seq INTEGER PRIMARY KEY, case_id TEXT NOT NULL REFERENCES applications, at REAL NOT NULL,
       event TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS case_events ON events(case_id,seq)""",
    """CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS tickets (
       digest TEXT PRIMARY KEY, role TEXT NOT NULL, expires REAL NOT NULL)""",
    # M2: one row per authenticated provider event. Replays increment receipts; business state does not move.
    """CREATE TABLE IF NOT EXISTS callback_inbox (
       provider TEXT NOT NULL, event_id TEXT NOT NULL, body_digest TEXT NOT NULL,
       attempt_id TEXT NOT NULL REFERENCES attempts, case_id TEXT NOT NULL REFERENCES applications,
       outcome TEXT NOT NULL, disposition TEXT NOT NULL, received_at REAL NOT NULL,
       receipts INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(provider,event_id))""",
    """CREATE INDEX IF NOT EXISTS inbox_by_case ON callback_inbox(case_id,received_at)""",
    # Bounded counters for requests rejected before they could be bound to a case.
    """CREATE TABLE IF NOT EXISTS rejected_callbacks (
       provider TEXT NOT NULL, reason TEXT NOT NULL, count INTEGER NOT NULL, last_at REAL NOT NULL,
       PRIMARY KEY(provider,reason))""",
)

# Columns added after M1. Applied to fresh and pre-existing run directories alike.
COLUMNS = {
    "attempts": (("reference", "TEXT"), ("settled_at", "REAL"), ("reason_code", "TEXT"),
                 ("registered_name", "TEXT"), ("name_score", "REAL")),
    "jobs": (("lease_token", "TEXT"), ("lease_expires", "REAL"),
             ("claims", "INTEGER NOT NULL DEFAULT 0"), ("last_reason", "TEXT")),
}
INDEXES = ("CREATE INDEX IF NOT EXISTS sent_deadlines ON attempts(status,deadline)",
           "CREATE INDEX IF NOT EXISTS due_jobs ON jobs(status,due_at)",
           "CREATE INDEX IF NOT EXISTS applied_attempt ON callback_inbox(attempt_id) WHERE disposition='APPLIED'")


def migrate_jobs(conn):
    # M1/M2 tied every job to a provider attempt. Preserve all claims, leases, keys and primary keys.
    if any(row["name"] == "attempt_id" and row["notnull"] for row in conn.execute("PRAGMA table_info(jobs)")):
        conn.execute("ALTER TABLE jobs RENAME TO legacy_dispatch_jobs")
        conn.execute(JOBS)
        columns = "id,operation_key,attempt_id,kind,status,due_at,lease_token,lease_expires,claims,last_reason"
        conn.execute(f"INSERT INTO jobs({columns}) SELECT {columns} FROM legacy_dispatch_jobs")
        conn.execute("DROP TABLE legacy_dispatch_jobs")


def initialize(run_dir, timing=None):
    """Create or migrate a run. An explicit timing profile replaces the stored one."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "onboarding.db"
    with closing(connect(path)) as conn:
        if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
            raise RuntimeError("A local disk with SQLite WAL support is required.")
    with write_tx(path, reuse=False) as conn:  # one-off work: leave no cached connection behind
        for ddl in SCHEMA:
            conn.execute(ddl)
        for table, columns in COLUMNS.items():
            present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns:
                if name not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        migrate_jobs(conn)
        for table in ("approvals", "decisions", "merchants", "extractions", "files", "identity_claims", "duplicate_dismissals",
                      "risk_clearance_scope"):
            for mutation in ("UPDATE", "DELETE"):
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{table}_{mutation}
                    BEFORE {mutation} ON {table} FOR EACH ROW BEGIN SELECT RAISE(ABORT,'immutable record'); END""")
        for table, key in (("merchants", "case_id"), ("decisions", "id"), ("extractions", "id")):
            # INSERT OR REPLACE would delete the old row without firing the DELETE trigger (M3 review O1).
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS replace_guard_{table} BEFORE INSERT ON {table}
                WHEN EXISTS(SELECT 1 FROM {table} WHERE {key}=NEW.{key})
                BEGIN SELECT RAISE(ABORT,'immutable record'); END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS approval_insert_once BEFORE INSERT ON approvals
            WHEN EXISTS(SELECT 1 FROM approvals WHERE case_id=NEW.case_id)
            BEGIN SELECT RAISE(ABORT,'immutable approval'); END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS frozen_policy BEFORE UPDATE OF policy ON applications
            WHEN NEW.policy<>OLD.policy BEGIN SELECT RAISE(ABORT,'immutable policy snapshot'); END""")
        for ddl in INDEXES:
            conn.execute(ddl)
        defaults = {"signing_key": secrets.token_hex(32), "sim_api_key": secrets.token_hex(32)}
        defaults.update({"callback_secret:" + p: secrets.token_hex(32) for p in PROVIDERS})
        for name, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (name, value))
        if timing is not None:
            conn.execute("""INSERT INTO settings VALUES ('timing',?)
                         ON CONFLICT(name) DO UPDATE SET value=excluded.value""", (packed(TIMING[timing]),))
        else:
            conn.execute("INSERT OR IGNORE INTO settings VALUES ('timing',?)", (packed(TIMING["demo"]),))
    return path


def setting(conn, name):
    row = conn.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
    return row[0] if row else None


def timing(conn):
    return json.loads(setting(conn, "timing"))


def policy():
    config = json.loads(Path(__file__).with_name("policy.json").read_text(encoding="utf-8"))
    if not valid_policy(config):
        raise ValueError("Invalid local verification policy.")
    return config


def valid_policy(config):
    if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] < 1:
        return False
    required, referrals = config.get("required"), config.get("referral_kinds")
    if not all(isinstance(group, list) and all(isinstance(k, str) for k in group) for group in (required, referrals)):
        return False
    kinds = set(required)
    return (2 <= len(kinds) == len(required) and kinds <= set(PROVIDERS)
            and len(set(referrals)) == len(referrals) and set(referrals) <= (kinds & {"RISK"}))
