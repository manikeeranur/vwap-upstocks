"""Scanner provenance and bounded, cache-only reporting snapshots."""
import asyncio
from datetime import date, datetime, timedelta
import unittest
from unittest.mock import AsyncMock, patch

from upstocks import upstox
from upstocks.scanner_candles import ScannerCandles, observe

DAY = date(2026, 11, 3)
KEY = "NSE_FO|123"
ROWS = [["2026-11-03T09:30:00+05:30", 100, 110, 90, 105],
        ["2026-11-03T09:31:00+05:30", 105, 120, 95, 110]]


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = ScannerCandles()
        upstox._cache.clear()
        p = patch("upstocks.upstox.today_ist", return_value=DAY)
        p.start()
        self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await upstox.close()

    async def test_only_scanner_reads_populate_even_when_network_result_is_cached(self):
        with patch("upstocks.upstox._fetch_candles", AsyncMock(return_value=ROWS)) as fetch:
            await upstox.candles(KEY, "1m", DAY, DAY, False)
            self.assertEqual(self.store.rows(KEY, DAY), ())
            with self.store.capture(DAY):
                rows = await upstox.candles(KEY, "1m", DAY, DAY, False)
            self.assertEqual(fetch.await_count, 1)
            self.assertEqual(self.store.rows(KEY, DAY), tuple(map(tuple, rows)))
            with patch.object(self.store, "record") as record:
                await upstox.candles(KEY, "1m", DAY, DAY, False)
                record.assert_not_called()

    async def test_wider_contract_range_is_sliced_to_scanned_day(self):
        yesterday = [["2026-11-02T09:30:00+05:30", 1, 999, 1, 1]]
        with patch("upstocks.upstox._fetch_candles", AsyncMock(side_effect=[yesterday, ROWS])) as fetch:
            with self.store.capture(DAY):
                result = await upstox.candles(KEY, "1m", DAY-timedelta(days=1), DAY, False)
            self.assertEqual(result, yesterday+ROWS)
            self.assertEqual(fetch.await_count, 2)  # exactly the scanner's existing hist + intraday reads
            self.assertEqual(self.store.rows(KEY, DAY), tuple(map(tuple, ROWS)))
            self.assertEqual(self.store.rows(KEY, DAY-timedelta(days=1)), ())

    async def test_ttl_hit_cannot_turn_a_forming_candle_into_a_final_peak(self):
        received = datetime(2026, 11, 3, 9, 30, 30, tzinfo=upstox.IST).timestamp()
        with patch("upstocks.upstox._fetch_candles", AsyncMock(return_value=ROWS)) as fetch:
            with self.store.capture(DAY), patch("upstocks.upstox.time.time", return_value=received):
                await upstox.candles(KEY, "1m", DAY, DAY, False)
            self.assertEqual(self.store.rows(KEY, DAY), ())
            with self.store.capture(DAY), patch("upstocks.upstox.time.time", return_value=received+40):
                await upstox.candles(KEY, "1m", DAY, DAY, False)
            self.assertEqual(fetch.await_count, 1)
            self.assertEqual(self.store.rows(KEY, DAY), ())  # 09:30 was incomplete when actually fetched
            with self.store.capture(DAY), patch("upstocks.upstox.time.time", return_value=received+61):
                await upstox.candles(KEY, "1m", DAY, DAY, False)
            self.assertEqual(fetch.await_count, 2)  # normal scanner refresh, not a reporting request
            self.assertEqual(self.store.rows(KEY, DAY), (tuple(ROWS[0]),))

    async def test_shared_read_from_research_is_captured_by_scanner_waiter_only(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def fetch(*args):
            entered.set()
            await release.wait()
            return ROWS
        async def scanner():
            with self.store.capture(DAY):
                return await upstox.candles(KEY, "1m", DAY, DAY, False)
        with patch("upstocks.upstox._fetch_candles", AsyncMock(side_effect=fetch)) as call:
            research = asyncio.create_task(upstox.candles(KEY, "1m", DAY, DAY, False))
            await entered.wait()
            scanned = asyncio.create_task(scanner())
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(research, scanned)
            self.assertEqual(call.await_count, 1)
            self.assertEqual(self.store.rows(KEY, DAY), tuple(map(tuple, ROWS)))

    async def test_cancelled_capture_does_not_leak_into_other_requests(self):
        entered = asyncio.Event()
        async def scanner():
            with self.store.capture(DAY):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(scanner())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        observe(KEY, "1m", ROWS)
        self.assertEqual(self.store.rows(KEY, DAY), ())

    async def test_reporting_capture_failure_cannot_fail_the_engine(self):
        with patch("upstocks.upstox._fetch_candles", AsyncMock(return_value=ROWS)), \
             patch.object(self.store, "record", side_effect=RuntimeError("optional cache failed")), \
             self.assertLogs("upstocks.scanner_candles", level="WARNING"):
            with self.store.capture(DAY):
                self.assertEqual(await upstox.candles(KEY, "1m", DAY, DAY, False), ROWS)

    def test_merge_revisions_and_timezones_without_mutating_engine_rows(self):
        self.store.record(KEY, "1m", DAY, ROWS[:1])
        revised = [["2026-11-03T04:00:00Z", 100, 115, 85, 110], ROWS[1]]
        self.store.record(KEY, "1m", DAY, revised)
        result = self.store.rows(KEY, DAY)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][2:4], (115, 85))
        self.assertEqual(ROWS[0][2:4], [110, 90])
        with self.assertRaises(TypeError):
            result[0][2] = 999

    def test_bounded_cache_and_wrong_interval_or_session_are_not_used(self):
        self.store.MAX_INSTRUMENT_DAYS = 2
        self.store.record(KEY, "5m", DAY, ROWS)
        self.store.record(KEY, "1m", DAY-timedelta(days=1), ROWS)
        self.assertEqual(self.store.rows(KEY, DAY), ())
        for instrument in ("old", "current", "new"):
            self.store.record(instrument, "1m", DAY, ROWS)
        self.assertEqual(self.store.rows("old", DAY), ())
        self.assertEqual(len(self.store._days), 2)
        self.assertEqual(len(self.store.rows("current", DAY)), 2)
