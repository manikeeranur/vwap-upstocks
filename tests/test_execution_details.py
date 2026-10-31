"""Execution display analytics use real fills and minute candles, without trading."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

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
        self.details = ExecutionDetails(self.repo)

    async def test_live_fill_basis_persists_and_survives_restart_without_mutation(self):
        data = snapshot()
        original = deepcopy(data)
        with patch("upstocks.upstox.candles", AsyncMock(return_value=ROWS)) as fetch:
            first = await self.details.get(data)
            second = await ExecutionDetails(self.repo).get(data)
        self.assertEqual(fetch.await_count, 1)
        info = first["accounts"]["1:kite"][TID]
        self.assertEqual(info["points"], -10)
        self.assertEqual(info["entry_at"], DAY+"T11:02:29+05:30")
        self.assertEqual(info["high"]["points"], 20)
        self.assertEqual(first, second)
        self.assertEqual(data, original)

    async def test_incomplete_candles_are_not_reported_as_full_trade_peaks(self):
        with patch("upstocks.upstox.candles", AsyncMock(return_value=ROWS[1:])):
            result = await self.details.get(snapshot())
        info = result["accounts"]["1:kite"][TID]
        self.assertEqual(info["status"], "incomplete")
        self.assertIsNone(info["high"])
        self.assertFalse(self.saved)

    async def test_failure_is_cooled_down_and_fills_remain_visible(self):
        with patch("upstocks.upstox.candles", AsyncMock(side_effect=TimeoutError)) as fetch, self.assertLogs("upstocks.services.execution_details", level="WARNING"):
            first = await self.details.get(snapshot())
            await self.details.get(snapshot())
        self.assertEqual(fetch.await_count, 1)
        self.assertEqual(first["accounts"]["1:kite"][TID]["points"], -10)
        self.assertFalse(self.saved)

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
        with patch("upstocks.upstox.candles", AsyncMock(return_value=ROWS)) as fetch:
            await self.details.get(snapshot())
            data = snapshot()
            data["state"]["orders"]["1:kite"][TID]["buy"]["fill_price"] = 105
            result = await self.details.get(data)
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(result["accounts"]["1:kite"][TID]["high"]["points"], 15)
