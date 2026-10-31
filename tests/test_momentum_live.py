import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from upstocks import execution_brokers, upstox
from upstocks.database import Database
from upstocks.repository import Repository
from upstocks.services.momentum_live import MomentumLiveService
from upstocks.services.trading import TradingService
from upstocks.stocks.domain import PortfolioError, StaleRevision, decide

INST = {"symbol": "AAA", "isin": "INEAAA", "key": "NSE_EQ|INEAAA", "rank": 1, "above_sma": True}
OTHER = {"symbol": "BBB", "isin": "INEBBB", "key": "NSE_EQ|INEBBB", "rank": 1, "above_sma": True}
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=upstox.IST)


class MomentumTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "db.sqlite")
        self.db.initialize()
        self.repo = Repository(self.db)
        self.repo.migrate_legacy(Path(self.tmp.name))
        self.repo.ensure_presets()
        self.uid2 = self.repo.add_user("second", "!")
        self.trading = TradingService(self.repo)
        self.trading._credentials = lambda account: SimpleNamespace(
            token_valid=lambda now=None: True, broker=account.split(":")[1], account=account
        )
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                repository=self.repo,
                database=self.db,
                trading=self.trading,
                momentum=SimpleNamespace(live_ranking=AsyncMock()),
            )
        )
        self.svc = MomentumLiveService(self.app)
        self.svc._market_day = NOW.date()
        self.svc.load()
        self.svc._price = AsyncMock(return_value=10_000)
        self.svc._tick_size = AsyncMock(return_value=5)
        self.store = self.svc.store
        self.pid = "1:kite"
        self.store.configure(
            self.pid, 0, enabled=True, paused=False, usable_capital=5_000_000, client_id="owner"
        )
        self.evidence = {
            "broker": "kite",
            "client_id": "owner",
            "cash": 5_000_000,
            "orders": [],
            "holdings": [],
            "positions": [],
        }
        self.plan = {"month": "2026-11", "day": "2026-11-02", "sell": None, "buy": INST, "reason": "Buy AAA"}

    async def p(self):
        return await self.svc._get(self.pid)

    def reserve(self, side="BUY", inst=INST, qty=100, cycle="2026-11"):
        return self.store.reserve(self.pid, cycle, side, inst, qty, 10_100, "owner")

    def fill(self, order, qty, status="complete", price=10_000):
        self.store.apply(
            order["id"],
            {
                "order_id": "broker-" + order["id"],
                "status": status,
                "filled_qty": qty,
                "notional": qty * price,
            },
        )

    async def test_acknowledgement_does_not_create_position_or_spend_cash(self):
        with patch.object(execution_brokers, "place_stock_limit", AsyncMock(return_value="ack")):
            await self.svc._execute(await self.p(), self.plan, "BUY", INST, self.evidence, object())
        p = await self.p()
        self.assertIsNone(p["position"])
        self.assertEqual(p["cash"], 5_000_000)
        self.assertEqual(p["orders"][0]["status"], "submitted")

    async def test_partial_fills_are_idempotent_and_survive_restart(self):
        order = self.reserve()
        self.fill(order, 40, "open")
        cash = (await self.p())["cash"]
        self.fill(order, 40, "open")
        self.assertEqual((await self.p())["cash"], cash)
        other = MomentumLiveService(self.app)
        other.load()
        self.assertEqual((await other._get(self.pid))["position"]["qty"], 40)
        self.fill(order, 100)
        self.assertEqual((await self.p())["position"]["qty"], 100)
        self.fill(order, 40, "open")
        self.assertEqual((await self.p())["position"]["qty"], 100)

    async def test_partial_sell_keeps_unsold_position(self):
        buy = self.reserve()
        self.fill(buy, 100)
        sell = self.reserve("SELL")
        self.fill(sell, 40, "open")
        self.assertEqual((await self.p())["position"]["qty"], 60)
        with self.assertRaisesRegex(PortfolioError, "Reconcile"):
            self.reserve(inst=OTHER, cycle="2026-12")

    async def test_rejected_exit_cannot_overwrite_holding(self):
        buy = self.reserve(cycle="2026-10")
        self.fill(buy, 100)
        sell = self.reserve("SELL")
        self.fill(sell, 0, "rejected")
        self.store.plan(self.pid, {**self.plan, "sell": INST, "buy": OTHER})
        self.evidence["holdings"] = [{"tradingsymbol": "AAA", "opening_quantity": 100}]
        with (
            patch.object(execution_brokers, "stock_snapshot", AsyncMock(return_value=self.evidence)),
            patch.object(execution_brokers, "place_stock_limit", AsyncMock()) as send,
            patch("upstocks.live.market_open", return_value=True),
        ):
            with self.assertRaisesRegex(PortfolioError, "cancelled/rejected"):
                await self.svc._portfolio_tick(self.pid, NOW, None)
            send.assert_not_awaited()
        self.assertEqual((await self.p())["position"]["symbol"], "AAA")

    async def test_global_pause_blocks_live_buy_but_not_paper(self):
        self.repo.configure(user=1, entries_paused=True)
        self.trading.reload_people()
        with patch.object(execution_brokers, "place_stock_limit", AsyncMock()) as send:
            with self.assertRaisesRegex(PortfolioError, "paused"):
                await self.svc._execute(await self.p(), self.plan, "BUY", INST, self.evidence, object())
            send.assert_not_awaited()
        paper = await self.svc._get("1:paper")
        await self.svc._execute(paper, self.plan, "BUY", INST)
        self.assertGreater((await self.svc._get("1:paper"))["position"]["qty"], 0)

    async def test_pause_preserves_exits_and_paper_disable_is_independent(self):
        order = self.reserve()
        self.fill(order, 100)
        p = await self.p()
        self.store.configure(
            self.pid,
            p["revision"],
            enabled=True,
            paused=True,
            usable_capital=p["usable_capital"],
            client_id="owner",
        )
        self.repo.configure(user=1, entries_paused=True)
        self.store.configure("1:paper", 0, enabled=False, paused=True, usable_capital=5_000_000)
        self.assertIsNotNone(self.reserve("SELL"))
        self.assertTrue((await self.p())["enabled"])

    async def test_size_uses_smallest_budget_and_reserves_costs(self):
        self.evidence["cash"] = 750_000
        with patch.object(execution_brokers, "place_stock_limit", AsyncMock(return_value="ack")):
            await self.svc._execute(await self.p(), self.plan, "BUY", INST, self.evidence, object())
        order = (await self.p())["orders"][0]
        self.assertLessEqual(order["reserved"], 750_000)
        self.assertGreater(order["reserved"], order["qty"] * order["limit_paise"])

    async def test_no_duplicate_submission_after_timeout_and_restart(self):
        with patch.object(
            execution_brokers, "place_stock_limit", AsyncMock(side_effect=TimeoutError("test"))
        ) as send:
            with self.assertRaises(TimeoutError):
                await self.svc._execute(await self.p(), self.plan, "BUY", INST, self.evidence, object())
            self.svc.load()
            with self.assertRaises(PortfolioError):
                await self.svc._execute(await self.p(), self.plan, "BUY", INST, self.evidence, object())
            self.assertEqual(send.await_count, 1)
        self.assertIsNone((await self.p())["position"])

    async def test_unknown_is_reconciled_by_unique_tag(self):
        order = self.reserve()
        self.store.submission(order["id"], error="timeout")
        row = {
            "order_id": "real",
            "tag": order["id"],
            "qty": 100,
            "filled_qty": 100,
            "notional": 1_000_000,
            "status": "complete",
            "side": "BUY",
            "symbol": "AAA",
            "isin": "INEAAA",
            "product": "CNC",
        }
        evidence = {**self.evidence, "orders": [row]}
        await self.svc._reconcile(await self.p(), evidence)
        self.assertEqual((await self.p())["position"]["qty"], 100)

    async def test_manual_holding_or_manual_order_blocks_buy(self):
        for evidence in (
            {**self.evidence, "holdings": [{"tradingsymbol": "AAA", "opening_quantity": 10}]},
            {
                **self.evidence,
                "orders": [
                    {
                        "order_id": "manual",
                        "filled_qty": 1,
                        "status": "complete",
                        "symbol": "AAA",
                        "product": "CNC",
                        "side": "BUY",
                    }
                ],
            },
        ):
            with patch.object(execution_brokers, "place_stock_limit", AsyncMock()) as send:
                with self.assertRaises(PortfolioError):
                    await self.svc._execute(await self.p(), self.plan, "BUY", INST, evidence, object())
                send.assert_not_awaited()

    async def test_user_ownership_is_not_insertion_order(self):
        self.trading.state["accounts"] = {
            f"{self.uid2}:kite": {"user": self.uid2, "broker": "kite"},
            "1:kite": {"user": 1, "broker": "kite"},
        }
        self.assertEqual((await self.svc._creds(await self.p())).account, "1:kite")
        snap = await self.svc.snapshot(self.uid2)
        self.assertTrue(all(p["user_id"] == self.uid2 for p in snap["portfolios"]))
        self.assertTrue(all(not p["orders"] for p in snap["portfolios"]))

    async def test_broker_login_change_blocks_reconciliation(self):
        with self.assertRaisesRegex(PortfolioError, "owner"):
            await self.svc._reconcile(await self.p(), {**self.evidence, "client_id": "someone-else"})

    async def test_revision_conflict_preserves_settings(self):
        with self.assertRaises(StaleRevision):
            self.store.configure(self.pid, 0, enabled=False, paused=True, usable_capital=100_000)
        self.assertTrue((await self.p())["enabled"])

    async def test_budget_change_rejected_while_exposed_or_pending(self):
        order = self.reserve()
        for filled in (False, True):
            if filled:
                self.fill(order, 100)
            p = await self.p()
            with self.assertRaisesRegex(PortfolioError, "flat"):
                self.store.configure(
                    self.pid, p["revision"], enabled=True, paused=False, usable_capital=1_000_000
                )

    async def test_reservation_failure_never_calls_broker(self):
        with (
            patch.object(self.store, "event", side_effect=RuntimeError("disk")),
            patch.object(execution_brokers, "place_stock_limit", AsyncMock()) as send,
        ):
            with self.assertRaisesRegex(RuntimeError, "disk"):
                await self.svc._execute(await self.p(), self.plan, "BUY", INST, self.evidence, object())
            send.assert_not_awaited()
        self.assertEqual((await self.p())["orders"], [])

    async def test_append_only_fill_and_event_evidence(self):
        import sqlite3

        for table in ("stock_events", "stock_fills"):
            with self.assertRaises(sqlite3.IntegrityError), self.db.connect(write=True) as c:
                if table == "stock_fills":
                    c.execute(
                        "INSERT INTO stock_orders(id,portfolio_id,cycle,side,symbol,isin,instrument_key,qty,limit_paise,reserved,client_id,status,created_at,updated_at) VALUES('x','1:kite','x','BUY','AAA','INEAAA','NSE_EQ|INEAAA',1,100,100,'owner','complete','x','x')"
                    )
                    c.execute(
                        "INSERT INTO stock_fills(order_id,cumulative_qty,qty,notional,fee_provision,created_at) VALUES('x',1,1,100,0,'x')"
                    )
                c.execute(f"DELETE FROM {table}")

    async def test_incomplete_ranking_never_becomes_a_trade(self):
        self.svc._ranking_day = NOW.date()
        self.svc._ranking_task = asyncio.create_task(
            asyncio.sleep(0, result={"date": "2026-10-30", "failed": 1, "ranking": [OTHER]})
        )
        await self.svc._ranking_task
        with self.assertRaisesRegex(PortfolioError, "Incomplete"):
            await self.svc._ranking(NOW.date())

    def test_missing_held_stock_does_not_mean_sell(self):
        with self.assertRaisesRegex(PortfolioError, "missing"):
            decide([OTHER], "AAA", 5)

    def test_keep_and_rotate_rules(self):
        self.assertEqual(decide([INST], "AAA", 5)[:2], (None, None))
        self.assertEqual(decide([{**INST, "rank": 6}, OTHER], "AAA", 5)[1]["symbol"], "BBB")

    async def test_quote_requires_recent_exchange_timestamp(self):
        self.svc._price = MomentumLiveService._price.__get__(self.svc)
        quote = {
            "instrument_token": INST["key"],
            "last_price": 100,
            "last_trade_time": str(int(NOW.timestamp() * 1000)),
        }
        with (
            patch("upstocks.live.now_ist", return_value=NOW),
            patch("upstocks.upstox._get", AsyncMock(return_value={"data": {"AAA": quote}})),
        ):
            self.assertEqual(await self.svc._price(INST["key"]), 10000)
            quote["last_trade_time"] = str(int((NOW.timestamp() - 121) * 1000))
            with self.assertRaisesRegex(PortfolioError, "stale"):
                await self.svc._price(INST["key"])

    async def test_paper_restart_can_cancel_unfilled_intent_without_broker(self):
        self.store.reserve("1:paper", "2026-11", "BUY", INST, 10, 10000, "")
        self.svc.load()
        with patch.object(execution_brokers, "stock_snapshot", AsyncMock()) as broker:
            await self.svc.reconcile(1, "paper")
            broker.assert_not_awaited()
        paper = await self.svc._get("1:paper")
        self.assertEqual(paper["orders"][0]["status"], "cancelled")
        self.assertIsNone(paper["position"])
        self.assertTrue(paper["paused"])

    async def test_legacy_records_are_preserved_and_block_live(self):
        self.repo.set_json(
            "momentum_tuned", {"orders": [{"order_id": "old"}], "positions": {"kite": {"qty": 20}}}
        )
        self.svc.load()
        p = await self.p()
        self.assertFalse(p["enabled"])
        self.assertIn("Legacy", p["blocked"])
        self.assertEqual(self.repo.get_json("momentum_tuned")["orders"][0]["order_id"], "old")

    async def test_manual_stock_after_completed_past_rotation_does_not_block(self):
        buy = self.reserve()
        self.fill(buy, 100)
        sell = self.reserve("SELL")
        self.fill(sell, 100)
        with self.db.connect(write=True) as c:
            c.execute("UPDATE stock_orders SET created_at='2026-09-01T10:00:00Z'")
        evidence = {**self.evidence, "holdings": [{"symbol": "AAA", "opening_quantity": 40}]}
        result = await self.svc._reconcile(await self.p(), evidence)
        self.assertIsNone(result["position"])

    async def test_current_day_exit_still_verifies_no_remaining_holding(self):
        buy = self.reserve()
        self.fill(buy, 100)
        sell = self.reserve("SELL")
        self.fill(sell, 100)
        evidence = {**self.evidence, "holdings": [{"symbol": "AAA", "opening_quantity": 40}]}
        with self.assertRaisesRegex(PortfolioError, "Ownership"):
            await self.svc._reconcile(await self.p(), evidence)

    async def test_market_data_outage_does_not_prevent_broker_reconciliation(self):
        self.svc._market_day = None
        order = self.reserve()
        self.store.submission(order["id"], "b1")
        row = {
            **INST,
            "order_id": "b1",
            "tag": order["id"],
            "side": "BUY",
            "qty": 100,
            "filled_qty": 100,
            "status": "complete",
            "notional": 1_000_000,
            "average_price": 100,
            "product": "CNC",
        }
        with (
            patch("upstocks.upstox.candles", AsyncMock(side_effect=TimeoutError())),
            patch.object(
                execution_brokers,
                "stock_snapshot",
                AsyncMock(return_value={**self.evidence, "orders": [row]}),
            ),
            self.assertLogs("upstocks.services.momentum_live", level="WARNING"),
        ):
            await self.svc.tick(NOW)
        self.assertEqual((await self.p())["position"]["qty"], 100)
        self.assertIn("Market session", self.svc.last_error)
