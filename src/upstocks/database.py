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
    (
        # Several people, each with their own login, broker keys/tokens, live switches and orders.
        # User 1 inherits the single-user settings: each strategy's live/lots become its Upstox switch.
        """CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
            telegram_chat_id TEXT, entries_paused INTEGER NOT NULL DEFAULT 0 CHECK(entries_paused IN (0,1)),
            created_at TEXT NOT NULL)""",
        """CREATE TABLE broker_accounts (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            broker TEXT NOT NULL CHECK(broker IN ('upstox','kite')),
            api_key TEXT NOT NULL DEFAULT '', api_secret TEXT NOT NULL DEFAULT '',
            access_token TEXT NOT NULL DEFAULT '', token_issued TEXT, broker_user TEXT,
            updated_at TEXT NOT NULL, PRIMARY KEY(user_id, broker))""",
        """CREATE TABLE broker_strategy (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            broker TEXT NOT NULL CHECK(broker IN ('upstox','kite')), preset TEXT NOT NULL,
            live INTEGER NOT NULL CHECK(live IN (0,1)), lots INTEGER NOT NULL CHECK(lots > 0),
            PRIMARY KEY(user_id, broker, preset))""",
        """CREATE TABLE broker_orders (
            trade_id TEXT NOT NULL, account TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
            preset TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','submitted','unknown')),
            payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(trade_id, account, side))""",
        "CREATE INDEX broker_orders_status ON broker_orders(status, account)",
        "INSERT INTO users(id,username,password_hash,created_at) VALUES (1,'mani','!',strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        "UPDATE users SET entries_paused=(SELECT value='true' FROM settings WHERE key='entries_paused') WHERE id=1",
        "INSERT INTO broker_strategy SELECT 1,'upstox',preset,live,lots FROM strategy_config",
        "INSERT INTO broker_orders SELECT trade_id,'1:upstox',side,preset,status,payload,created_at,updated_at FROM order_attempts",
    ),
    (
        # Broker account name, email and photo from the last login: {"user_id","name","email","avatar"}.
        "ALTER TABLE broker_accounts ADD COLUMN profile TEXT NOT NULL DEFAULT '{}'",
    ),
    (
        # Add INDmoney (INDstocks) as a third broker.
        """CREATE TABLE broker_accounts_v5 (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            broker TEXT NOT NULL CHECK(broker IN ('upstox','kite','indmoney')),
            api_key TEXT NOT NULL DEFAULT '', api_secret TEXT NOT NULL DEFAULT '',
            access_token TEXT NOT NULL DEFAULT '', token_issued TEXT, broker_user TEXT,
            updated_at TEXT NOT NULL, profile TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(user_id, broker))""",
        """INSERT INTO broker_accounts_v5(user_id,broker,api_key,api_secret,access_token,token_issued,broker_user,updated_at,profile)
           SELECT user_id,broker,api_key,api_secret,access_token,token_issued,broker_user,updated_at,profile FROM broker_accounts""",
        "DROP TABLE broker_accounts",
        "ALTER TABLE broker_accounts_v5 RENAME TO broker_accounts",
        """CREATE TABLE broker_strategy_v5 (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            broker TEXT NOT NULL CHECK(broker IN ('upstox','kite','indmoney')), preset TEXT NOT NULL,
            live INTEGER NOT NULL CHECK(live IN (0,1)), lots INTEGER NOT NULL CHECK(lots > 0),
            PRIMARY KEY(user_id, broker, preset))""",
        "INSERT INTO broker_strategy_v5(user_id,broker,preset,live,lots) SELECT user_id,broker,preset,live,lots FROM broker_strategy",
        "DROP TABLE broker_strategy",
        "ALTER TABLE broker_strategy_v5 RENAME TO broker_strategy",
    ),
    (
        # Orders placed before the account holder was recorded: take it from that broker's login at the time of this
        # upgrade, which is still the account those orders went to.
        """UPDATE broker_orders SET payload = json_set(payload,
             '$.client_id', COALESCE((SELECT broker_user FROM broker_accounts a WHERE a.user_id || ':' || a.broker = broker_orders.account), ''),
             '$.holder', COALESCE((SELECT json_extract(a.profile, '$.name') FROM broker_accounts a WHERE a.user_id || ':' || a.broker = broker_orders.account), ''))
           WHERE json_extract(payload, '$.order_id') IS NOT NULL AND json_extract(payload, '$.client_id') IS NULL""",
    ),
    (
        """CREATE TABLE stock_portfolios (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), broker TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
            paused INTEGER NOT NULL DEFAULT 1 CHECK(paused IN (0,1)),
            usable_capital INTEGER NOT NULL CHECK(usable_capital>0), cash INTEGER NOT NULL CHECK(cash>=0),
            realized INTEGER NOT NULL DEFAULT 0, client_id TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 0, blocked TEXT, plan TEXT, last_error TEXT, last_checked TEXT,
            UNIQUE(user_id,broker), CHECK(broker IN ('paper','upstox','kite','indmoney')))""",
        """CREATE TABLE stock_positions (
            portfolio_id TEXT PRIMARY KEY REFERENCES stock_portfolios(id), symbol TEXT NOT NULL,
            isin TEXT NOT NULL, instrument_key TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty>0),
            cost INTEGER NOT NULL CHECK(cost>=0), opened_at TEXT NOT NULL)""",
        """CREATE TABLE stock_orders (
            id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL REFERENCES stock_portfolios(id),
            cycle TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
            symbol TEXT NOT NULL, isin TEXT NOT NULL, instrument_key TEXT NOT NULL,
            qty INTEGER NOT NULL CHECK(qty>0), limit_paise INTEGER NOT NULL CHECK(limit_paise>0),
            reserved INTEGER NOT NULL, client_id TEXT NOT NULL, status TEXT NOT NULL,
            broker_id TEXT, filled_qty INTEGER NOT NULL DEFAULT 0 CHECK(filled_qty>=0 AND filled_qty<=qty),
            notional INTEGER NOT NULL DEFAULT 0, fee_provision INTEGER NOT NULL DEFAULT 0,
            error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(portfolio_id,cycle,side),
            CHECK(status IN ('pending','submitted','unknown','open','complete','cancelled','rejected')))""",
        "CREATE INDEX stock_orders_portfolio ON stock_orders(portfolio_id,status)",
        """CREATE TABLE stock_fills (
            id INTEGER PRIMARY KEY, order_id TEXT NOT NULL REFERENCES stock_orders(id),
            cumulative_qty INTEGER NOT NULL, qty INTEGER NOT NULL, notional INTEGER NOT NULL,
            fee_provision INTEGER NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE stock_events (
            id INTEGER PRIMARY KEY, portfolio_id TEXT NOT NULL REFERENCES stock_portfolios(id),
            kind TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TRIGGER stock_events_no_update BEFORE UPDATE ON stock_events
           BEGIN SELECT RAISE(ABORT,'Stock events are append-only'); END""",
        """CREATE TRIGGER stock_events_no_delete BEFORE DELETE ON stock_events
           BEGIN SELECT RAISE(ABORT,'Stock events are append-only'); END""",
        """CREATE TRIGGER stock_fills_no_update BEFORE UPDATE ON stock_fills
           BEGIN SELECT RAISE(ABORT,'Stock fills are append-only'); END""",
        """CREATE TRIGGER stock_fills_no_delete BEFORE DELETE ON stock_fills
           BEGIN SELECT RAISE(ABORT,'Stock fills are append-only'); END""",
    ),

    (
        "ALTER TABLE stock_portfolios ADD COLUMN entry_source TEXT NOT NULL DEFAULT 'monthly'",
        "ALTER TABLE stock_portfolios ADD COLUMN alignment_pending INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE stock_portfolios ADD COLUMN cash_shortfall INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE stock_orders ADD COLUMN parent_id TEXT REFERENCES stock_orders(id)",
        "ALTER TABLE stock_orders ADD COLUMN origin_cycle TEXT",
        "ALTER TABLE stock_orders ADD COLUMN charges_paise INTEGER CHECK(charges_paise>=0)",
        "ALTER TABLE stock_orders ADD COLUMN charges_source TEXT",
        "ALTER TABLE stock_orders ADD COLUMN charges_document TEXT",
        "CREATE UNIQUE INDEX stock_order_replacement ON stock_orders(parent_id) WHERE parent_id IS NOT NULL",
        """CREATE TABLE stock_recoveries (
            id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL REFERENCES stock_portfolios(id),
            parent_id TEXT NOT NULL UNIQUE REFERENCES stock_orders(id), status TEXT NOT NULL,
            expires_at TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE reconciliation_documents (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), broker TEXT NOT NULL,
            reference TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, UNIQUE(user_id,broker,digest))""",
        """CREATE TABLE stock_legacy_claims (
            identity TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL REFERENCES stock_portfolios(id),
            document_id TEXT NOT NULL REFERENCES reconciliation_documents(id))""",
        """CREATE TABLE broker_order_attempts (
            id INTEGER PRIMARY KEY, trade_id TEXT NOT NULL, account TEXT NOT NULL,
            side TEXT NOT NULL, payload TEXT NOT NULL, archived_at TEXT NOT NULL)""",
        """CREATE TRIGGER broker_attempts_no_update BEFORE UPDATE ON broker_order_attempts
           BEGIN SELECT RAISE(ABORT,'Broker attempts are append-only'); END""",
        """CREATE TRIGGER broker_attempts_no_delete BEFORE DELETE ON broker_order_attempts
           BEGIN SELECT RAISE(ABORT,'Broker attempts are append-only'); END""",
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
