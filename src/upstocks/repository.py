"""Persistence boundary. Every business update and its audit entry commit together."""
from dataclasses import asdict
from datetime import date
import hashlib
import json
from pathlib import Path
import secrets
import uuid
from typing import Any

from .database import Database, utc_now
from .observability import clean, redact, request_id

PRESETS = ("frontend_live", "smc_live", "ema25_vwap", "ema_pullback", "smc_tuned", "smc_ema_combo", "harmonic", "harmonic_2y", "sensex_swing", "sensex_bigleg", "harmonic_power")
# Lots a newly added strategy starts with (others 1): the SENSEX pair runs as 3 + 2 lots.
DEFAULT_LOTS = {"sensex_swing": 3, "sensex_bigleg": 2}
BROKERS = ("upstox", "kite", "indmoney")


def account_id(user_id, broker):
    """Orders and switches belong to one person's account at one broker: '<user id>:<broker>'."""
    return f"{user_id}:{broker}"


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=lambda v: v.isoformat())


def _audit(conn, kind, message, details=None, level="INFO", component="trading"):
    conn.execute("INSERT INTO audit_events(created_at,kind,message,details,request_id,level,component) VALUES (?,?,?,?,?,?,?)",
                 (utc_now(), kind, redact(message), encode(clean(details or {})), request_id.get(), level, component))


class ConfigurationConflict(Exception):
    pass


def _setting(conn, key, value):
    conn.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encode(value)))


def _positive(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f"Invalid {name} in legacy data")
    return value


class Repository:
    def __init__(self, db: Database):
        self.db = db
        self.run_id = uuid.uuid4().hex
        root = Path(__file__).parent
        files = ("live.py", "vwap.py", "smc.py", "ema_pullback.py", "combo.py", "harmonic.py", "services/trading.py", "upstox.py")
        hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}
        self.implementation = {"files": hashes, "sha256": hashlib.sha256(encode(hashes).encode()).hexdigest()}

    def dashboard_token(self):
        with self.db.connect(write=True) as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key='dashboard_token'").fetchone()
            if row:
                return row[0]
            token = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO metadata VALUES ('dashboard_token',?)", (token,))
            return token

    def session_secret(self):
        with self.db.connect(write=True) as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key='session_secret'").fetchone()
            if row:
                return row[0]
            secret = secrets.token_urlsafe(48)
            conn.execute("INSERT INTO metadata VALUES ('session_secret',?)", (secret,))
            return secret

    def event(self, kind, message, *, level="INFO", component="application", details=None):
        with self.db.connect(write=True) as conn:
            _audit(conn, kind, message, {"run_id": self.run_id, **(details or {})}, level, component)

    def _evidence(self, conn, day, stage, payload):
        conn.execute("INSERT INTO decision_evidence(day,stage,created_at,run_id,payload) VALUES (?,?,?,?,?)",
                     (day, stage, utc_now(), self.run_id,
                      encode(clean({"implementation": self.implementation, **payload}))))

    def migrate_legacy(self, directory: Path):
        """Import once. Invalid input rolls back, never silently resets live state."""
        with self.db.connect(write=True) as conn:
            if conn.execute("SELECT 1 FROM metadata WHERE key='legacy_import'").fetchone():
                return
            sources, manifests = {}, {}
            for name in ("live_state.json", "paper_state.json", "journal.json"):
                path = directory / name
                if not path.exists():
                    continue
                raw = path.read_bytes()
                try:
                    data = json.loads(raw.decode("utf-8-sig"))
                    if not isinstance(data, dict):
                        raise ValueError("Expected object")
                except (ValueError, UnicodeError) as exc:
                    raise ValueError(f"Cannot migrate {name}: invalid JSON; original file preserved") from exc
                digest = hashlib.sha256(raw).hexdigest()
                backup = self.db.path.parent / "legacy" / f"{path.stem}-{digest[:16]}.json"
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    backup.write_bytes(raw)
                elif backup.read_bytes() != raw:
                    raise ValueError(f"Legacy backup checksum mismatch for {name}")
                sources[name] = data
                manifests[name] = {"sha256": digest, "backup": backup.name}

            state = sources.get("live_state.json", sources.get("paper_state.json", {}))
            _setting(conn, "lot_size", _positive(state.get("lot_size", 65), "lot size"))
            config = state.get("config", {})
            if not isinstance(config, dict) or set(config) - set(PRESETS):
                raise ValueError("Unknown or invalid legacy strategy config")
            for preset in PRESETS:
                cfg = config.get(preset, {})
                if not isinstance(cfg, dict) or any(type(cfg.get(k, default)) is not bool for k, default in (("on", True), ("live", False))):
                    raise ValueError(f"Invalid legacy config: {preset}")
                conn.execute("INSERT INTO strategy_config VALUES (?,?,?,?)",
                             (preset, cfg.get("on", True), cfg.get("live", False), _positive(cfg.get("lots", 1), "lots")))
                # The single-user live switch becomes person 1's Upstox switch.
                conn.execute("INSERT OR REPLACE INTO broker_strategy VALUES (1,'upstox',?,?,?)",
                             (preset, cfg.get("live", False), _positive(cfg.get("lots", 1), "lots")))
            orders = state.get("orders", {})
            if not isinstance(orders, dict):
                raise ValueError("Invalid legacy orders")
            for tid, sides in orders.items():
                if not isinstance(sides, dict) or len(tid.split("|", 3)) != 4 or tid.split("|")[0] not in PRESETS:
                    raise ValueError("Invalid legacy order identity")
                for side, payload in sides.items():
                    if side not in {"buy", "sell"} or not isinstance(payload, dict):
                        raise ValueError("Invalid legacy order")
                    _positive(payload.get("qty"), "order quantity")
                    status = "submitted" if payload.get("order_id") else "unknown"
                    payload = {**payload, "status": status}
                    if status == "unknown":
                        payload.setdefault("error", "Legacy order outcome unknown. Check the broker before taking action.")
                    conn.execute("INSERT INTO order_attempts VALUES (?,?,?,?,?,?,?)",
                                 (tid, side.upper(), tid.split("|")[0], status, encode(payload), utc_now(), utc_now()))
                    conn.execute("INSERT INTO broker_orders VALUES (?,?,?,?,?,?,?,?)",
                                 (tid, "1:upstox", side.upper(), tid.split("|")[0], status, encode(payload), utc_now(), utc_now()))
            logs = state.get("log", [])
            if not isinstance(logs, list) or any(not isinstance(v, str) for v in logs):
                raise ValueError("Invalid legacy activity log")
            for line in logs:
                _audit(conn, "activity", line)
            journal = sources.get("journal.json", {})
            if not isinstance(journal.get("days", {}), dict) or not isinstance(journal.get("notes", {}), dict):
                raise ValueError("Invalid legacy journal")
            for day, sessions in journal.get("days", {}).items():
                date.fromisoformat(day)
                if not isinstance(sessions, dict) or set(sessions) - set(PRESETS):
                    raise ValueError("Invalid legacy journal strategies")
                self._write_sessions(conn, day, sessions)
            for tid, text in journal.get("notes", {}).items():
                if not isinstance(text, str):
                    raise ValueError("Invalid legacy note")
                conn.execute("INSERT INTO notes VALUES (?,?,?)", (tid, text, utc_now()))
            conn.execute("INSERT INTO metadata VALUES ('legacy_import',?)", (encode({"at": utc_now(), "sources": manifests}),))
            _audit(conn, "migration", "Legacy migration completed", {"sources": list(sources), "orders": len(orders)})

    def ensure_presets(self):
        """Give strategies (and people) added later a settings row: paper on; every broker switch off, 1 lot
        (DEFAULT_LOTS for the SENSEX pair).
        Existing rows are left as they are."""
        with self.db.connect(write=True) as conn:
            for preset in PRESETS:
                conn.execute("INSERT OR IGNORE INTO strategy_config VALUES (?,1,0,1)", (preset,))
            for (uid,) in conn.execute("SELECT id FROM users").fetchall():
                for broker in BROKERS:
                    for preset in PRESETS:
                        conn.execute("INSERT OR IGNORE INTO broker_strategy VALUES (?,?,?,0,?)", (uid, broker, preset, DEFAULT_LOTS.get(preset, 1)))

    def get_json(self, key, default=None):
        """A JSON value kept in the settings table (e.g. Momentum Tuned's live state)."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_json(self, key, value):
        with self.db.connect(write=True) as conn:
            _setting(conn, key, value)

    def state(self):
        """Global strategy settings plus every person's broker switches and orders."""
        with self.db.connect() as conn:
            config = {r["preset"]: {"on": bool(r["enabled"])} for r in conn.execute("SELECT * FROM strategy_config")}
            settings = {r[0]: json.loads(r[1]) for r in conn.execute("SELECT key,value FROM settings WHERE key IN ('lot_size','config_revision')")}
            users = {r["id"]: {"username": r["username"], "paused": bool(r["entries_paused"]), "telegram": bool(r["telegram_chat_id"])}
                     for r in conn.execute("SELECT * FROM users")}
            accounts: dict[str, dict[str,Any]] = {}
            for r in conn.execute("SELECT * FROM broker_strategy"):
                acc = accounts.setdefault(account_id(r["user_id"], r["broker"]),
                                          {"user": r["user_id"], "broker": r["broker"], "live": {}, "lots": {}})
                acc["live"][r["preset"]], acc["lots"][r["preset"]] = bool(r["live"]), r["lots"]
            orders: dict[str, dict[str,Any]] = {}
            for r in conn.execute("SELECT * FROM broker_orders ORDER BY created_at"):
                orders.setdefault(r["account"], {}).setdefault(r["trade_id"], {})[r["side"].lower()] = json.loads(r["payload"])
            logs = [r[0] for r in conn.execute("SELECT message FROM audit_events WHERE kind='activity' ORDER BY id DESC LIMIT 300")]
            return {"lot_size": settings["lot_size"], "config": config, "users": users, "accounts": accounts,
                    "orders": orders, "log": logs[::-1], "revision": settings["config_revision"]}

    def configure(self, preset=None, *, user=None, broker=None, on=None, live=None, lots=None, lot_size=None,
                  entries_paused=None, revision=None):
        """`on` and `lot_size` are shared; `live`/`lots` belong to one person's broker account; `entries_paused` to one person."""
        with self.db.connect(write=True) as conn:
            current = json.loads(conn.execute("SELECT value FROM settings WHERE key='config_revision'").fetchone()[0])
            if revision is not None and revision != current:
                raise ConfigurationConflict("Settings changed in another tab. Review the refreshed settings and try again.")
            changes = {k: v for k, v in {"on": on, "live": live, "lots": lots, "lot_size": lot_size, "entries_paused": entries_paused}.items() if v is not None}
            if preset and on is not None:
                conn.execute("UPDATE strategy_config SET enabled=? WHERE preset=?", (on, preset))
            if preset and (live is not None or lots is not None):
                if user is None or broker not in BROKERS:
                    raise ValueError("A person and broker are required for live settings")
                for column, value in (("live", live), ("lots", lots)):
                    if value is not None:
                        conn.execute(f"UPDATE broker_strategy SET {column}=? WHERE user_id=? AND broker=? AND preset=?",
                                     (value, user, broker, preset))
            if lot_size is not None:
                _setting(conn, "lot_size", lot_size)
            if entries_paused is not None:
                if user is None:
                    raise ValueError("A person is required to pause entries")
                conn.execute("UPDATE users SET entries_paused=? WHERE id=?", (entries_paused, user))
            _setting(conn, "config_revision", current + 1)
            _audit(conn, "config", "Entries paused" if entries_paused is True else "Entries resumed" if entries_paused is False else "Strategy settings updated",
                   {"preset": preset, "user": user, "broker": broker, "revision": current + 1, **changes})
            return current + 1

    def reserve_order(self, tid, account, preset, side, payload, context=None):
        with self.db.connect(write=True) as conn:
            inserted = conn.execute("INSERT OR IGNORE INTO broker_orders VALUES (?,?,?,?,?,?,?,?)",
                                    (tid, account, side, preset, "pending", encode(payload), utc_now(), utc_now())).rowcount
            if inserted:
                _audit(conn, "order_reserved", f"{side} order reserved", {"trade_id": tid, "account": account, "qty": payload["qty"]})
                self._evidence(conn, tid.split("|", 3)[1], "ORDER_INTENT",
                               {"trade_id": tid, "account": account, "side": side, "order": payload, "context": context or {}})
            return bool(inserted)

    def replace_exit(self, tid, account, expected, payload):
        """Archive a terminal attempt and reserve only its unfilled remainder atomically."""
        from .stocks.domain import PortfolioError
        from .stocks.statements import fingerprint
        with self.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM broker_orders WHERE trade_id=? AND account=? AND side='SELL'",(tid,account)).fetchone()
            buy = conn.execute("SELECT payload FROM broker_orders WHERE trade_id=? AND account=? AND side='BUY'",(tid,account)).fetchone()
            if not row or not buy:
                raise PortfolioError("Exit intent is unavailable")
            old, bought = json.loads(row["payload"]), json.loads(buy[0])
            if fingerprint(old) != fingerprint(expected) or old.get("execution_status") not in ("cancelled","rejected"):
                raise PortfolioError("Exit changed since review; reconcile and review it again")
            if payload["qty"] != bought.get("filled_qty",0) - old.get("filled_qty",0) or payload["qty"] <= 0:
                raise PortfolioError("Replacement quantity exceeds the remaining strategy position")
            conn.execute("INSERT INTO broker_order_attempts(trade_id,account,side,payload,archived_at) VALUES(?,?,'SELL',?,?)",
                         (tid,account,row["payload"],utc_now()))
            conn.execute("UPDATE broker_orders SET status='pending',payload=?,updated_at=? WHERE trade_id=? AND account=? AND side='SELL'",
                         (encode(payload),utc_now(),tid,account))
            self._evidence(conn,tid.split("|")[1],"EXIT_REPLACEMENT",{"account":account,"trade_id":tid,"previous":old,"replacement":payload})
            return True

    def finish_order(self, tid, account, side, payload, message):
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE broker_orders SET status=?,payload=?,updated_at=? WHERE trade_id=? AND account=? AND side=?",
                         (payload["status"], encode(payload), utc_now(), tid, account, side))
            _audit(conn, "activity", message, level="ERROR" if payload["status"] == "unknown" else "INFO")
            self._evidence(conn, tid.split("|", 3)[1], "ORDER_RESULT", {"trade_id": tid, "account": account, "side": side, "order": payload})

    def save_fill(self, tid, account, side, price=None, charges=None, filled_qty=None, execution_status=None):
        """Record the broker's average fill price and/or charges on a submitted order (kept once known)."""
        with self.db.connect(write=True) as conn:
            row = conn.execute("SELECT payload FROM broker_orders WHERE trade_id=? AND account=? AND side=?", (tid, account, side)).fetchone()
            if row is None:
                return
            payload = json.loads(row["payload"])
            changes = {k: v for k, v in (("fill_price", price), ("charges", charges), ("filled_qty", filled_qty), ("execution_status", execution_status)) if v is not None and payload.get(k) != v}
            if not changes:
                return
            payload.update(changes)
            conn.execute("UPDATE broker_orders SET payload=?,updated_at=? WHERE trade_id=? AND account=? AND side=?",
                         (encode(payload), utc_now(), tid, account, side))

    def recover(self):
        with self.db.connect(write=True) as conn:
            pending = conn.execute("SELECT * FROM broker_orders WHERE status='pending'").fetchall()
            for row in pending:
                payload = json.loads(row["payload"])
                payload.update(status="unknown", error="Order interrupted. Verify its status with the broker; it will not be retried automatically.")
                conn.execute("UPDATE broker_orders SET status='unknown',payload=?,updated_at=? WHERE trade_id=? AND account=? AND side=?",
                             (encode(payload), utc_now(), row["trade_id"], row["account"], row["side"]))
                _audit(conn, "activity", f"Order needs reconciliation: {row['trade_id']} {row['account']} {row['side']}", level="WARNING")
                self._evidence(conn, row['trade_id'].split('|', 3)[1], "ORDER_RECOVERY",
                               {"trade_id": row['trade_id'], "account": row["account"], "side": row['side'], "order": payload})
            jobs = conn.execute("UPDATE backtest_jobs SET status='error',error='Interrupted by application restart. Saved results are retained.',updated_at=? WHERE status='running'", (utc_now(),)).rowcount
            if pending or jobs:
                _audit(conn, "recovery", "Interrupted operations recovered", {"orders": len(pending), "jobs": jobs})

    def activity(self, message):
        with self.db.connect(write=True) as conn:
            _audit(conn, "activity", message)

    def get_view(self):
        with self.db.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='latest_scan'").fetchone()
            view = json.loads(row[0]) if row else {"day": None, "scanned_at": None, "results": {}, "errors": {}}
            return {**view, "running": False}

    @staticmethod
    def _write_sessions(conn, day, sessions):
        for preset, session in sessions.items():
            conn.execute("""INSERT INTO journal_sessions VALUES (?,?,?,?,?) ON CONFLICT(day,preset)
                         DO UPDATE SET lots=excluded.lots,lot_size=excluded.lot_size,updated=excluded.updated""",
                         (day, preset, _positive(session["lots"], "journal lots"), _positive(session["lot_size"], "journal lot size"), session["updated"]))
            conn.execute("DELETE FROM journal_trades WHERE day=? AND preset=?", (day, preset))
            for position, trade in enumerate(session["trades"]):
                conn.execute("INSERT INTO journal_trades VALUES (?,?,?,?,?)", (day, preset, trade["id"], position, encode(trade)))

    def save_scan(self, view, sessions=None, context=None):
        with self.db.connect(write=True) as conn:
            _setting(conn, "latest_scan", {**view, "running": False})
            self._evidence(conn, view["day"], "SCAN", {"view": view, "context": context or {}})
            _audit(conn, "scan", "Scan completed", {"day": view["day"], "errors": view.get("errors", {}), "recorded_in_journal": sessions is not None},
                   "WARNING" if view.get("errors") else "INFO", "scanner")
            if sessions is not None:
                self._write_sessions(conn, view["day"], sessions)

    def journal(self, start, end, user_id=None):
        with self.db.connect() as conn:
            days: dict[str, Any] = {}
            for r in conn.execute("SELECT * FROM journal_sessions WHERE day BETWEEN ? AND ? ORDER BY day,preset", (start, end)):
                days.setdefault(r["day"], {})[r["preset"]] = {"lots": r["lots"], "lot_size": r["lot_size"], "updated": r["updated"], "trades": []}
            for r in conn.execute("SELECT * FROM journal_trades WHERE day BETWEEN ? AND ? ORDER BY day,preset,position", (start, end)):
                days[r["day"]][r["preset"]]["trades"].append(json.loads(r["payload"]))
            notes = {r[0]: r[1] for r in conn.execute("SELECT DISTINCT n.trade_id,n.text FROM notes n JOIN journal_trades t ON n.trade_id=t.trade_id WHERE t.day BETWEEN ? AND ?", (start, end))}
            # Trades a broker actually bought, with the real fill prices once known:
            # {trade id: {account: {"qty", "buy", "sell"}}} (trade ids are "preset|YYYY-MM-DD|…").
            live: dict[str, Any] = {}
            sells = {}
            for r in conn.execute("SELECT trade_id,account,side,payload FROM broker_orders "
                                  "WHERE substr(trade_id, instr(trade_id,'|')+1, 10) BETWEEN ? AND ?", (str(start), str(end))):
                if user_id is not None and not r["account"].startswith(f"{user_id}:"):
                    continue
                order = json.loads(r["payload"])
                if not order.get("order_id"):
                    continue
                if r["side"] == "BUY":
                    live.setdefault(r["trade_id"], {})[r["account"]] = {"qty": order.get("filled_qty") or 0, "sold_qty": 0,
                                                                         "buy_status": order.get("execution_status"), "sell_status": None,
                                                                         "buy": order.get("fill_price"), "sell": None,
                                                                         "buy_charges": order.get("charges") if order.get("charges_source") == "operator_statement" else None, "sell_charges": None,
                                                                         "client_id": order.get("client_id") or "", "holder": order.get("holder") or ""}
                else:
                    sells[(r["trade_id"], r["account"])] = order
            for (tid, account), order in sells.items():
                if account in live.get(tid, {}):
                    live[tid][account].update(sell=order.get("fill_price"), sell_charges=order.get("charges") if order.get("charges_source") == "operator_statement" else None,
                                                 sold_qty=order.get("filled_qty") or 0, sell_status=order.get("execution_status"))
            return {"days": days, "notes": notes, "live": live}

    def set_note(self, tid, text):
        with self.db.connect(write=True) as conn:
            if text.strip():
                conn.execute("INSERT INTO notes VALUES (?,?,?) ON CONFLICT(trade_id) DO UPDATE SET text=excluded.text,updated_at=excluded.updated_at", (tid, text.strip(), utc_now()))
            else:
                conn.execute("DELETE FROM notes WHERE trade_id=?", (tid,))
            _audit(conn, "note", "Trade note updated", {"trade_id": tid})

    def create_job(self, jid, settings, preset):
        with self.db.connect(write=True) as conn:
            conn.execute("INSERT INTO backtest_jobs(id,settings,preset,status,created_at,updated_at) VALUES (?,?,?,'running',?,?)", (jid, encode(asdict(settings)), preset, utc_now(), utc_now()))
            _audit(conn, "backtest", "Backtest started", {"job_id": jid, "preset": preset}, component="research")

    def update_job(self, jid, **changes):
        allowed = {"status", "total", "current", "error", "missing"}
        if not changes or set(changes) - allowed:
            raise ValueError("Invalid job update")
        changes = {k: encode(v) if k == "missing" else v for k, v in changes.items()}
        with self.db.connect(write=True) as conn:
            assignments = ",".join(f"{k}=?" for k in changes)
            conn.execute(f"UPDATE backtest_jobs SET {assignments},updated_at=? WHERE id=?", (*changes.values(), utc_now(), jid))
            if changes.get("status") in {"done", "error"}:
                _audit(conn, "backtest", f"Backtest {changes['status']}", {"job_id": jid}, "ERROR" if changes["status"] == "error" else "INFO", "research")

    def add_job_day(self, jid, day):
        with self.db.connect(write=True) as conn:
            sequence = conn.execute("SELECT COUNT(*) FROM backtest_days WHERE job_id=?", (jid,)).fetchone()[0]
            conn.execute("INSERT INTO backtest_days VALUES (?,?,?,?)", (jid, day["date"], encode(day), sequence))
            conn.execute("UPDATE backtest_jobs SET updated_at=? WHERE id=?", (utc_now(), jid))

    def get_job(self, jid):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM backtest_jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                return None
            job = dict(row)
            for key in ("settings", "missing"):
                job[key] = json.loads(job[key])
            job["days"] = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM backtest_days WHERE job_id=? ORDER BY sequence", (jid,))]
            job["done"] = len(job["days"])
            if job["status"] != "running":
                job["days"].sort(key=lambda d: d["date"])
            return job

    def recent_jobs(self, limit=30):
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id,preset,status,settings,created_at FROM backtest_jobs ORDER BY created_at DESC LIMIT ?", (limit,))
            return [{"id": r["id"], "preset": r["preset"], "status": r["status"], "created_at": r["created_at"],
                     "start": json.loads(r["settings"])["start"], "end": json.loads(r["settings"])["end"]} for r in rows]

    def health(self):
        with self.db.connect() as conn:
            version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            unknown = conn.execute("SELECT COUNT(*) FROM broker_orders WHERE status='unknown'").fetchone()[0]
            return {"database": "ok", "schema_version": version, "orders_needing_review": unknown}

    # ── People and their broker accounts ──
    def user(self, user_id=None, username=None):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?" if user_id is not None else "SELECT * FROM users WHERE username=?",
                               (user_id if user_id is not None else username,)).fetchone()
            return dict(row) if row else None

    def users(self):
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT id,username,telegram_chat_id,entries_paused,created_at FROM users ORDER BY id")]

    def add_user(self, username, password_hash):
        with self.db.connect(write=True) as conn:
            uid = conn.execute("INSERT INTO users(username,password_hash,created_at) VALUES (?,?,?)",
                               (username, password_hash, utc_now())).lastrowid
            for broker in BROKERS:
                for preset in PRESETS:
                    conn.execute("INSERT OR IGNORE INTO broker_strategy VALUES (?,?,?,0,?)", (uid, broker, preset, DEFAULT_LOTS.get(preset, 1)))
            _audit(conn, "user", "Person added", {"user": uid, "username": username}, component="application")
            return uid

    def set_password(self, user_id, password_hash):
        with self.db.connect(write=True) as conn:
            if not conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id)).rowcount:
                raise ValueError("No such person")
            _audit(conn, "user", "Password changed", {"user": user_id}, component="application")

    def set_telegram(self, user_id, chat_id):
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE users SET telegram_chat_id=? WHERE id=?", (chat_id or None, user_id))

    def broker_account(self, user_id, broker):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM broker_accounts WHERE user_id=? AND broker=?", (user_id, broker)).fetchone()
            return dict(row) if row else None

    def broker_accounts_by_key(self, broker, api_key):
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM broker_accounts WHERE broker=? AND api_key=?", (broker, api_key))]

    def save_broker_keys(self, user_id, broker, api_key=None, api_secret_sealed=None):
        with self.db.connect(write=True) as conn:
            conn.execute("INSERT OR IGNORE INTO broker_accounts(user_id,broker,updated_at) VALUES (?,?,?)", (user_id, broker, utc_now()))
            if api_key is not None and broker == "indmoney":
                # INDmoney's Client ID only generates tokens; the current token stays valid when it changes.
                conn.execute("UPDATE broker_accounts SET api_key=?,updated_at=? WHERE user_id=? AND broker=?",
                             (api_key, utc_now(), user_id, broker))
            elif api_key is not None:
                # A different app key invalidates the old login; saving the same key again keeps it.
                conn.execute("UPDATE broker_accounts SET api_key=?,access_token='',token_issued=NULL,broker_user=NULL,profile='{}',updated_at=? "
                             "WHERE user_id=? AND broker=? AND api_key<>?",
                             (api_key, utc_now(), user_id, broker, api_key))
            if api_secret_sealed is not None:
                conn.execute("UPDATE broker_accounts SET api_secret=?,updated_at=? WHERE user_id=? AND broker=?",
                             (api_secret_sealed, utc_now(), user_id, broker))
            _audit(conn, "broker", "Broker keys updated", {"user": user_id, "broker": broker}, component="application")

    def save_broker_token(self, user_id, broker, token_sealed, issued, broker_user, profile=None):
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE broker_accounts SET access_token=?,token_issued=?,broker_user=?,profile=?,updated_at=? WHERE user_id=? AND broker=?",
                         (token_sealed, issued, broker_user, json.dumps(profile or {}), utc_now(), user_id, broker))
            _audit(conn, "broker", "Broker login saved", {"user": user_id, "broker": broker, "broker_user": broker_user}, component="application")
