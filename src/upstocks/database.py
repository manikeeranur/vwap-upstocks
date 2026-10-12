"""Short SQLite transactions, versioned schema, and single-process ownership."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


MIGRATIONS = [
    (
        "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        """CREATE TABLE strategy_config (
            preset TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
            live INTEGER NOT NULL CHECK(live IN (0,1)), lots INTEGER NOT NULL CHECK(lots > 0))""",
        """CREATE TABLE order_attempts (
            trade_id TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
            preset TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','submitted','unknown')),
            payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(trade_id, side))""",
        "CREATE INDEX orders_status ON order_attempts(status, preset)",
        """CREATE TABLE journal_sessions (
            day TEXT NOT NULL, preset TEXT NOT NULL, lots INTEGER NOT NULL,
            lot_size INTEGER NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(day,preset))""",
        """CREATE TABLE journal_trades (
            day TEXT NOT NULL, preset TEXT NOT NULL, trade_id TEXT NOT NULL,
            position INTEGER NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(day,preset,trade_id), FOREIGN KEY(day,preset)
            REFERENCES journal_sessions(day,preset) ON DELETE CASCADE)""",
        "CREATE INDEX journal_trade_id ON journal_trades(trade_id)",
        "CREATE TABLE notes (trade_id TEXT PRIMARY KEY, text TEXT NOT NULL, updated_at TEXT NOT NULL)",
        """CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
            message TEXT NOT NULL, details TEXT NOT NULL, request_id TEXT)""",
        "CREATE INDEX audit_created ON audit_events(created_at)",
        """CREATE TABLE backtest_jobs (
            id TEXT PRIMARY KEY, settings TEXT NOT NULL, preset TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running','done','error')),
            total INTEGER, current TEXT, error TEXT, missing TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE backtest_days (
            job_id TEXT NOT NULL REFERENCES backtest_jobs(id) ON DELETE CASCADE,
            day TEXT NOT NULL, payload TEXT NOT NULL, sequence INTEGER NOT NULL,
            PRIMARY KEY(job_id,day), UNIQUE(job_id,sequence))""",
    ),
    (
        "ALTER TABLE audit_events ADD COLUMN level TEXT NOT NULL DEFAULT 'INFO'",
        "ALTER TABLE audit_events ADD COLUMN component TEXT NOT NULL DEFAULT 'trading'",
        "CREATE INDEX audit_filter ON audit_events(component,level,created_at,id)",
        "INSERT INTO settings VALUES ('config_revision','0')",
        "INSERT INTO settings VALUES ('entries_paused','false')",
        """CREATE TABLE decision_evidence (
            id INTEGER PRIMARY KEY, day TEXT NOT NULL, stage TEXT NOT NULL,
            created_at TEXT NOT NULL, run_id TEXT NOT NULL, payload TEXT NOT NULL)""",
        "CREATE INDEX evidence_session ON decision_evidence(day,id)",
        """CREATE TRIGGER evidence_no_update BEFORE UPDATE ON decision_evidence
            BEGIN SELECT RAISE(ABORT,'Decision evidence is append-only'); END""",
        """CREATE TRIGGER evidence_no_delete BEFORE DELETE ON decision_evidence
            BEGIN SELECT RAISE(ABORT,'Decision evidence is append-only'); END""",
    ),
]


class Database:
    def __init__(self, path: Path, timeout: float = 10):
        self.path, self.timeout = Path(path), timeout

    @contextmanager
    def connect(self, *, write=False):
        address = str(self.path) if write else self.path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(address, uri=not write, timeout=self.timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=self.timeout)) as conn:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise RuntimeError("SQLite WAL mode is required; use a local disk")
        with self.connect(write=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            version = conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
            if version > len(MIGRATIONS):
                raise RuntimeError("Database schema is newer than this application")
            for i, statements in enumerate(MIGRATIONS, 1):
                if i > version:
                    for sql in statements:
                        conn.execute(sql)
                    conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (i, utc_now()))

    def check(self):
        with self.connect() as conn:
            return {"integrity": conn.execute("PRAGMA quick_check").fetchone()[0],
                    "foreign_key_errors": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
                    "schema_version": conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]}

    def backup(self, destination: Path):
        destination = Path(destination).resolve()
        if destination.exists() or destination == self.path.resolve():
            raise ValueError("Backup destination must be a new file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # sqlite3.backup includes committed WAL contents and supports a running app.
        with closing(sqlite3.connect(self.path, timeout=self.timeout)) as source:
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target, pages=256)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
        return destination


class ProcessLock:
    """OS lock released even on a crash. File presence alone is not a lock."""
    def __init__(self, database_path: Path):
        self.path = Path(str(database_path) + ".lock")
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("Another process owns this database. Run one app worker.") from exc
        self.handle = handle

    def release(self):
        if self.handle:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None
