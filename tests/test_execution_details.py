"""Execution display analytics use real fills and minute candles, without trading."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from upstocks.scanner_candles import ScannerCandles
from upstocks.services.execution_details import ExecutionDetails, candle_peaks, order_time, timestamp

DAY = "2026-10-30"
TID = f"harmonic|{DAY}|11:01|NSE_FO|1"
ROWS = [[DAY+"T11:02:00+05:30", 100, 110, 90, 105],
        [DAY+"T11:03:00+05:30", 105, 120, 95, 110],
        [DAY+"T11:04:00+05:30", 110, 120, 85, 90]]


def snapshot():
    return {"intraday_session": {"day": DAY, "server_now": DAY+"T16:00:00+05:30"},
            "state": {"orders": {"1:kite": {TID: {
                "buy": {"at": "11:02:29", "filled_qty": 65, "fill_price": 100, "execution_status": "complete"},
                "sell": {"at": "11:04:20", "filled_qty": 65, "fill_price": 90, "execution_status": "complete"}}}}}}


class PeakTests(unittest.TestCase):
    def test_peak_window_points_first_tie_and_timezone(self):
        start = timestamp("2026-10-30T05:32:29Z", DAY)
        end = timestamp("11:04:20", DAY)
        rows = [[DAY+"T11:01:00+05:30", 100, 999, 1, 100], *reversed(ROWS),
                [DAY+"T11:05:00+05:30", 100, 999, 1, 100], ["bad"], [DAY+"T11:03:00+05:30", 1, float("nan"), 1, 1]]
        peaks = candle_peaks(rows, start, end, 100)
        self.assertEqual(peaks["high"], {"price": 120, "at": DAY+"T11:03:00+05:30", "points": 20, "source": "candle"})
        self.assertEqual(peaks["low"]["points"], -15)
        self.assertTrue(peaks["starts_at_entry"] and peaks["ends_at_exit"])

    def test_entry_is_the_floor_for_favorable_excursion(self):
        peaks = candle_peaks(ROWS, timestamp("11:02:29", DAY), timestamp("11:04:20", DAY), 130)
        self.assertEqual(peaks["high"], {"price": 130, "at": DAY+"T11:02:29+05:30", "points": 0, "source": "entry"})

    def test_order_time_uses_filled_attempts_not_confirmation_or_signal_time(self):
        order = {"created_at": DAY+"T12:30:00+05:30", "filled_qty": 65, "attempt_filled_qty": 0,
                 "attempts": [{"created_at": DAY+"T11:30:00+05:30", "filled_qty": 65}], "confirmed_at": DAY+"T17:00:00+05:30"}
        self.assertEqual(order_time(order, DAY, last=True).hour, 11)
        self.assertIsNone(order_time({"at": "11:30:00", "filled_qty": 0}, DAY))
        self.assertIsNone(timestamp("bad", DAY))


class DetailsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved = {}
        self.repo = SimpleNamespace(get_json=self.saved.get, set_json=self.saved.__setitem__)
        self.candles = ScannerCandles()
        self.candles.record("NSE_FO|1", "1m", date.fromisoformat(DAY), ROWS)
        self.details = ExecutionDetails(self.repo, self.candles)
        self.fetch = AsyncMock(side_effect=AssertionError("Reporting must not request candles"))
        patcher = patch("upstocks.upstox.candles", self.fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_live_fill_basis_persists_and_survives_restart_without_mutation(self):
        data = snapshot()
        original = deepcopy(data)
        first = await self.details.get(data)
        second = await ExecutionDetails(self.repo, ScannerCandles()).get(data)
        self.fetch.assert_not_awaited()
        info = first["accounts"]["1:kite"][TID]
        self.assertEqual(info["points"], -10)
        self.assertEqual(info["entry_at"], DAY+"T11:02:29+05:30")
        self.assertEqual(info["high"]["points"], 20)
        self.assertEqual(first, second)
        self.assertEqual(data, original)

    async def test_incomplete_candles_are_not_reported_as_full_trade_peaks(self):
        self.candles._days.clear()
        self.candles.record("NSE_FO|1", "1m", date.fromisoformat(DAY), ROWS[1:])
        result = await self.details.get(snapshot())
        info = result["accounts"]["1:kite"][TID]
        self.assertEqual(info["status"], "incomplete")
        self.assertIsNone(info["high"])
        self.assertFalse(self.saved)

    async def test_cache_miss_never_fetches_and_new_scanner_rows_are_immediately_visible(self):
        self.candles._days.clear()
        first = await self.details.get(snapshot())
        self.assertEqual(first["accounts"]["1:kite"][TID]["status"], "unavailable")
        self.assertEqual(first["accounts"]["1:kite"][TID]["points"], -10)
        self.assertFalse(self.saved)
        self.candles.record("NSE_FO|1", "1m", date.fromisoformat(DAY), ROWS)
        second = await self.details.get(snapshot())
        self.assertEqual(second["accounts"]["1:kite"][TID]["high"]["points"], 20)
        self.assertEqual(second["accounts"]["1:kite"][TID]["candle_source"], "scanner")
        self.fetch.assert_not_awaited()

    async def test_open_peaks_refresh_without_caching_or_persistence(self):
        data = snapshot()
        data["state"]["orders"]["1:kite"][TID].pop("sell")
        first = await self.details.get(data)
        self.candles.record("NSE_FO|1", "1m", date.fromisoformat(DAY),
                            [[DAY+"T11:05:00+05:30", 100, 150, 80, 110]])
        second = await self.details.get(data)
        self.assertEqual(first["accounts"]["1:kite"][TID]["high"]["price"], 120)
        self.assertEqual(second["accounts"]["1:kite"][TID]["high"]["price"], 150)
        self.assertEqual(second["accounts"]["1:kite"][TID]["through"], DAY+"T11:05:00+05:30")
        self.assertFalse(self.saved)
        self.fetch.assert_not_awaited()

    async def test_in_progress_exit_minute_is_not_persisted_as_final(self):
        data = snapshot()
        data["intraday_session"]["server_now"] = DAY+"T11:04:30+05:30"
        result = await self.details.get(data)
        self.assertEqual(result["accounts"]["1:kite"][TID]["status"], "incomplete")
        self.assertFalse(self.saved)
        data["intraday_session"]["server_now"] = DAY+"T11:05:00+05:30"
        result = await self.details.get(data)
        self.assertEqual(result["accounts"]["1:kite"][TID]["status"], "available")
        self.fetch.assert_not_awaited()

    async def test_storage_failure_preserves_fills_and_never_fetches(self):
        with patch.object(self.repo, "get_json", side_effect=OSError("database unavailable")), self.assertLogs("upstocks.services.execution_details", level="WARNING"):
            result = await self.details.get(snapshot())
        info = result["accounts"]["1:kite"][TID]
        self.assertEqual(info["status"], "unavailable")
        self.assertEqual(info["points"], -10)
        self.fetch.assert_not_awaited()

    async def test_no_history_or_unfilled_order_analytics(self):
        data = snapshot()
        data["intraday_session"]["day"] = "2026-11-02"
        with patch("upstocks.upstox.candles", AsyncMock()) as fetch:
            result = await self.details.get(data)
            self.assertEqual(result["accounts"], {})
            data = snapshot()
            data["state"]["orders"]["1:kite"][TID]["buy"]["filled_qty"] = 0
            result = await self.details.get(data)
            self.assertIsNone(result["accounts"]["1:kite"][TID]["entry_at"])
            fetch.assert_not_awaited()

    async def test_updated_fill_price_invalidates_cached_points(self):
        await self.details.get(snapshot())
        data = snapshot()
        data["state"]["orders"]["1:kite"][TID]["buy"]["fill_price"] = 105
        result = await self.details.get(data)
        self.fetch.assert_not_awaited()
        self.assertEqual(result["accounts"]["1:kite"][TID]["high"]["points"], 15)
