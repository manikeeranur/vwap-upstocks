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
    return brokers.Credentials(broker, "key", "secret", "token", datetime.now(upstox.IST).isoformat(timespec="seconds"), "TEST-OWNER", "Test")


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

    async def repo_fill(self, qty):
        order = self.trading.state["orders"][ACC][TID]["buy"]
        order.update(filled_qty=qty, execution_status="complete")
        self.repo.save_fill(TID, ACC, "BUY", 100, None, qty, "complete")

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
        await self.trading.configure(PRESET, user=1, broker="indmoney", live=True, lots=2)
        self.repo.save_broker_keys(1, "kite", "k")
        self.repo.save_broker_token(1, "kite", "sealed", "2026-10-12T08:31:00+05:30", "AB1234", {"name": "Test Holder"})
        trade = {**TRADE, "id": TID, "open": True, "status": "EOD", "entry_time": "09:36", "entry": 300,
                 "expiry": "2026-10-13", "strike": 25000, "type": "CE"}
        with patch("upstocks.upstox.place_market_order", new=AsyncMock(return_value="u-1")) as up, \
                patch("upstocks.brokers.kite_instrument", new=AsyncMock(return_value={"tradingsymbol": "NIFTY26O1325000CE", "lot_size": 65})), \
                patch("upstocks.brokers.kite_place", new=AsyncMock(return_value="k-1")) as kite, \
                patch("upstocks.brokers.ind_instrument", new=AsyncMock(return_value={"security_id": "56998", "trading_symbol": "NIFTY 13 OCT 25000 CE", "lot_size": 65})), \
                patch("upstocks.brokers.ind_place", new=AsyncMock(return_value="DRV-1")) as ind, \
                patch("upstocks.live.market_open", return_value=True), \
                patch("upstocks.live.now_ist", return_value=datetime(2026, 10, 12, 9, 37, tzinfo=upstox.IST)):
            self.trading._credentials = lambda account: brokers.Credentials(account.split(":")[1], "k", "s", "t", "2026-10-12T08:31:00+05:30", "AB1234" if account.endswith(":kite") else "TEST-OWNER", "Test Holder" if account.endswith(":kite") else "Test")
            await self.trading._act(PRESET, [trade])
        self.assertEqual(up.await_args.args[1:3], (65, "BUY"))
        self.assertEqual(kite.await_args.args[1:4], ("NIFTY26O1325000CE", 195, "BUY"))
        orders = self.repo.state()["orders"]
        self.assertEqual(orders["1:upstox"][TID]["buy"]["order_id"], "u-1")
        self.assertEqual(orders["1:kite"][TID]["buy"]["order_id"], "k-1")
        self.assertEqual((orders["1:kite"][TID]["buy"]["client_id"], orders["1:kite"][TID]["buy"]["holder"]), ("AB1234", "Test Holder"))
        self.assertEqual(ind.await_args.args[1:4], ("56998", 130, "BUY"))
        self.assertEqual(orders["1:indmoney"][TID]["buy"]["order_id"], "DRV-1")

    async def test_peaks_are_the_high_and_low_between_entry_and_exit(self):
        rows = [["2026-10-12T09:59:00+05:30", 0, 999, 1, 0], ["2026-10-12T10:01:00+05:30", 0, 470, 455, 0],
                ["2026-10-12T11:00:00+05:30", 0, 636.7, 500, 0], ["2026-10-12T13:38:00+05:30", 0, 580, 560, 0],
                ["2026-10-12T13:39:00+05:30", 0, 999, 1, 0]]
        trade = {"id": "x", "instrument_key": "NSE_FO|1", "expiry": "2099-01-01", "entry_time": "10:01", "entry": 460,
                 "exit_time": "13:38", "open": False}
        with patch("upstocks.upstox.candles", new=AsyncMock(return_value=rows)) as candles:
            await self.trading._peaks(date(2026, 10, 12), [trade])
            await self.trading._peaks(date(2026, 10, 12), [dict(trade)])   # closed: worked out once
        self.assertEqual((trade["peak_high"], trade["peak_low"]), (636.7, 455))
        self.assertEqual(candles.await_count, 1)

    async def test_reconciliation_tracks_quantities_and_terminal_status(self):
        today = upstox.today_ist().isoformat()
        tid = f"harmonic|{today}|10:01|NSE_FO|1"
        self.trading.state["orders"] = {"1:kite": {tid: {"buy": {"order_id":"b1","qty":65,"status":"submitted","client_id":"TEST-OWNER"}}}}
        self.repo.reserve_order(tid, "1:kite", "harmonic", "BUY", self.trading.state["orders"]["1:kite"][tid]["buy"])
        self.trading._credentials = lambda account: logged_in("kite")
        rows = [{"order_id":"b1","tag":None,"qty":65,"filled_qty":20,"notional":200000,
                 "average_price":100,"status":"open","side":"BUY"}]
        with patch("upstocks.execution_brokers.order_book", AsyncMock(return_value=rows)) as book, patch.object(self.trading, "_store_charges", AsyncMock()):
            self.assertEqual(await self.trading.live_fills(1), {"1:kite":{"b1":100}})
            await self.trading.live_fills(1)
        self.assertEqual(book.await_count, 1)
        order = self.trading.state["orders"]["1:kite"][tid]["buy"]
        self.assertEqual((order["filled_qty"],order["execution_status"]),(20,"open"))
        self.assertTrue(self.trading._unresolved("1:kite"))

    def test_indmoney_token_lasts_24_hours(self):
        creds = brokers.Credentials("indmoney", "", "", "t", "2026-10-12T08:31:00+05:30")
        self.assertTrue(creds.token_valid(datetime(2026, 10, 13, 8, 30, tzinfo=upstox.IST)))
        self.assertFalse(creds.token_valid(datetime(2026, 10, 13, 8, 32, tzinfo=upstox.IST)))

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

    async def test_slow_strategy_does_not_hold_up_fast_strategy_execution(self):
        slow_entered, release, acted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def engine(preset, day):
            if preset == "smc_live":
                slow_entered.set()
                await release.wait()
            return []
        async def act(preset, trades):
            if preset == PRESET:
                acted.set()
        with patch("upstocks.live.run_engine", engine), patch.object(self.trading, "_act", act), patch.object(self.trading, "_peaks", AsyncMock()):
            task = asyncio.create_task(self.trading.scan())
            try:
                await asyncio.wait_for(slow_entered.wait(), 1)
                await asyncio.wait_for(acted.wait(), 1)
                self.assertFalse(task.done())
            finally:
                release.set()
                await task
                await self.trading.stop()

    async def test_ack_only_buy_cannot_be_sold(self):
        await self.live()
        with patch("upstocks.upstox.place_market_order", AsyncMock(return_value="ack")) as broker, patch("upstocks.live.market_open", return_value=True):
            await self.trading._order(ACC, PRESET, TID, TRADE, "BUY", "test")
            self.assertFalse(await self.trading._order(ACC, PRESET, TID, TRADE, "SELL", "test"))
            self.assertEqual(broker.await_count, 1)

    async def test_rejected_buy_with_zero_fills_cannot_be_sold(self):
        await self.live()
        with patch("upstocks.upstox.place_market_order", AsyncMock(return_value="ack")) as broker, patch("upstocks.live.market_open", return_value=True):
            await self.trading._order(ACC, PRESET, TID, TRADE, "BUY", "test")
            self.trading.state["orders"][ACC][TID]["buy"].update(execution_status="rejected", filled_qty=0)
            self.assertFalse(await self.trading._order(ACC, PRESET, TID, TRADE, "SELL", "test"))
            self.assertEqual(broker.await_count, 1)

    async def test_sell_uses_original_quantity_even_after_config_change(self):
        await self.live(lots=2)
        with patch("upstocks.upstox.place_market_order", new=AsyncMock(return_value="broker-1")) as broker, patch("upstocks.live.market_open", return_value=True):
            await self.trading._order(ACC, PRESET, TID, TRADE, "BUY", "test")
            self.trading.state["orders"][ACC][TID]["buy"].update(filled_qty=130, execution_status="complete")
            await self.live(False, lots=5)
            await self.trading.configure(lot_size=25)
            self.assertEqual(await self.trading.square_off(PRESET, user=1), 1)
            self.assertEqual(broker.await_args.args[1:3], (130, "SELL"))
            self.assertEqual(await self.trading.square_off(PRESET, user=1), 0)

    async def test_pause_prevents_new_buys_but_allows_existing_exit(self):
        await self.live(lots=2)
        with patch("upstocks.upstox.place_market_order", new=AsyncMock(return_value="broker-1")) as broker, patch("upstocks.live.market_open", return_value=True):
            self.assertTrue(await self.trading._order(ACC, PRESET, TID, TRADE, "BUY", "test"))
            await self.repo_fill(130)
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
