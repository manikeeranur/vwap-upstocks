import concurrent.futures
from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from upstocks.database import Database, ProcessLock
from upstocks.repository import Repository
from upstocks.vwap import Settings

PRESET = "frontend_live"
TID = "frontend_live|2026-10-12|09:36|NSE_FO|123"


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "data" / "test.sqlite3")
        self.db.initialize()
        self.repo = Repository(self.db)

    def initialize(self):
        self.repo.migrate_legacy(self.root)

    def test_journal_live_trades_carry_real_fill_prices(self):
        tid, ctx = "harmonic|2026-10-28|10:01|NSE_FO|1", {"trade": {}}
        for side, oid, price in (("BUY", "b1", 439.1), ("SELL", "s1", 543.8)):
            self.assertTrue(self.repo.reserve_order(tid, "1:kite", "harmonic", side, {"status": "pending", "qty": 65}, ctx))
            self.repo.finish_order(tid, "1:kite", side, {"status": "submitted", "order_id": oid, "qty": 65, "broker_symbol": "NIFTY SAMPLE CE"}, "sent")
            self.repo.save_fill(tid, "1:kite", side, price, 60.5 if side == "BUY" else 67.58, filled_qty=65, execution_status="complete")
        live = self.repo.journal("2026-10-28", "2026-10-28")["live"]
        self.assertEqual(live, {tid: {"1:kite": {"qty": 65, "sold_qty": 65, "buy_status": "complete", "sell_status": "complete", "buy": 439.1, "sell": 543.8, "buy_charges": None, "sell_charges": None, "client_id": "", "holder": "", "symbol": "NIFTY SAMPLE CE"}}})

    def test_journal_filters_live_orders_by_user_and_uses_actual_fills(self):
        for account in ("1:kite", "2:kite"):
            self.repo.reserve_order(TID, account, PRESET, "BUY", {"status": "pending", "qty": 65}, {"trade": {}})
            self.repo.finish_order(TID, account, "BUY", {"status": "submitted", "order_id": account, "qty": 65,
                                                       "filled_qty": 10, "execution_status": "open"}, "partial")
        rows = self.repo.journal("2026-10-12", "2026-10-12", 1)["live"][TID]
        self.assertEqual(set(rows), {"1:kite"})
        self.assertEqual(rows["1:kite"]["qty"], 10)
        self.assertEqual(rows["1:kite"]["sold_qty"], 0)

    def test_schema_idempotent_and_wal(self):
        self.db.initialize()
        self.assertEqual(self.db.check(), {"integrity": "ok", "foreign_key_errors": 0, "schema_version": 8})
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_legacy_import_precedence_backup_and_idempotence(self):
        paper = {"lot_size": 25}
        state = {"lot_size": 65, "config": {PRESET: {"on": False, "live": False, "lots": 3}},
                 "orders": {TID: {"buy": {"qty": 195, "order_id": "broker-id", "symbol": "NIFTY"}}}, "log": ["old activity"]}
        for name, data in (("paper_state.json", paper), ("live_state.json", state)):
            (self.root / name).write_text(json.dumps(data))
        raw = (self.root / "live_state.json").read_bytes()
        self.initialize()
        self.repo.configure(PRESET, user=1, broker="upstox", lots=4)
        self.initialize()
        loaded = self.repo.state()
        self.assertEqual(loaded["lot_size"], 65)
        self.assertEqual(loaded["accounts"]["1:upstox"]["lots"][PRESET], 4)
        self.assertEqual(loaded["orders"]["1:upstox"][TID]["buy"]["order_id"], "broker-id")
        self.assertEqual(loaded["log"], ["old activity"])
        self.assertEqual((self.root / "live_state.json").read_bytes(), raw)
        self.assertEqual(next((self.db.path.parent / "legacy").glob("live_state-*.json")).read_bytes(), raw)

    def test_corrupt_legacy_does_not_reset_state(self):
        (self.root / "live_state.json").write_text('{broken')
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            self.initialize()
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM strategy_config").fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT * FROM metadata WHERE key='legacy_import'").fetchone())

    def test_invalid_orders_roll_back_entire_import(self):
        (self.root / "live_state.json").write_text(json.dumps({"lot_size": 65, "orders": {TID: {"buy": {"qty": 0}}}}))
        with self.assertRaises(ValueError):
            self.initialize()
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0], 2)

    def test_unique_order_reservation_under_concurrency(self):
        self.initialize()
        payload = {"qty": 65, "status": "pending"}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            reserved = list(pool.map(lambda _: self.repo.reserve_order(TID, "1:upstox", PRESET, "BUY", payload), range(20)))
        self.assertEqual(sum(reserved), 1)
        self.repo.recover()
        record = Repository(Database(self.db.path)).state()["orders"]["1:upstox"][TID]["buy"]
        self.assertEqual(record["status"], "unknown")
        self.assertIn("broker", record["error"])
        self.assertFalse(self.repo.reserve_order(TID, "1:upstox", PRESET, "BUY", payload))

    def test_journal_rescan_keeps_notes_and_other_strategies(self):
        self.initialize()
        day = "2026-10-12"
        session = {"lots": 2, "lot_size": 65, "updated": "2026-10-12 10:00", "trades": [{"id": TID, "pnl": 10}]}
        self.repo.save_scan({"day": day}, {PRESET: session, "smc_live": {**session, "trades": []}})
        self.repo.set_note(TID, " keep this ")
        session["trades"][0]["pnl"] = 20
        self.repo.save_scan({"day": day}, {PRESET: session})
        result = self.repo.journal(day, day)
        self.assertEqual(result["notes"][TID], "keep this")
        self.assertEqual(result["days"][day][PRESET]["trades"][0]["pnl"], 20)
        self.assertIn("smc_live", result["days"][day])
        self.assertFalse(self.repo.get_view()["running"])
        self.assertEqual(self.repo.journal("2026-10-01", "2026-10-02"), {"days": {}, "notes": {}, "live": {}})

    def test_jobs_survive_restart_and_partial_results_are_kept(self):
        self.initialize()
        settings = Settings(start=date(2026, 10, 1), end=date(2026, 10, 2))
        self.repo.create_job("done", settings, PRESET)
        self.repo.create_job("interrupted", settings, PRESET)
        self.repo.add_job_day("done", {"date": "2026-10-02", "trades": []})
        self.repo.add_job_day("done", {"date": "2026-10-01", "trades": []})
        self.repo.add_job_day("interrupted", {"date": "2026-10-01", "trades": []})
        self.repo.update_job("done", status="done", total=2)
        restarted = Repository(Database(self.db.path))
        restarted.recover()
        self.assertEqual(restarted.get_job("done")["status"], "done")
        self.assertEqual(restarted.get_job("done")["days"][0]["date"], "2026-10-01")
        self.assertEqual(restarted.get_job("interrupted")["status"], "error")
        self.assertEqual(restarted.get_job("interrupted")["done"], 1)

    def test_online_backup_contains_committed_wal(self):
        self.initialize()
        # Hold a reader open, preventing the WAL from being checkpointed away.
        with self.db.connect() as reader:
            reader.execute("SELECT * FROM settings").fetchall()
            self.repo.configure(PRESET, user=1, broker="upstox", lots=7)
            target = self.db.backup(self.root / "backup.sqlite3")
        self.assertEqual(Repository(Database(target)).state()["accounts"]["1:upstox"]["lots"][PRESET], 7)
        self.assertEqual(Database(target).check()["integrity"], "ok")
        with self.assertRaises(ValueError):
            self.db.backup(target)

    def test_transaction_rollback(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.connect(write=True) as conn:
                conn.execute("INSERT INTO settings VALUES ('a','1')")
                conn.execute("INSERT INTO settings VALUES ('a','2')")
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT * FROM settings WHERE key='a'").fetchone())

    def test_process_lock_rejects_duplicate_and_can_be_reacquired(self):
        first, second = ProcessLock(self.db.path), ProcessLock(self.db.path)
        first.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, "Another process"):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.release()


if __name__ == "__main__":
    unittest.main()
