import asyncio
from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from upstocks import upstox, vwap
from upstocks.database import Database
from upstocks.repository import Repository
from upstocks.schemas import BacktestRequest
from upstocks.services.backtests import BacktestService, BacktestsBusy
from upstocks.services.trading import TradingService

PRESET = "frontend_live"
TID = "frontend_live|2026-10-12|09:36|NSE_FO|123"
TRADE = {"symbol": "NIFTY", "instrument_key": "NSE_FO|123"}


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = Database(root / "test.sqlite3")
        self.db.initialize()
        self.repo = Repository(self.db)
        self.repo.migrate_legacy(root)
        self.trading = TradingService(self.repo)

    async def test_order_intent_precedes_broker_and_is_never_duplicated(self):
        await self.trading.configure(PRESET, live=True, lots=2)

        async def broker(*args):
            self.assertEqual(self.repo.state()["orders"][TID]["buy"]["status"], "pending")
            return "broker-1"

        with patch("upstocks.upstox.place_market_order", new=AsyncMock(side_effect=broker)) as call, patch("upstocks.live.market_open", return_value=True):
            results = await asyncio.gather(*(self.trading._order(PRESET, TID, TRADE, "BUY", "test") for _ in range(4)))
            self.assertEqual(sum(results), 1)
            self.assertEqual(call.await_count, 1)
            restarted = TradingService(self.repo)
            self.assertFalse(await restarted._order(PRESET, TID, TRADE, "BUY", "test"))
            self.assertEqual(call.await_count, 1)
        self.assertEqual(self.repo.state()["orders"][TID]["buy"]["qty"], 130)

    async def test_broker_timeout_is_unknown_and_never_retried(self):
        await self.trading.configure(PRESET, live=True)
        with patch("upstocks.upstox.place_market_order", new=AsyncMock(side_effect=TimeoutError("timeout"))) as call, patch("upstocks.live.market_open", return_value=True), self.assertLogs("upstocks.services.trading", level="ERROR"):
            self.assertFalse(await self.trading._order(PRESET, TID, TRADE, "BUY", "test"))
            self.assertFalse(await self.trading._order(PRESET, TID, TRADE, "BUY", "test"))
            self.assertEqual(call.await_count, 1)
        self.assertEqual(self.repo.state()["orders"][TID]["buy"]["status"], "unknown")

    async def test_failed_reservation_never_calls_broker(self):
        await self.trading.configure(PRESET, live=True)
        with patch.object(self.repo, "reserve_order", side_effect=sqlite3.OperationalError("disk full")), patch("upstocks.live.market_open", return_value=True), patch("upstocks.upstox.place_market_order", new=AsyncMock()) as broker:
            with self.assertRaises(sqlite3.OperationalError):
                await self.trading._order(PRESET, TID, TRADE, "BUY", "test")
            broker.assert_not_awaited()

    async def test_missing_broker_id_remains_unknown(self):
        await self.trading.configure(PRESET, live=True)
        with patch("upstocks.upstox.place_market_order", new=AsyncMock(return_value="")), patch("upstocks.live.market_open", return_value=True), self.assertLogs("upstocks.services.trading", level="ERROR"):
            self.assertFalse(await self.trading._order(PRESET, TID, TRADE, "BUY", "test"))
        self.assertEqual(self.repo.state()["orders"][TID]["buy"]["status"], "unknown")

    async def test_cancellation_recovers_unknown_intent(self):
        await self.trading.configure(PRESET, live=True)
        entered = asyncio.Event()

        async def broker(*args):
            entered.set()
            await asyncio.Event().wait()

        with patch("upstocks.upstox.place_market_order", new=broker), patch("upstocks.live.market_open", return_value=True):
            task = asyncio.create_task(self.trading._order(PRESET, TID, TRADE, "BUY", "test"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.repo.recover()
        self.assertEqual(self.repo.state()["orders"][TID]["buy"]["status"], "unknown")

    async def test_disable_during_fetch_prevents_new_buy(self):
        await self.trading.configure(PRESET, live=True)
        entered, finish = asyncio.Event(), asyncio.Event()
        today = date(2026, 10, 12)

        async def engine(preset, day):
            if preset != PRESET:
                return []
            entered.set()
            await finish.wait()
            return [{**TRADE, "date": str(today), "entry_time": "09:36", "entry": 300, "status": "EOD"}]

        with patch("upstocks.live.run_engine", new=engine), patch("upstocks.live.market_open", return_value=True), patch("upstocks.upstox.today_ist", return_value=today), patch("upstocks.live.now_ist", return_value=datetime(2026, 10, 12, 9, 37, tzinfo=upstox.IST)), patch("upstocks.upstox.place_market_order", new=AsyncMock()) as broker:
            task = asyncio.create_task(self.trading.scan())
            await entered.wait()
            await self.trading.configure(PRESET, live=False)
            finish.set()
            await task
            broker.assert_not_awaited()

    async def test_sell_uses_original_quantity_even_after_config_change(self):
        await self.trading.configure(PRESET, live=True, lots=2)
        with patch("upstocks.upstox.place_market_order", new=AsyncMock(return_value="broker-1")) as broker, patch("upstocks.live.market_open", return_value=True):
            await self.trading._order(PRESET, TID, TRADE, "BUY", "test")
            await self.trading.configure(PRESET, live=False, lots=5, lot_size=25)
            self.assertEqual(await self.trading.square_off(PRESET), 1)
            self.assertEqual(broker.await_args.args[1:3], (130, "SELL"))
            self.assertEqual(await self.trading.square_off(PRESET), 0)

    async def test_pause_prevents_new_buys_but_allows_existing_exit(self):
        await self.trading.configure(PRESET, live=True, lots=2)
        with patch("upstocks.upstox.place_market_order", new=AsyncMock(return_value="broker-1")) as broker, patch("upstocks.live.market_open", return_value=True):
            self.assertTrue(await self.trading._order(PRESET, TID, TRADE, "BUY", "test"))
            await self.trading.configure(entries_paused=True)
            self.assertFalse(await self.trading._order(PRESET, TID + '2', TRADE, "BUY", "test"))
            restarted = TradingService(self.repo)
            self.assertFalse(await restarted._order(PRESET, TID + '2', TRADE, "BUY", "test"))
            # Automatic exit decisions still run while the entry gate is paused.
            await restarted._act(PRESET, [{**TRADE, 'id': TID, 'open': False, 'status': 'SL'}])
            self.assertEqual(broker.await_count, 2)
            self.assertEqual(broker.await_args.args[1:3], (130, "SELL"))
            self.assertTrue(restarted.state['config'][PRESET]['live'])

    async def test_pause_during_scan_records_evidence_without_buying(self):
        await self.trading.configure(PRESET, live=True)
        entered, finish = asyncio.Event(), asyncio.Event()
        today = date(2026, 10, 12)

        async def engine(preset, day):
            if preset != PRESET:
                return []
            entered.set()
            await finish.wait()
            return [{**TRADE, "date": str(today), "entry_time": "09:36", "entry": 300, "status": "EOD"}]

        with patch("upstocks.live.run_engine", new=engine), patch("upstocks.live.market_open", return_value=True), patch("upstocks.upstox.today_ist", return_value=today), patch("upstocks.live.now_ist", return_value=datetime(2026, 10, 12, 9, 37, tzinfo=upstox.IST)), patch("upstocks.upstox.place_market_order", new=AsyncMock()) as broker:
            task = asyncio.create_task(self.trading.scan())
            await entered.wait()
            await self.trading.configure(entries_paused=True)
            finish.set()
            result = await task
            self.assertTrue(result['results'][PRESET])
            broker.assert_not_awaited()
        with self.db.connect() as conn:
            payload = json.loads(conn.execute("SELECT payload FROM decision_evidence WHERE stage='SCAN'").fetchone()[0])
            self.assertTrue(payload['context']['entries_paused'])
            self.assertFalse(payload['context']['is_replay'])
            self.assertEqual(payload['context']['revision'], 2)

    async def test_backtest_results_and_capacity_and_shutdown(self):
        service = BacktestService(self.repo, max_jobs=1)
        entered = asyncio.Event()

        async def fetch(*args):
            entered.set()
            await asyncio.Event().wait()

        settings = vwap.Settings(start=date(2026, 10, 1), end=date(2026, 10, 2))
        with patch("upstocks.vwap.trading_days_and_spot", new=fetch):
            jid = await service.submit(settings, PRESET)
            await entered.wait()
            with self.assertRaises(BacktestsBusy):
                await service.submit(settings, PRESET)
            await service.stop()
        self.assertEqual(self.repo.get_job(jid)["status"], "error")
        self.assertEqual(service.tasks, {})

    async def test_successful_backtest_saves_each_day(self):
        service = BacktestService(self.repo)
        settings = vwap.Settings(start=date(2026, 10, 1), end=date(2026, 10, 2))

        async def run(day, *args):
            return SimpleNamespace(date=day.isoformat(), expiry=None, note="No trades", trades=[])

        with patch("upstocks.vwap.trading_days_and_spot", new=AsyncMock(return_value={"2026-10-01": [], "2026-10-02": []})), patch("upstocks.vwap.run_day", new=run):
            jid = await service.submit(settings, PRESET)
            await service.tasks[jid]
        job = self.repo.get_job(jid)
        self.assertEqual((job["status"], job["done"], job["total"]), ("done", 2, 2))

    def test_all_presets_preserve_engine_settings(self):
        from dataclasses import replace
        for preset in vwap.PRESETS:
            request = BacktestRequest(start="2026-10-01", end="2026-10-02", preset=preset)
            expected = replace(vwap.Settings(start=date(2026, 10, 1), end=date(2026, 10, 2)), **vwap.PRESETS[preset])
            self.assertEqual(request.settings(), expected)


if __name__ == "__main__":
    unittest.main()
