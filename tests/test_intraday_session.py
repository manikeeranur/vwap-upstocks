"""Intraday presentation rollover; no live orders or external calls."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from helpers import DashboardClient
from upstocks import upstox
from upstocks.app import create_app
from upstocks.config import AppSettings
from upstocks.database import Database
from upstocks.repository import Repository
from upstocks.services.exchange_calendar import ExchangeCalendar
from upstocks.services.trading import TradingService


def moment(value):
    return datetime.fromisoformat(value).replace(tzinfo=upstox.IST)


class IntradayCalendarTests(unittest.TestCase):
    def setUp(self):
        self.calendar = ExchangeCalendar(SimpleNamespace(get_json=lambda key: None))

    def test_monday_rollover_is_exactly_0830_ist(self):
        before = self.calendar.intraday_session(moment("2026-11-02T08:29:59.999999"))
        after = self.calendar.intraday_session(moment("2026-11-02T08:30:00"))
        self.assertEqual((before["day"], before["phase"]), ("2026-10-30", "previous"))
        self.assertEqual(before["next_reset_at"], "2026-11-02T08:30:00+05:30")
        self.assertEqual((after["day"], after["phase"]), ("2026-11-02", "current"))
        self.assertEqual(after["next_reset_at"], "2026-11-03T08:30:00+05:30")

    def test_weekends_and_exchange_holidays_keep_the_previous_session(self):
        for at, expected in [("2026-10-31T12:00:00", "2026-10-30"),
                             ("2026-11-01T12:00:00", "2026-10-30"),
                             ("2026-10-20T12:00:00", "2026-10-19"),
                             ("2026-10-21T08:29:59", "2026-10-19")]:
            with self.subTest(at=at):
                self.assertEqual(self.calendar.intraday_session(moment(at))["day"], expected)
        self.assertEqual(self.calendar.intraday_session(moment("2026-10-21T08:30:00"))["day"], "2026-10-21")

    def test_special_sunday_session_resets_at_0830(self):
        self.assertEqual(self.calendar.intraday_session(moment("2026-11-08T08:29:59"))["day"], "2026-11-06")
        self.assertEqual(self.calendar.intraday_session(moment("2026-11-08T08:30:00"))["day"], "2026-11-08")

    def test_server_timezone_does_not_change_the_cutoff(self):
        now = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)
        session = self.calendar.intraday_session(now)
        self.assertEqual(session["day"], "2026-11-02")
        self.assertEqual(session["server_now"], "2026-11-02T08:30:00+05:30")

    def test_unknown_calendar_does_not_invent_a_trading_day(self):
        session = self.calendar.intraday_session(moment("2027-01-04T09:00:00"))
        self.assertIsNone(session["day"])
        self.assertIsNone(session["next_reset_at"])
        self.assertEqual(session["phase"], "unavailable")


class IntradaySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.db = Database(self.root / "test.sqlite3")
        self.db.initialize()
        self.repo = Repository(self.db)
        self.repo.migrate_legacy(self.root)
        self.repo.ensure_presets()
        for day in ["2026-10-30", "2026-10-28"]:
            trade = {"id": f"harmonic|{day}|10:00|NSE_FO|1", "pnl": 12, "open": False}
            view = {"day": day, "scanned_at": "15:30:00", "results": {"harmonic": [trade]}, "errors": {}, "running": False}
            self.repo.save_scan(view, {"harmonic": {"lots": 1, "lot_size": 65, "updated": day + " 15:30", "trades": [trade]}})
        self.service = TradingService(self.repo)

    def snapshot(self, at):
        with patch("upstocks.live.now_ist", return_value=moment(at)):
            return self.service.snapshot(1, intraday=True)

    def test_saved_previous_session_survives_a_different_replay(self):
        snapshot = self.snapshot("2026-11-02T08:29:59")
        self.assertEqual(snapshot["view"]["day"], "2026-10-30")
        self.assertEqual(len(snapshot["view"]["results"]["harmonic"]), 1)
        self.assertEqual(self.service.view["day"], "2026-10-28")
        self.assertEqual(self.service.snapshot(1)["view"]["day"], "2026-10-28")

    def test_rollover_empties_the_view_without_deleting_scans_or_orders(self):
        self.service.state["orders"]["1:upstox"] = {"harmonic|2026-10-30|10:00|NSE_FO|1": {
            "buy": {"filled_qty": 65, "fill_price": 100, "execution_status": "complete"}}}
        state, view = deepcopy(self.service.state), deepcopy(self.service.view)
        snapshot = self.snapshot("2026-11-02T08:30:00")
        self.assertEqual(snapshot["view"]["day"], "2026-11-02")
        self.assertEqual(snapshot["view"]["results"], {})
        self.assertIsNone(snapshot["view"]["scanned_at"])
        self.assertEqual(snapshot["state"]["orders"], state["orders"])
        self.assertEqual(self.service.state, state)
        self.assertEqual(self.service.view, view)
        self.assertEqual(set(self.repo.journal("2026-10-28", "2026-10-30", 1)["days"]), {"2026-10-28", "2026-10-30"})

    def test_current_session_appears_as_soon_as_a_scan_arrives(self):
        self.service.view = {"day": "2026-11-02", "results": {"harmonic": [{"id": "new-trade", "pnl": 5}]}, "errors": {}, "running": False}
        snapshot = self.snapshot("2026-11-02T09:45:00")
        self.assertEqual(snapshot["view"]["results"]["harmonic"][0]["id"], "new-trade")

    def test_unavailable_calendar_has_no_old_paper_results(self):
        snapshot = self.snapshot("2027-01-04T09:00:00")
        self.assertIsNone(snapshot["view"]["day"])
        self.assertEqual(snapshot["view"]["results"], {})

    def test_daily_reset_works_after_restart(self):
        self.service = TradingService(self.repo)
        self.assertEqual(self.snapshot("2026-11-02T08:30:00")["view"]["results"], {})
        self.assertEqual(self.repo.get_view()["day"], "2026-10-28")

    def test_api_opt_in_preserves_the_scan_and_replay_response(self):
        settings = AppSettings(database_path=self.db.path, log_dir=self.root / "logs", cache_dir=self.root / "cache", legacy_dir=self.root, scanner_enabled=False)
        with DashboardClient(create_app(settings)) as client, patch("upstocks.live.now_ist", return_value=moment("2026-11-02T08:30:00")):
            scoped = client.get("/api/paper?intraday=true")
            self.assertEqual(scoped.status_code, 200)
            self.assertEqual(scoped.json()["view"]["day"], "2026-11-02")
            self.assertEqual(scoped.json()["view"]["results"], {})
            self.assertEqual(client.get("/api/paper").json()["view"]["day"], "2026-10-28")
