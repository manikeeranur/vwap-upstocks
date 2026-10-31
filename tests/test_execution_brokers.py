import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from upstocks import execution_brokers as e
from upstocks.stocks.domain import PortfolioError


class BrokerEvidenceTests(unittest.TestCase):
    def test_normalizes_partial_and_terminal_order_states(self):
        row = {
            "order_id": "k1",
            "quantity": 10,
            "filled_quantity": 3,
            "average_price": 100,
            "status": "OPEN",
            "transaction_type": "BUY",
        }
        result = e.normalize_order("kite", row)
        self.assertEqual((result["filled_qty"], result["notional"], result["status"]), (3, 30000, "open"))
        result = e.normalize_order(
            "indmoney",
            {
                "id": "i1",
                "requested_qty": 10,
                "traded_qty": 10,
                "traded_price": "100.25",
                "status": "SUCCESS",
                "txn_type": "BUY",
            },
        )
        self.assertEqual(
            (result["order_id"], result["notional"], result["status"]), ("i1", 100250, "complete")
        )

    def test_unknown_status_is_never_assumed_complete(self):
        result = e.normalize_order(
            "upstox",
            {"order_id": "u", "quantity": 5, "filled_quantity": 0, "average_price": 0, "status": "NEW_STATE"},
        )
        self.assertEqual(result["status"], "open")

    def test_missing_quantity_cannot_be_inferred_from_ordered_quantity(self):
        with self.assertRaises(PortfolioError):
            e.normalize_order(
                "kite", {"order_id": "k", "quantity": 10, "average_price": 100, "status": "COMPLETE"}
            )

    def test_same_symbol_manual_holding_and_external_orders_are_rejected(self):
        inst = {"symbol": "AAA", "isin": "INEAAA", "key": "NSE_EQ|INEAAA"}
        base = {
            "broker": "kite",
            "orders": [],
            "holdings": [{"symbol": "AAA", "opening_quantity": 20}],
            "positions": [],
        }
        with self.assertRaisesRegex(PortfolioError, "Ownership"):
            e.ownership(base, inst, 10, set())
        with self.assertRaisesRegex(PortfolioError, "Manual"):
            e.ownership(
                {
                    **base,
                    "orders": [
                        {"order_id": "manual", "symbol": "AAA", "filled_qty": 1, "status": "complete"}
                    ],
                },
                inst,
                21,
                set(),
            )

    def test_upstox_total_holding_does_not_double_count_t1(self):
        inst = {"symbol": "AAA", "isin": "INEAAA", "key": "NSE_EQ|INEAAA"}
        snap = {
            "broker": "upstox",
            "orders": [],
            "holdings": [{"isin": "INEAAA", "quantity": 10, "t1_quantity": 10}],
            "positions": [],
        }
        self.assertEqual(e.ownership(snap, inst, 10, set()), 10)

    def test_unique_tag_and_instrument_are_required(self):
        intent = {
            "id": "mt1",
            "broker_id": None,
            "qty": 10,
            "side": "BUY",
            "isin": "INEAAA",
            "symbol": "AAA",
            "instrument_key": "NSE_EQ|INEAAA",
        }
        row = {
            "order_id": "o",
            "tag": "mt1",
            "qty": 10,
            "side": "BUY",
            "isin": "INEBBB",
            "symbol": "BBB",
            "product": "CNC",
        }
        with self.assertRaisesRegex(PortfolioError, "instrument"):
            e.match_order(intent, [row])
        with self.assertRaisesRegex(PortfolioError, "unresolved"):
            e.match_order(intent, [])

    def test_missing_broker_identity_is_not_a_matchable_order(self):
        with self.assertRaisesRegex(PortfolioError, "identity"):
            e.normalize_order("indmoney", {"requested_qty": 10, "traded_qty": 0, "status": "OPEN"})

    def test_reserved_manual_shares_cannot_be_sold(self):
        inst = {"symbol": "AAA", "isin": "INEAAA", "key": "NSE_EQ|INEAAA"}
        for broker, holding in [
            ("indmoney", {"total_qty": 10, "used_qty": 1}),
            ("upstox", {"quantity": 10, "cnc_used_quantity": 1}),
        ]:
            snap = {
                "broker": broker,
                "orders": [],
                "positions": [],
                "holdings": [{"isin": "INEAAA", **holding}],
            }
            with self.subTest(broker=broker), self.assertRaisesRegex(PortfolioError, "reserved"):
                e.ownership(snap, inst, 10, set())


class BrokerLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_stock_connectors_send_limit_price_and_unique_tag(self):
        order = {
            "id": "mt123",
            "symbol": "AAA",
            "isin": "INEAAA",
            "instrument_key": "NSE_EQ|INEAAA",
            "side": "BUY",
            "qty": 10,
            "limit_paise": 10050,
        }
        for broker in ("kite", "upstox", "indmoney"):
            with (
                self.subTest(broker=broker),
                patch.object(e, "request", AsyncMock(return_value={"order_id": "ack"})) as request,
                patch.object(e.brokers, "kite_equity_symbols", AsyncMock(return_value={"AAA"})),
                patch.object(e.brokers, "ind_equity_id", AsyncMock(return_value="1")),
            ):
                self.assertEqual(await e.place_stock_limit(SimpleNamespace(broker=broker), order), "ack")
                payload = request.await_args.kwargs.get("data") or request.await_args.kwargs["json"]
                self.assertEqual(payload["order_type"], "LIMIT")
                self.assertEqual(payload.get("price", payload.get("limit_price")), 100.5)
                self.assertEqual(payload.get("tag", payload.get("remarks")), "mt123")

    async def test_exact_id_history_adapters_do_not_guess_missing_orders(self):
        row = {
            "order_id": "id-1",
            "quantity": 10,
            "filled_quantity": 4,
            "average_price": 100,
            "status": "CANCELLED",
            "transaction_type": "SELL",
            "tradingsymbol": "AAA",
            "product": "CNC",
        }
        with patch.object(e, "request", AsyncMock(return_value=[row])) as request:
            result = await e.order_detail(SimpleNamespace(broker="kite"), "id-1")
            self.assertEqual(result["filled_qty"], 4)
            self.assertEqual(request.await_args.args[1], "/orders/id-1")
        ind = {
            "id": "id-1",
            "requested_qty": 10,
            "traded_qty": 4,
            "traded_price": 100,
            "status": "PARTIALLY FILLED - CANCELLED",
            "txn_type": "SELL",
            "name": "AAA",
        }
        with patch.object(e, "request", AsyncMock(return_value=ind)) as request:
            result = await e.order_detail(SimpleNamespace(broker="indmoney"), "id-1", equity=False)
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(request.await_args.kwargs["json"], {"order_id": "id-1", "segment": "DERIVATIVE"})
        with patch.object(e, "request", AsyncMock(return_value={**row, "order_id": "wrong"})):
            with self.assertRaises(PortfolioError):
                await e.order_detail(SimpleNamespace(broker="upstox"), "id-1")
