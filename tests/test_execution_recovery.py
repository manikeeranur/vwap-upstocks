import csv
from datetime import datetime, timedelta
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from upstocks import execution_brokers, momentum, upstox
from upstocks.database import Database, MIGRATIONS
from upstocks.order_evidence import apply_evidence, combine, current_attempt
from upstocks.repository import Repository
from upstocks.services.momentum_live import MomentumLiveService
from upstocks.services.trading import TradingService
from upstocks.stocks.domain import PortfolioError, StaleRevision
from upstocks.stocks.statements import COLUMNS, parse_statement

INST = {"symbol": "AAA", "isin": "INEAAA", "key": "NSE_EQ|INEAAA", "rank": 1, "above_sma": True}
NOW = datetime(2026, 11, 12, 12, tzinfo=upstox.IST)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = Database(self.root / "db.sqlite")
        self.db.initialize()
        self.repo = Repository(self.db)
        self.repo.migrate_legacy(self.root)
        self.repo.ensure_presets()
        self.trading = TradingService(self.repo)
        self.trading._credentials = lambda account: SimpleNamespace(
            token_valid=lambda now=None: True, client_id="owner", holder="Test", broker=account.split(":")[1]
        )
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                database=self.db,
                repository=self.repo,
                trading=self.trading,
                momentum=SimpleNamespace(runs=lambda: [], run=lambda run_id: None),
            )
        )
        self.svc = MomentumLiveService(self.app)
        self.svc.load()
        self.svc._market_day = NOW.date()
        self.svc._price = AsyncMock(return_value=10000)
        self.svc._tick_size = AsyncMock(return_value=5)
        self.store = self.svc.store
        self.pid = "1:kite"
        self.store.configure(
            self.pid, 0, enabled=True, paused=False, usable_capital=5000000, client_id="owner"
        )
        self.evidence = {
            "broker": "kite",
            "client_id": "owner",
            "cash": 5000000,
            "holdings": [],
            "positions": [],
            "orders": [],
        }

    def portfolio(self):
        return next(p for p in self.store.snapshot(1) if p["id"] == self.pid)

    def order(self, side="BUY", qty=10, cycle="2026-10"):
        return self.store.reserve(self.pid, cycle, side, INST, qty, 20000, "owner")

    def fill(self, order, qty=10, status="complete", price=100):
        self.store.apply(
            order["id"],
            {
                "order_id": "b-" + order["id"],
                "filled_qty": qty,
                "notional": qty * price * 100,
                "status": status,
            },
        )
        return next(o for o in self.portfolio()["orders"] if o["id"] == order["id"])

    def row(self, order, **changes):
        return {
            "client_id": "owner",
            "order_id": order["broker_id"] or "b-" + order["id"],
            "intent_id": order["id"],
            "symbol": order["symbol"],
            "isin": order["isin"],
            "side": order["side"],
            "quantity": order["qty"],
            "filled_quantity": order["filled_qty"],
            "average_price": order["notional"] / max(1, order["filled_qty"]) / 100,
            "status": order["status"],
            "trade_date": datetime.fromisoformat(order["created_at"])
            .astimezone(upstox.IST)
            .date()
            .isoformat(),
            "total_charges": 5,
            **changes,
        }

    def csv(self, rows):
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        return out.getvalue()

    def preview(self, rows):
        return self.svc.reconciliation.preview(1, "kite", "contract-note-1", self.csv(rows), "owner", NOW)

    def apply(self, doc):
        return self.svc.reconciliation.apply(1, doc["document_id"], NOW)

    async def test_final_charges_revalue_closed_trade_and_are_idempotent(self):
        buy = self.fill(self.order())
        sell = self.fill(self.order("SELL"), price=120)
        self.assertIsNone(self.portfolio()["net_realized"])
        doc = self.preview([self.row(sell, total_charges=7), self.row(buy, total_charges=5)])
        self.apply(doc)
        p = self.portfolio()
        self.assertEqual(p["net_realized"], 18800)
        self.assertEqual(p["cash"], 5018800)
        self.assertIsNone(p["position"])
        self.apply(doc)
        self.assertEqual(self.portfolio()["cash"], p["cash"])
        again = self.preview([self.row(sell, total_charges=7), self.row(buy, total_charges=5)])
        self.assertEqual(again["status"], "applied")

    async def test_charges_after_partial_exit_reallocate_remaining_basis(self):
        buy = self.fill(self.order())
        sell = self.fill(self.order("SELL"), qty=4, status="cancelled", price=120)
        self.apply(self.preview([self.row(buy, total_charges=10), self.row(sell, total_charges=2)]))
        p = self.portfolio()
        self.assertEqual(p["position"]["qty"], 6)
        self.assertEqual(p["position"]["cost"], 60600)
        self.assertEqual(p["net_realized"], 7400)

    async def test_manual_or_wrong_account_statement_is_rejected(self):
        buy = self.fill(self.order())
        for change in (
            {"order_id": "manual", "intent_id": ""},
            {"client_id": "different"},
            {"symbol": "OTHER"},
            {"isin": "WRONG"},
            {"quantity": 11},
            {"trade_date": "2001-01-01"},
        ):
            with self.subTest(change=change), self.assertRaises(PortfolioError):
                self.preview([self.row(buy, **change)])

    async def test_preview_cannot_be_applied_by_another_user(self):
        doc = self.preview([self.row(self.fill(self.order()))])
        self.repo.add_user("other", "!")
        with self.assertRaisesRegex(PortfolioError, "not found"):
            self.svc.reconciliation.apply(2, doc["document_id"], NOW)

    async def test_preview_expires_and_changed_fills_require_new_review(self):
        buy = self.fill(self.order(), 4, "open")
        doc = self.preview([self.row(buy, status="complete", filled_quantity=10, average_price=100)])
        with self.assertRaisesRegex(PortfolioError, "expired"):
            self.svc.reconciliation.apply(1, doc["document_id"], NOW + timedelta(hours=1))
        self.fill(buy, 6, "open")
        with self.assertRaises(StaleRevision):
            self.apply(doc)
        self.assertEqual(self.portfolio()["position"]["qty"], 6)

    async def test_noop_broker_refresh_does_not_invalidate_statement_preview(self):
        buy = self.fill(self.order())
        doc = self.preview([self.row(buy)])
        self.fill(buy)
        self.apply(doc)
        self.assertEqual(self.portfolio()["missing_charges"], 0)

    async def test_statement_transaction_rolls_back_impossible_ownership(self):
        buy = self.fill(self.order())
        sell = self.order("SELL")
        self.store.submission(sell["id"], "b-" + sell["id"])
        sell = next(o for o in self.portfolio()["orders"] if o["id"] == sell["id"])
        # Rollback at the persistence boundary, after evidence updates but before commit.
        doc = self.preview(
            [self.row(buy), self.row(sell, status="complete", filled_quantity=10, average_price=120)]
        )
        with patch.object(self.store, "revalue", side_effect=PortfolioError("invalid ownership")):
            with self.assertRaises(PortfolioError):
                self.apply(doc)
        p = self.portfolio()
        self.assertEqual(p["position"]["qty"], 10)
        self.assertIsNone(p["orders"][0]["charges_paise"])
        with self.db.connect() as c:
            self.assertEqual(
                c.execute(
                    "SELECT status FROM reconciliation_documents WHERE id=?", (doc["document_id"],)
                ).fetchone()[0],
                "preview",
            )

    async def test_parser_rejects_nonfinite_invalid_prices_and_zero_fill_fees(self):
        row = self.row(self.fill(self.order()))
        for change in (
            {"average_price": "wat"},
            {"average_price": "NaN"},
            {"average_price": "Infinity"},
            {"total_charges": "-1"},
            {"filled_quantity": 0, "status": "cancelled", "total_charges": 10},
        ):
            with self.subTest(change=change), self.assertRaises(PortfolioError):
                parse_statement(self.csv([{**row, **change}]), NOW.date())

    async def test_recovery_only_reserves_remaining_qty_and_preserves_original(self):
        self.fill(self.order())
        sell = self.fill(self.order("SELL", cycle="2026-11"), 4, "cancelled", 120)
        p = self.portfolio()
        self.store.queue_recovery(self.pid, p["revision"], sell["id"], (NOW + timedelta(days=7)).isoformat())
        p = self.portfolio()
        recovery = p["recoveries"][0]
        child = self.store.reserve(
            self.pid, "recovery:" + recovery["id"], "SELL", INST, 6, 12000, "owner", sell["id"]
        )
        self.assertEqual(child["qty"], 6)
        self.assertEqual(child["origin_cycle"], "2026-11")
        self.fill(child, 6, price=120)
        self.assertIsNone(self.portfolio()["position"])
        self.assertEqual(self.portfolio()["orders"][1]["filled_qty"], 4)
        with self.assertRaises(PortfolioError):
            self.store.reserve(self.pid, "other-retry", "SELL", INST, 6, 12000, "owner", sell["id"])

    async def test_queued_recovery_can_be_cancelled_and_explicitly_reapproved(self):
        self.fill(self.order())
        sell = self.fill(self.order("SELL"), 0, "rejected")
        p = self.portfolio()
        self.store.queue_recovery(self.pid, p["revision"], sell["id"], (NOW + timedelta(days=7)).isoformat())
        p = self.portfolio()
        rid = p["recoveries"][0]["id"]
        self.store.cancel_recovery(self.pid, p["revision"], rid)
        self.store.queue_recovery(
            self.pid, self.portfolio()["revision"], sell["id"], (NOW + timedelta(days=7)).isoformat()
        )
        self.assertEqual(self.portfolio()["recoveries"][0]["status"], "queued")
        self.assertEqual(len(self.portfolio()["recoveries"]), 1)

    async def test_unknown_exit_cannot_be_replaced(self):
        self.fill(self.order())
        sell = self.order("SELL")
        self.store.submission(sell["id"], error="unknown")
        with self.assertRaises(PortfolioError):
            self.store.queue_recovery(
                self.pid, self.portfolio()["revision"], sell["id"], (NOW + timedelta(days=7)).isoformat()
            )

    async def test_midmonth_backtest_alignment_enters_once_with_budget_limit(self):
        p = self.portfolio()
        self.store.configure(
            self.pid,
            p["revision"],
            enabled=True,
            paused=False,
            usable_capital=2500000,
            client_id="owner",
            entry_source="backtest:123456abcdef",
        )
        self.app.state.momentum.run = lambda run_id: {
            "settings": {"score": "tuned", **momentum.TUNED},
            "data": {"failed": 0, "through": "2026-11-11"},
            "holdings": [{"symbol": "AAA"}],
        }
        ranking = {"date": "2026-11-11", "failed": 0, "ranking": [INST]}
        with (
            patch.object(execution_brokers, "stock_snapshot", AsyncMock(return_value=self.evidence)),
            patch.object(execution_brokers, "place_stock_limit", AsyncMock(return_value="ack")) as send,
        ):
            await self.svc._portfolio_tick(self.pid, NOW, ranking)
            p = self.portfolio()
            self.assertEqual(len(p["orders"]), 1)
            self.assertLessEqual(p["orders"][0]["reserved"], 2500000)
            self.assertFalse(p["alignment_pending"])
            send.assert_awaited_once()
            self.assertIsNone(p["position"])

    async def test_alignment_waits_for_paper_rotation_and_fresh_backtest(self):
        p = {**self.portfolio(), "entry_source": "paper"}
        self.store.plan("1:paper", {"month": "2026-11", "buy": INST})
        with self.assertRaisesRegex(PortfolioError, "entry is still due"):
            await self.svc._target(p, NOW, {"date": "2026-11-11", "ranking": [INST]})
        self.app.state.momentum.run = lambda _: {
            "settings": {"score": "tuned", **momentum.TUNED},
            "data": {"failed": 0, "through": "2026-10-16"},
            "holdings": [{"symbol": "AAA"}],
        }
        with self.assertRaisesRegex(PortfolioError, "latest completed"):
            await self.svc._target(
                {**p, "entry_source": "backtest:123456abcdef"}, NOW, {"date": "2026-11-11", "ranking": [INST]}
            )

    async def test_intraday_attempt_aggregation_uses_remaining_fills_and_final_charges(self):
        prior = {
            "qty": 10,
            "filled_qty": 4,
            "notional": 44000,
            "execution_status": "cancelled",
            "charges_paise": 200,
        }
        order = {"qty": 6, "execution_status": "unknown", "filled_qty": 0, "attempts": [prior]}
        current = apply_evidence(
            current_attempt(order),
            {
                "order_id": "replacement",
                "qty": 6,
                "filled_qty": 6,
                "notional": 72000,
                "status": "complete",
                "charges_paise": 300,
            },
        )
        combined = combine(order, current)
        self.assertEqual(combined["filled_qty"], 10)
        self.assertEqual(combined["fill_price"], 116)
        self.assertEqual(combined["charges"], 5)
        self.assertEqual(combined["charges_source"], "operator_statement")

    async def test_legacy_import_claim_is_unique_and_keeps_live_disabled(self):
        # Fresh portfolio: legacy submissions must all be reconciled, never inferred from holdings.
        legacy = {
            "orders": [
                {
                    "at": "2026-10-20 12:00:00",
                    "broker": "indmoney",
                    "side": "BUY",
                    "symbol": "AAA",
                    "isin": "INEAAA",
                    "qty": 10,
                    "status": "submitted",
                    "order_id": "old-buy",
                }
            ],
            "positions": {"indmoney": {"qty": 10}},
        }
        self.repo.set_json("momentum_tuned", legacy)
        self.svc.load()
        row = {
            "client_id": "owner",
            "order_id": "old-buy",
            "intent_id": "",
            "symbol": "AAA",
            "isin": "INEAAA",
            "side": "BUY",
            "quantity": 10,
            "filled_quantity": 10,
            "average_price": 100,
            "status": "complete",
            "trade_date": "2026-10-20",
            "total_charges": 5,
        }
        doc = self.svc.reconciliation.preview(1, "indmoney", "legacy-note", self.csv([row]), "owner", NOW)
        self.apply(doc)
        p = next(p for p in self.store.snapshot(1) if p["broker"] == "indmoney")
        self.assertFalse(p["enabled"])
        self.assertTrue(p["paused"])
        self.assertEqual(p["position"]["qty"], 10)
        uid = self.repo.add_user("other", "!")
        self.store.ensure([self.repo.user(uid)], legacy)
        with self.assertRaises(PortfolioError):
            self.svc.reconciliation.preview(uid, "indmoney", "legacy-note", self.csv([row]), "owner", NOW)

    async def test_schema6_backup_migrates_without_modifying_source(self):
        source = Database(self.root / "old.sqlite")
        with patch("upstocks.database.MIGRATIONS", MIGRATIONS[:6]):
            source.initialize()
        backup = source.backup(self.root / "upgrade.sqlite")
        upgraded = Database(backup)
        upgraded.initialize()
        self.assertEqual(upgraded.check(), {"integrity": "ok", "foreign_key_errors": 0, "schema_version": 8})
        self.assertEqual(source.check()["schema_version"], 6)
        upgraded.initialize()
        self.assertEqual(upgraded.check()["schema_version"], 8)

    async def test_intraday_replacement_archives_partial_attempt_and_is_not_duplicated(self):
        tid = "frontend_live|2026-11-12|10:00|NSE_FO|123"
        base = {
            "status": "submitted",
            "client_id": "owner",
            "symbol": "NIFTY",
            "broker_symbol": "NIFTY26NOVFUT",
            "qty": 10,
            "contract": {},
        }
        buy = {
            **base,
            "order_id": "buy1",
            "filled_qty": 10,
            "fill_price": 100,
            "execution_status": "complete",
        }
        sell = {
            **base,
            "order_id": "sell1",
            "filled_qty": 4,
            "fill_price": 110,
            "execution_status": "cancelled",
        }
        for side, order in (("BUY", buy), ("SELL", sell)):
            self.repo.reserve_order(tid, self.pid, "frontend_live", side, order)
            self.repo.finish_order(tid, self.pid, side, order, "fixture")
        self.trading.state = self.repo.state()
        self.trading.live_fills = AsyncMock(return_value={})
        evidence = {**self.evidence, "positions": [{"tradingsymbol": "NIFTY26NOVFUT", "quantity": 6}]}
        body = SimpleNamespace(broker="kite", trade_id=tid, order_id="sell1")
        with (
            patch("upstocks.live.market_open", return_value=True),
            patch.object(execution_brokers, "stock_snapshot", AsyncMock(return_value=evidence)),
            patch(
                "upstocks.brokers.place_order", AsyncMock(return_value=("sell2", 6, "NIFTY26NOVFUT"))
            ) as send,
        ):
            result = await self.trading.recover_exit(1, body)
            self.assertEqual(result["sold"], 1)
            self.assertEqual(send.await_args.kwargs["qty"], 6)
            with self.assertRaises(PortfolioError):
                await self.trading.recover_exit(1, body)
            send.assert_awaited_once()
        saved = self.repo.state()["orders"][self.pid][tid]["sell"]
        self.assertEqual(saved["filled_qty"], 4)
        self.assertEqual(saved["qty"], 6)
        self.assertEqual(saved["total_qty"], 10)
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM broker_order_attempts").fetchone()[0], 1)
        row = {
            "order_id": "sell2",
            "qty": 6,
            "side": "SELL",
            "filled_qty": 6,
            "status": "complete",
            "notional": 72000,
            "average_price": 120,
        }
        self.trading._fills_cache.clear()
        with (
            patch.object(execution_brokers, "order_book", AsyncMock(return_value=[row])),
            patch("upstocks.brokers.order_charges", AsyncMock(return_value={})),
        ):
            await TradingService.live_fills(self.trading, 1)
        updated = self.repo.state()["orders"][self.pid][tid]["sell"]
        self.assertEqual(updated["filled_qty"], 10)
        self.assertEqual(updated["fill_price"], 116)

    async def test_intraday_import_corrects_archived_attempt_charges(self):
        tid = "frontend_live|2026-10-20|10:00|NSE_FO|123"
        prior = {
            "order_id": "sell1",
            "client_id": "owner",
            "symbol": "NIFTY",
            "broker_symbol": "NIFTY26NOVFUT",
            "qty": 10,
            "filled_qty": 4,
            "notional": 44000,
            "execution_status": "cancelled",
            "charges_paise": None,
        }
        order = {
            "status": "submitted",
            "order_id": "sell2",
            "client_id": "owner",
            "symbol": "NIFTY",
            "broker_symbol": "NIFTY26NOVFUT",
            "qty": 6,
            "filled_qty": 10,
            "fill_price": 116,
            "attempt_filled_qty": 6,
            "attempt_notional": 72000,
            "execution_status": "complete",
            "attempts": [prior],
        }
        self.repo.reserve_order(tid, self.pid, "frontend_live", "SELL", order)
        self.repo.finish_order(tid, self.pid, "SELL", order, "fixture")
        base = {
            "client_id": "owner",
            "intent_id": "",
            "symbol": "NIFTY26NOVFUT",
            "isin": "",
            "side": "SELL",
            "trade_date": "2026-10-20",
        }
        rows = [
            {
                **base,
                "order_id": "sell1",
                "quantity": 10,
                "filled_quantity": 4,
                "average_price": 110,
                "status": "cancelled",
                "total_charges": 2,
            },
            {
                **base,
                "order_id": "sell2",
                "quantity": 6,
                "filled_quantity": 6,
                "average_price": 120,
                "status": "complete",
                "total_charges": 3,
            },
        ]
        self.apply(self.preview(rows))
        result = self.repo.state()["orders"][self.pid][tid]["sell"]
        self.assertEqual(result["charges"], 5)
        self.assertEqual(result["filled_qty"], 10)
        self.assertEqual(result["charges_source"], "operator_statement")

    async def test_ledger_replay_orders_mixed_timezone_timestamps_by_instant(self):
        buy=self.fill(self.order())
        sell=self.fill(self.order("SELL"),price=120)
        with self.db.connect(write=True) as c:
            c.execute("UPDATE stock_orders SET created_at='2026-10-31T10:00:00+05:30' WHERE id=?",(buy["id"],))
            c.execute("UPDATE stock_orders SET created_at='2026-10-31T06:00:00+00:00' WHERE id=?",(sell["id"],))
        p=self.portfolio()
        self.assertEqual(p["orders"][0]["id"],buy["id"])
        self.assertIsNone(p["position"])
