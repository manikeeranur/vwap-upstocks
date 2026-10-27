import asyncio
from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from upstocks import brokers, upstox, vwap
from upstocks.database import Database
from upstocks.repository import Repository
from upstocks.schemas import BacktestRequest
from upstocks.services.backtests import BacktestService, BacktestsBusy
from upstocks.services.trading import TradingService

PRESET = "frontend_live"
TID = "frontend_live|2026-10-12|09:36|NSE_FO|123"
TRADE = {"symbol": "NIFTY", "instrument_key": "NSE_FO|123"}
ACC = "1:upstox"


def logged_in(broker="upstox"):
    """Credentials with a token issued just now (valid for today)."""
    return brokers.Credentials(broker, "key", "secret", "token", datetime.now(upstox.IST).isoformat(timespec="seconds"))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = Database(root / "test.sqlite3")
        self.db.initialize()
        self.repo = Repository(self.db)
        self.repo.migrate_legacy(root)
        self.repo.ensure_presets()
        self.trading = self.service()

    def service(self):
        svc = TradingService(self.repo)
        svc._credentials = lambda account: logged_in(account.split(":")[1])
        return svc

    async def live(self, on=True, **kw):
        await self.trading.configure(PRESET, user=1, broker="upstox", live=on, **kw)

    async def test_order_intent_precedes_broker_and_is_never_duplicated(self):
        await self.live(lots=2)

        async def broker(*args, **kwargs):
            self.assertEqual(self.repo.state()["orders"][ACC][TID]["buy"]["status"], "pending")
            self.assertEqual(kwargs["token"], "token")       # the person's own daily token
            return "broker-1"

        with patch("upstocks.upstox.place_market_order", new=AsyncMock(side_effect=broker)) as call, patch("upstocks.live.market_open", return_value=True):
            results = await asyncio.gather(*(self.trading._order(ACC, PRESET, TID, TRADE, "BUY", "test") for _ in range(4)))
            self.assertEqual(sum(results), 1)
            self.assertEqual(call.await_count, 1)
            restarted = self.service()
            self.assertFalse(await restarted._order(ACC, PRESET, TID, TRADE, "BUY", "test"))
            self.assertEqual(call.await_count, 1)
        self.assertEqual(self.repo.state()["orders"][ACC][TID]["buy"]["qty"], 130)

    async def test_broker_timeout_is_unknown_and_never_retried(self):
        await self.live()
        with patch("upstocks.upstox.place_market_order", new=AsyncMock(side_effect=TimeoutError("timeout"))) as call, patch("upstocks.live.market_open", return_value=True), self.assertLogs("upstocks.services.trading", level="ERROR"):
            self.assertFalse(await self.trading._order(ACC, PRESET, TID, TRADE, "BUY", "test"))
            self.assertFalse(await self.trading._order(ACC, PRESET, TID, TRADE, "BUY", "test"))
            self.assertEqual(call.await_count, 1)
        self.assertEqual(self.repo.state()["orders"][ACC][TID]["buy"]["status"], "unknown")

    async def test_failed_reservation_never_calls_broker(self):
        await self.live()
        with patch.object(self.repo, "reserve_order", side_effect=sqlite3.OperationalError("disk full")), patch("upstocks.live.market_open", return_value=True), patch("upstocks.upstox.place_market_order", new=AsyncMock()) as broker:
            with self.assertRaises(sqlite3.OperationalError):
                await self.trading._order(ACC, PRESET, TID, TRADE, "BUY", "test")
            broker.assert_not_awaited()

    async def test_missing_broker_id_remains_unknown(self):
        await self.live()
        with patch("upstocks.upstox.place_market_order", new=AsyncMock(return_value="")), patch("upstocks.live.market_open", return_value=True), self.assertLogs("upstocks.services.trading", level="ERROR"):
            self.assertFalse(await self.trading._order(ACC, PRESET, TID, TRADE, "BUY", "test"))
        self.assertEqual(self.repo.state()["orders"][ACC][TID]["buy"]["status"], "unknown")

    async def test_not_logged_in_reserves_nothing_and_sends_nothing(self):
        await self.live()
        self.trading._credentials = lambda account: brokers.Credentials("upstox", "key", "secret", "", None)
        with patch("upstocks.upstox.place_market_order", new=AsyncMock()) as broker, patch("upstocks.live.market_open", return_value=True):
            self.assertFalse(await self.trading._order(ACC, PRESET, TID, TRADE, "BUY", "test"))
            self.assertFalse(await self.trading._order(ACC, PRESET, TID, TRADE, "BUY", "test"))
            broker.assert_not_awaited()
        self.assertNotIn(ACC, self.repo.state()["orders"])
        self.assertEqual(sum("skipped" in m for m in self.trading.state["log"]), 1)   # reported once

    async def test_each_broker_account_gets_its_own_order(self):
        await self.live()
        await self.trading.configure(PRESET, user=1, broker="kite", live=True, lots=3)
        trade = {**TRADE, "id": TID, "open": True, "status": "EOD", "entry_time": "09:36", "entry": 300,
                 "expiry": "2026-10-13", "strike": 25000, "type": "CE"}
        with patch("upstocks.upstox.place_market_order", new=AsyncMock(return_value="u-1")) as up, \
                patch("upstocks.brokers.kite_instrument", new=AsyncMock(return_value={"tradingsymbol": "NIFTY26O1325000CE", "lot_size": 65})), \
                patch("upstocks.brokers.kite_place", new=AsyncMock(return_value="k-1")) as kite, \
                patch("upstocks.live.market_open", return_value=True), \
                patch("upstocks.live.now_ist", return_value=datetime(2026, 10, 12, 9, 37, tzinfo=upstox.IST)):
            self.trading._credentials = lambda account: brokers.Credentials(account.split(":")[1], "k", "s", "t", "2026-10-12T08:31:00+05:30")
            await self.trading._act(PRESET, [trade])
        self.assertEqual(up.await_args.args[1:3], (65, "BUY"))
        self.assertEqual(kite.await_args.args[1:4], ("NIFTY26O1325000CE", 195, "BUY"))
        orders = self.repo.state()["orders"]
        self.assertEqual(orders["1:upstox"][TID]["buy"]["order_id"], "u-1")
        self.assertEqual(orders["1:kite"][TID]["buy"]["order_id"], "k-1")

    async def test_cancellation_recovers_unknown_intent(self):
        await self.live()
        entered = asyncio.Event()

        async def broker(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with patch("upstocks.upstox.place_market_order", new=broker), patch("upstocks.live.market_open", return_value=True):
            task = asyncio.create_task(self.trading._order(ACC, PRESET, TID, TRADE, "BUY", "test"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.repo.recover()
        self.assertEqual(self.repo.state()["orders"][ACC][TID]["buy"]["status"], "unknown")

    async def test_disable_during_fetch_prevents_new_buy(self):
        await self.live()
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
            await self.live(False)
            finish.set()
            await task
            broker.assert_not_awaited()

    async def test_sell_uses_original_quantity_even_after_config_change(self):
        await self.live(lots=2)
        with patch("upstocks.upstox.place_market_order", new=AsyncMock(return_value="broker-1")) as broker, patch("upstocks.live.market_open", return_value=True):
            await self.trading._order(ACC, PRESET, TID, TRADE, "BUY", "test")
            await self.live(False, lots=5)
            await self.trading.configure(lot_size=25)
            self.assertEqual(await self.trading.square_off(PRESET, user=1), 1)
            self.assertEqual(broker.await_args.args[1:3], (130, "SELL"))
            self.assertEqual(await self.trading.square_off(PRESET, user=1), 0)

    async def test_pause_prevents_new_buys_but_allows_existing_exit(self):
        await self.live(lots=2)
        with patch("upstocks.upstox.place_market_order", new=AsyncMock(return_value="broker-1")) as broker, patch("upstocks.live.market_open", return_value=True):
            self.assertTrue(await self.trading._order(ACC, PRESET, TID, TRADE, "BUY", "test"))
            await self.trading.configure(user=1, entries_paused=True)
            self.assertFalse(await self.trading._order(ACC, PRESET, TID + '2', TRADE, "BUY", "test"))
            restarted = self.service()
            self.assertFalse(await restarted._order(ACC, PRESET, TID + '2', TRADE, "BUY", "test"))
            # Automatic exit decisions still run while the entry gate is paused.
            await restarted._act(PRESET, [{**TRADE, 'id': TID, 'open': False, 'status': 'SL'}])
            self.assertEqual(broker.await_count, 2)
            self.assertEqual(broker.await_args.args[1:3], (130, "SELL"))
            self.assertTrue(restarted.state['accounts'][ACC]['live'][PRESET])

    async def test_pause_during_scan_records_evidence_without_buying(self):
        await self.live()
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
            await self.trading.configure(user=1, entries_paused=True)
            finish.set()
            result = await task
            self.assertTrue(result['results'][PRESET])
            broker.assert_not_awaited()
        with self.db.connect() as conn:
            payload = json.loads(conn.execute("SELECT payload FROM decision_evidence WHERE stage='SCAN'").fetchone()[0])
            self.assertTrue(payload['context']['paused']['1'])
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
