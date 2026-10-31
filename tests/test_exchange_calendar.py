"""Calendar and reminder regressions. No real broker or notification calls."""

import asyncio
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from helpers import DashboardClient
from upstocks import upstox
from upstocks.app import create_app
from upstocks.config import AppSettings
from upstocks.database import Database
from upstocks.repository import Repository
from upstocks.services.exchange_calendar import ExchangeCalendar, validate_snapshot
from upstocks.services.reminders import ReminderService


def memory_repo():
    values = {}
    return SimpleNamespace(
        get_json=lambda key: deepcopy(values.get(key)),
        set_json=lambda key, value: values.update({key: deepcopy(value)}),
    )


class CalendarTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = memory_repo()
        self.calendar = ExchangeCalendar(self.repo)

    def test_closures_settlement_days_and_special_sessions(self):
        for key in ("2026-01-15", "2026-01-26", "2026-10-20", "2026-11-10", "2026-12-25"):
            with self.subTest(key=key):
                day = self.calendar.day(date.fromisoformat(key))
                self.assertFalse(day["login_alerts_required"])
                self.assertEqual({v["kind"] for v in day["exchanges"].values()}, {"holiday"})
        for key in ("2026-02-19", "2026-03-19", "2026-04-01", "2026-08-26", "2026-11-02"):
            with self.subTest(key=key):
                day = self.calendar.day(date.fromisoformat(key))
                self.assertTrue(day["login_alerts_required"])
                self.assertEqual(day["exchanges"]["NSE"]["kind"], "open")
        for key in ("2026-02-01", "2026-11-08"):
            day = self.calendar.day(date.fromisoformat(key))
            self.assertEqual(day["exchanges"]["NSE"]["kind"], "special")
            self.assertIn("IST", day["exchanges"]["NSE"]["session"])
            self.assertFalse(day["login_alerts_required"])

    def test_weekend_unknown_year_and_leap_month(self):
        for key in ("2026-10-31", "2026-11-01"):
            day = self.calendar.day(date.fromisoformat(key))
            self.assertEqual(day["exchanges"]["NSE"]["kind"], "weekend")
            self.assertFalse(day["login_alerts_required"])
        day = self.calendar.day(date(2027, 1, 4))
        self.assertFalse(day["verified"])
        self.assertFalse(day["login_alerts_required"])
        self.assertEqual(day["exchanges"]["NSE"]["kind"], "unknown")
        self.assertEqual(len(self.calendar.month(2028, 2)["days"]), 29)

    def test_corrupt_and_older_cache_do_not_replace_bundled_data(self):
        self.repo.set_json("exchange_calendar", {"broken": {"year": 2026}, "bad": []})
        self.assertIn(2026, ExchangeCalendar(self.repo).years)
        self.repo.set_json("exchange_calendar", ["not a mapping"])
        self.assertIn(2026, ExchangeCalendar(self.repo).years)
        old = deepcopy(self.calendar.years[2026])
        old["checked_at"] = "2026-01-01T00:00:00+05:30"
        old["records"][0]["description"] = "Old cached description"
        self.repo.set_json("exchange_calendar", {"2026": old})
        self.assertNotEqual(
            ExchangeCalendar(self.repo).years[2026]["records"][0]["description"], "Old cached description"
        )

    def test_unreadable_database_cache_keeps_bundled_calendar(self):
        repo = SimpleNamespace(get_json=Mock(side_effect=ValueError("Invalid JSON")))
        self.assertIn(2026, ExchangeCalendar(repo).years)

    async def test_refresh_failure_retains_cache_and_backs_off(self):
        self.calendar._fetch = AsyncMock(side_effect=httpx.RequestError("offline"))
        now = datetime(2026, 11, 2, 8, 30, tzinfo=upstox.IST)
        await self.calendar.refresh(now)
        await self.calendar.refresh(now)
        self.assertEqual(self.calendar._fetch.await_count, 1)
        self.assertIsNotNone(self.calendar.month(2026, 11)["warning"])
        self.assertEqual(self.calendar.day(date(2026, 11, 10))["exchanges"]["NSE"]["kind"], "holiday")

    async def test_invalid_refresh_does_not_overwrite_valid_calendar(self):
        self.calendar._fetch = AsyncMock(return_value=[])
        before = deepcopy(self.calendar.years)
        await self.calendar.refresh(datetime(2026, 11, 2, tzinfo=upstox.IST))
        self.assertEqual(self.calendar.years, before)
        self.assertIsNone(self.repo.get_json("exchange_calendar"))

    async def test_parallel_refresh_is_single_fetch_and_persists(self):
        records = deepcopy(self.calendar.years[2026]["records"])
        records[0]["description"] = "Updated exchange notice"
        self.calendar._fetch = AsyncMock(return_value=records)
        now = datetime(2026, 11, 2, tzinfo=upstox.IST)
        await asyncio.gather(*(self.calendar.refresh(now) for _ in range(3)))
        self.assertEqual(self.calendar._fetch.await_count, 1)
        self.assertEqual(
            ExchangeCalendar(self.repo).years[2026]["records"][0]["description"], "Updated exchange notice"
        )
        self.assertIsNone(self.calendar.last_error)

    async def test_new_year_requires_matching_feed(self):
        self.calendar._fetch = AsyncMock(return_value=self.calendar.years[2026]["records"])
        now = datetime(2027, 1, 4, tzinfo=upstox.IST)
        await self.calendar.refresh(now)
        self.assertFalse(self.calendar.day(now.date())["verified"])
        self.calendar._retry_after = 0
        self.calendar._fetch = AsyncMock(
            return_value=[
                {
                    "date": "2027-01-26",
                    "description": "Republic Day",
                    "holiday_type": "TRADING_HOLIDAY",
                    "closed_exchanges": ["NSE", "NFO", "BSE", "BFO"],
                    "open_exchanges": [],
                }
            ]
        )
        await self.calendar.refresh(now)
        self.assertTrue(self.calendar.day(now.date())["login_alerts_required"])
        self.assertFalse(self.calendar.day(date(2027, 1, 26))["login_alerts_required"])

    def test_invalid_sessions_are_rejected(self):
        for change in ("wrong_year", "duplicate", "bad_session", "missing_exchange"):
            value = deepcopy(self.calendar.years[2026])
            if change == "wrong_year":
                value["year"] = 2027
            elif change == "duplicate":
                value["records"].append(value["records"][0])
            elif change == "bad_session":
                value["records"][0]["open_exchanges"] = ["bad"]
            else:
                for row in value["records"]:
                    row["closed_exchanges"] = [x for x in row["closed_exchanges"] if x != "BFO"]
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                validate_snapshot(value)


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calendar = ExchangeCalendar(memory_repo())
        self.calendar.refresh = AsyncMock()
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                exchange_calendar=self.calendar,
                settings=SimpleNamespace(public_url="https://example.invalid"),
                repository=SimpleNamespace(user=Mock(return_value={"telegram_chat_id": "test-only"})),
            )
        )
        self.service = ReminderService(self.app)
        self.service._needs_login = Mock(return_value={1: ["upstox", "kite", "indmoney"]})
        self.service.send = AsyncMock()
        self.approval = patch("upstocks.api.accounts.request_upstox_approval", new_callable=AsyncMock)
        self.approve = self.approval.start()
        self.addCleanup(self.approval.stop)

    async def test_every_reminder_is_silent_on_weekends_and_exchange_holidays(self):
        for key in ("2026-10-31", "2026-11-01", "2026-10-20", "2026-11-08", "2026-11-10"):
            for hhmm in ("08:30", "08:45", "09:00", "09:10", "09:15"):
                now = datetime.fromisoformat(key + "T" + hhmm + ":00+05:30")
                await self.service.tick(hhmm, now)
        self.service._needs_login.assert_not_called()
        self.service.send.assert_not_awaited()
        self.approve.assert_not_awaited()

    async def test_reminders_resume_on_trading_and_settlement_weekdays(self):
        for key in ("2026-11-02", "2026-04-01", "2026-08-26"):
            await self.service.tick("08:30", datetime.fromisoformat(key + "T08:30:00+05:30"))
        self.assertEqual(self.service.send.await_count, 3)
        self.assertEqual(self.approve.await_count, 3)
        await self.service.tick("09:15", datetime(2026, 11, 2, 9, 15, tzinfo=upstox.IST))
        self.assertIn("MISSED", self.service.send.call_args.args[1])
        self.assertEqual(self.approve.await_count, 3)

    async def test_ist_date_and_unknown_year_fail_quietly(self):
        await self.service.tick("08:30", datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc))
        await self.service.tick("08:30", datetime(2027, 1, 4, 8, 30, tzinfo=upstox.IST))
        await self.service.tick("12:00", datetime(2026, 11, 2, 12, 0, tzinfo=upstox.IST))
        self.service.send.assert_not_awaited()
        self.approve.assert_not_awaited()


class CalendarApiTests(unittest.TestCase):
    def test_api_metadata_validation_and_login_error_visibility(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = create_app(
                AppSettings(
                    database_path=root / "db.sqlite3",
                    log_dir=root / "logs",
                    cache_dir=root / "cache",
                    legacy_dir=root,
                    scanner_enabled=False,
                )
            )
            with (
                DashboardClient(app) as client,
                patch.object(app.state.exchange_calendar, "refresh", new_callable=AsyncMock),
            ):
                data = client.get("/api/market/calendar?year=2026&month=11").json()
                self.assertEqual(data["days"]["2026-11-10"]["exchanges"]["NSE"]["kind"], "holiday")
                self.assertEqual(data["days"]["2026-11-08"]["exchanges"]["NSE"]["kind"], "special")
                for query in ("year=2026&month=13", "year=1900&month=1", "year=2026&month=0"):
                    self.assertEqual(client.get("/api/market/calendar?" + query).status_code, 422)
                svc = app.state.trading
                svc._fills_errors["1:kite"] = "Broker login expired; reconciliation unavailable"
                with patch("upstocks.live.now_ist", return_value=datetime(2026, 10, 31, tzinfo=upstox.IST)):
                    data = client.get("/api/paper").json()
                    self.assertFalse(data["market_calendar"]["login_alerts_required"])
                    self.assertIsNone(data["reconciliation"]["1:kite"]["error"])
                    with patch.object(svc, "_unresolved", return_value=True):
                        self.assertIn(
                            "Unresolved orders", svc.snapshot(1)["reconciliation"]["1:kite"]["error"]
                        )
                with patch("upstocks.live.now_ist", return_value=datetime(2026, 11, 2, tzinfo=upstox.IST)):
                    self.assertIn(
                        "login expired", client.get("/api/paper").json()["reconciliation"]["1:kite"]["error"]
                    )
                svc._fills_errors["1:kite"] = "Broker ownership mismatch"
                self.assertEqual(
                    svc.snapshot(1)["reconciliation"]["1:kite"]["error"], "Broker ownership mismatch"
                )

    def test_calendar_cache_survives_repository_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "db.sqlite3")
            db.initialize()
            repo = Repository(db)
            cal = ExchangeCalendar(repo)
            repo.set_json("exchange_calendar", {"2026": cal.years[2026]})
            loaded = ExchangeCalendar(Repository(Database(Path(folder) / "db.sqlite3")))
            self.assertEqual(loaded.month(2026, 11), cal.month(2026, 11))
