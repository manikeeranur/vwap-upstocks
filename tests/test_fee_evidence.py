import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from upstocks import fees
from upstocks.database import Database
from upstocks.order_evidence import current_attempt, combine, apply_evidence
from upstocks.repository import Repository
from upstocks.services.trading import TradingService
from upstocks.stocks.domain import PortfolioError
from upstocks.stocks.store import StockStore

CONTEXT = {
    "symbol": "NIFTYTEST",
    "exchange": "NFO",
    "product": "MIS",
    "order_type": "MARKET",
    "variety": "regular",
    "side": "BUY",
    "trade_date": "2026-10-30",
}
ATTEMPT = {
    "order_id": "buy1",
    "client_id": "owner",
    "qty": 10,
    "filled_qty": 10,
    "notional": 100000,
    "execution_status": "complete",
    "fee_context": CONTEXT,
}


def estimate(attempt, total=1000):
    return {
        "order_id": attempt["order_id"],
        "client_id": attempt["client_id"],
        "filled_qty": attempt["filled_qty"],
        "notional": attempt["notional"],
        "total_paise": total,
        "source": "kite_virtual_contract_note",
        "calculated_at": "2026-10-31T00:00:00+00:00",
    }


class FeeEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "test.sqlite3")
        self.db.initialize()
        self.repo = Repository(self.db)
        self.repo.migrate_legacy(Path(self.temp.name))
        self.repo.ensure_presets()
        self.creds = SimpleNamespace(broker="kite", client_id="owner")
        self.order = {
            "status": "submitted",
            "order_id": "buy1",
            "client_id": "owner",
            "qty": 10,
            "filled_qty": 10,
            "fill_price": 100,
            "execution_status": "complete",
            "fee_context": CONTEXT,
        }
        self.tid = "harmonic|2026-10-30|11:01|NSE_FO|123"

    async def test_kite_calculator_validates_response_identity_and_preserves_breakdown(self):
        response = {
            **{k: CONTEXT[k] for k in ("exchange", "product", "order_type", "variety")},
            "tradingsymbol": "NIFTYTEST",
            "transaction_type": "BUY",
            "quantity": 10,
            "price": 100,
            "charges": {"total": 12.345, "brokerage": 10},
        }
        with patch("upstocks.execution_brokers.request", AsyncMock(return_value=[response])) as call:
            result = await fees.calculate(self.creds, ATTEMPT, CONTEXT)
        self.assertEqual(result["total_paise"], 1235)
        self.assertEqual(result["components"], response["charges"])
        self.assertEqual(call.call_args.args[1], "/charges/orders")
        self.assertEqual(call.call_args.kwargs["method"], "POST")
        self.assertEqual(call.call_args.kwargs["json"][0]["quantity"], 10)
        for bad in (
            [],
            [{**response, "quantity": 11}],
            [{**response, "price": 101}],
            [{**response, "charges": {}}],
            [{**response, "charges": {"total": -1}}],
            [{**response, "charges": {"total": float("nan")}}],
        ):
            with patch("upstocks.execution_brokers.request", AsyncMock(return_value=bad)):
                with self.assertRaises(PortfolioError):
                    await fees.calculate(self.creds, ATTEMPT, CONTEXT)

    async def test_wrong_account_or_missing_fill_never_calls_broker(self):
        with patch("upstocks.execution_brokers.request", AsyncMock()) as call:
            for change in ({"client_id": "other"}, {"filled_qty": 0}, {"notional": 0}):
                with self.assertRaises(PortfolioError):
                    await fees.calculate(self.creds, {**ATTEMPT, **change}, CONTEXT)
            call.assert_not_awaited()

    async def test_upstox_and_indmoney_calculators_use_actual_cumulative_fills(self):
        with patch(
            "upstocks.execution_brokers.request", AsyncMock(return_value={"charges": {"total": 15}})
        ) as call:
            result = await fees.calculate(
                SimpleNamespace(broker="upstox", client_id="owner"),
                ATTEMPT,
                {**CONTEXT, "product": "I", "key": "NSE_FO|123"},
            )
            self.assertEqual(result["total_paise"], 1500)
            self.assertEqual(call.call_args.kwargs["params"]["price"], 100)
        with patch(
            "upstocks.execution_brokers.request", AsyncMock(return_value={"charges": {"total_charges": 11.8}})
        ) as call:
            result = await fees.calculate(
                SimpleNamespace(broker="indmoney", client_id="owner"),
                ATTEMPT,
                {**CONTEXT, "product": "INTRADAY", "security_id": "123", "segment": "DERIVATIVE"},
            )
            self.assertEqual(result["total_paise"], 1180)
            self.assertEqual(call.call_args.args[1], "/margin")
            self.assertEqual(call.call_args.kwargs["json"]["quantity"], "10")
            self.assertEqual(call.call_args.kwargs["json"]["price"], "100.0")

    async def test_fees_persist_restart_skip_duplicates_and_refresh_changed_fills(self):
        partial = {**self.order, "filled_qty": 4, "execution_status": "open"}
        self.repo.reserve_order(self.tid, "1:kite", "harmonic", "BUY", partial, {"trade": {}})
        service = TradingService(self.repo)

        async def calculate(creds, attempt, context):
            return estimate(attempt)

        with patch("upstocks.fees.calculate", AsyncMock(side_effect=calculate)) as call:
            await service._store_charges("1:kite", self.creds)
            restarted = TradingService(self.repo)
            await restarted._store_charges("1:kite", self.creds)
            self.assertEqual(call.await_count, 1)
            saved = restarted.state["orders"]["1:kite"][self.tid]["buy"]
            self.assertEqual(fees.order_fee(saved), {"amount": 10, "quality": "estimated"})
            update = {
                "order_id": "buy1",
                "qty": 10,
                "filled_qty": 10,
                "notional": 100000,
                "status": "complete",
            }
            changed = combine(saved, apply_evidence(current_attempt(saved), update))
            self.assertIsNone(fees.order_fee(changed)["amount"])
            self.repo.finish_order(self.tid, "1:kite", "BUY", changed, "new fill")
            restarted = TradingService(self.repo)
            await restarted._store_charges("1:kite", self.creds)
            self.assertEqual(call.await_count, 2)
        row = self.repo.journal("2026-10-30", "2026-10-30", 1)["live"][self.tid]["1:kite"]
        self.assertEqual(row["buy_charges"], 10)
        self.assertEqual(row["buy_fee_quality"], "estimated")

    def test_replacement_attempts_are_counted_once_and_statement_supersedes_estimate(self):
        first = {
            **ATTEMPT,
            "order_id": "sell1",
            "filled_qty": 4,
            "notional": 44000,
            "execution_status": "cancelled",
        }
        first["fee_estimate"] = estimate(first, 500)
        second = {**ATTEMPT, "order_id": "sell2", "qty": 6, "filled_qty": 6, "notional": 72000}
        order = {**self.order, "order_id": "sell2", "qty": 6}
        combined = combine(order, second, [first])
        self.assertIsNone(fees.order_fee(combined)["amount"])
        second["fee_estimate"] = estimate(second, 700)
        combined = combine(order, second, [first])
        self.assertEqual(fees.order_fee(combined), {"amount": 12, "quality": "estimated"})
        self.assertEqual(combine(combined, current_attempt(combined)), combined)
        first["charges_paise"], second["charges_paise"] = 600, 800
        final = combine(order, second, [first])
        self.assertEqual(fees.order_fee(final), {"amount": 14, "quality": "final"})

    async def test_failed_calculation_retries_with_backoff_and_never_invents_zero(self):
        self.repo.reserve_order(self.tid, "1:kite", "harmonic", "BUY", self.order, {"trade": {}})
        service = TradingService(self.repo)
        with patch("upstocks.fees.calculate", AsyncMock(side_effect=PortfolioError("Unavailable"))) as call:
            await service._store_charges("1:kite", self.creds)
            await service._store_charges("1:kite", self.creds)
            self.assertEqual(call.await_count, 1)
        self.assertIsNone(fees.order_fee(service.state["orders"]["1:kite"][self.tid]["buy"])["amount"])
        self.assertIn("1:kite", service._fee_errors)

    def test_stock_estimates_persist_without_releasing_cash_or_budget(self):
        store = StockStore(self.db)
        store.ensure(self.repo.users())
        store.configure("1:kite", 0, enabled=True, paused=False, usable_capital=5000000, client_id="owner")
        instrument = {"symbol": "AAA", "isin": "INEAAA", "key": "NSE_EQ|INEAAA"}
        for side, price in (("BUY", 10000), ("SELL", 11000)):
            order = store.reserve("1:kite", "test", side, instrument, 10, 12000, "owner")
            store.apply(
                order["id"],
                {"order_id": side, "filled_qty": 10, "notional": price * 10, "status": "complete"},
            )
        before = next(p for p in store.snapshot(1) if p["id"] == "1:kite")
        for order in before["orders"]:
            store.save_fee_estimate(order["id"], estimate(fees.stock_attempt(order), 1000))
        after = next(p for p in StockStore(self.db).snapshot(1) if p["id"] == "1:kite")
        self.assertEqual(after["estimated_net_realized"], 8000)
        self.assertIsNone(after["net_realized"])
        self.assertEqual(after["cash"], before["cash"])
        self.assertEqual(after["usable_capital"], before["usable_capital"])
        invalid = deepcopy(after["orders"][0]["fee_estimate"])
        invalid["filled_qty"] = 9
        with self.assertRaises(PortfolioError):
            store.save_fee_estimate(after["orders"][0]["id"], invalid)
