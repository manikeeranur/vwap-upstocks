"""Background recovery must not replay stale strategy results or ignore shutdown."""
import asyncio
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from upstocks import upstox
from upstocks.database import Database
from upstocks.repository import Repository
from upstocks.services.trading import TradingService

PRESET = "frontend_live"


class ScannerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        db = Database(root / "test.sqlite3")
        db.initialize()
        self.repo = Repository(db)
        self.repo.migrate_legacy(root)
        self.repo.ensure_presets()
        self.service = TradingService(self.repo)
        self.service.WORKER_CHECK_INTERVAL = .005
        self.now = datetime(2026, 11, 3, 10, 0, tzinfo=upstox.IST)
        for p in [patch("upstocks.upstox.today_ist", return_value=self.now.date()),
                  patch("upstocks.live.now_ist", return_value=self.now),
                  patch("upstocks.live.market_open", return_value=True)]:
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.service.stop()

    async def test_unexpected_worker_exception_is_restarted_and_logged(self):
        restarted = asyncio.Event()
        calls = 0
        async def scanner():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("isolated test failure")
            restarted.set()
            await asyncio.Event().wait()
        async def reconciliation():
            await asyncio.Event().wait()
        before = deepcopy(self.service.state)
        with patch.object(self.service, "_loop", scanner), patch.object(self.service, "_reconcile_loop", reconciliation), self.assertLogs("upstocks.services.trading", level="ERROR"):
            self.service.start()
            await asyncio.wait_for(restarted.wait(), 1)
            self.assertEqual(self.service.worker_health()["restarts"], 1)
            self.assertTrue(self.service.worker_health()["running"])
            self.assertEqual(self.service.state, before)
            await self.service.stop()
        self.assertIsNone(self.service._watch_task)

    async def test_cancelled_worker_recovers_but_intentional_shutdown_does_not(self):
        entered, recovered = asyncio.Event(), asyncio.Event()
        calls = 0
        async def scanner():
            nonlocal calls
            calls += 1
            (entered if calls == 1 else recovered).set()
            await asyncio.Event().wait()
        async def reconciliation():
            await asyncio.Event().wait()
        with patch.object(self.service, "_loop", scanner), patch.object(self.service, "_reconcile_loop", reconciliation), self.assertLogs("upstocks.services.trading", level="ERROR"):
            self.service.start()
            await entered.wait()
            self.service._task.cancel()
            await asyncio.wait_for(recovered.wait(), 1)
            await self.service.stop()
            await asyncio.sleep(.02)
            self.assertEqual(calls, 2)
            self.assertFalse(self.service.worker_health()["running"])

    async def test_child_engine_cancellation_does_not_abort_other_strategies(self):
        async def engine(preset, day):
            if preset == PRESET:
                raise asyncio.CancelledError()
            return []
        with patch("upstocks.live.run_engine", engine), patch.object(self.service, "_act", AsyncMock()) as act, \
             patch.object(self.service, "_peaks", AsyncMock()):
            result = await asyncio.wait_for(self.service.scan(), 1)
            self.assertIn("interrupted", result["errors"][PRESET])
            self.assertIn("smc_live", result["results"])
            self.assertNotIn(PRESET, [call.args[0] for call in act.await_args_list])
            self.assertFalse(result["running"])

    async def test_scan_captures_engine_candles_without_report_downloads(self):
        day = self.now.date()
        rows = [[f"{day}T09:30:00+05:30", 100, 110, 95, 105],
                [f"{day}T09:31:00+05:30", 105, 120, 90, 110]]
        for preset, cfg in self.service.state["config"].items():
            cfg["on"] = preset == PRESET
        async def engine(name, scan_day):
            await upstox.candles("NSE_FO|1", "1m", scan_day, scan_day, False)
            return [{"date": str(scan_day), "instrument_key": "NSE_FO|1", "entry_time": "09:30", "entry": 100,
                     "exit_time": "09:31", "exit": 110, "status": "TARGET", "pnl": 10, "expiry": "2026-11-10"}]
        upstox._cache.clear()
        with patch("upstocks.live.run_engine", engine), \
             patch("upstocks.upstox._fetch_candles", AsyncMock(return_value=rows)) as fetch, \
             patch.object(self.service, "_act", AsyncMock()) as act:
            await self.service.scan(execute_live=False)
            await self.service._report_task
            trade = self.service.view["results"][PRESET][0]
            self.assertEqual((trade["peak_high"], trade["peak_low"]), (120, 90))
            self.assertEqual(fetch.await_count, 1)
            act.assert_not_awaited()
        upstox._cache.clear()

    async def test_external_scan_cancellation_still_propagates(self):
        entered = asyncio.Event()
        async def engine(*args):
            entered.set()
            await asyncio.Event().wait()
        with patch("upstocks.live.run_engine", engine), patch.object(self.service, "_act", AsyncMock()) as act:
            scan = asyncio.create_task(self.service.scan())
            await entered.wait()
            scan.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await scan
            act.assert_not_awaited()
            self.assertFalse(self.service.view["running"])
        await self.service.stop()
        self.assertFalse(self.service._engine_runs)

    async def test_failed_refresh_preserves_display_and_journal_but_cannot_place_orders(self):
        day = self.now.date().isoformat()
        trade = {"id": f"{PRESET}|{day}|09:30|NSE_FO|1", "symbol": "TEST", "orders": {}, "open": True, "pnl": 10}
        view = {"day": day, "scanned_at": "09:35:00", "results": {PRESET: [trade]}, "errors": {}, "running": False}
        self.repo.save_scan(view, {PRESET: {"lots": 1, "lot_size": 65, "updated": "09:35:00", "trades": [trade]}})
        self.service.view = deepcopy(view)
        for preset in self.service.state["config"]:
            self.service.state["config"][preset]["on"] = preset == PRESET
        before = self.repo.paper_view(day)
        with patch("upstocks.live.run_engine", AsyncMock(side_effect=upstox.MarketDataUnavailable("Upstox market data timed out after 3 attempts"))), \
             patch.object(self.service, "_act", AsyncMock()) as act, patch.object(self.service, "_peaks", AsyncMock()), \
             self.assertLogs("upstocks.services.trading", level="ERROR"):
            result = await self.service.scan()
        self.assertEqual(result["results"][PRESET], view["results"][PRESET])
        self.assertIn("retrying automatically", result["errors"][PRESET])
        act.assert_not_awaited()
        self.assertEqual(self.repo.paper_view(day), before)
