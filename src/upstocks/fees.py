"""Broker-calculated fee estimates, tied to an exact cumulative fill.

Calculator responses are estimates, never statement evidence. Missing fees stay
unknown. Amounts are stored in paise and include their inputs and timestamp.
"""

from decimal import Decimal
from typing import Any

from . import execution_brokers
from .database import utc_now
from .stocks.domain import PortfolioError, paise, quantity

Record = dict[str, Any]


def valid_estimate(attempt: Record) -> bool:
    fee = attempt.get("fee_estimate") or {}
    return bool(
        attempt.get("filled_qty", 0) > 0
        and fee.get("order_id") == attempt.get("order_id")
        and fee.get("client_id") == attempt.get("client_id")
        and fee.get("filled_qty") == attempt.get("filled_qty")
        and fee.get("notional") == attempt.get("notional")
        and type(fee.get("total_paise")) is int
        and fee["total_paise"] >= 0
    )


def order_fee(order: Record) -> Record:
    """Public reporting projection; do not infer zero for missing charges."""
    if order.get("charges_source") == "operator_statement" and order.get("charges") is not None:
        return {"amount": order["charges"], "quality": "final"}
    if (
        order.get("estimated_charges") is not None
        and order.get("estimated_charges_qty") == order.get("filled_qty")
        and order.get("estimated_charges_notional")
        == paise(Decimal(str(order.get("fill_price") or 0)) * order.get("filled_qty", 0))
    ):
        return {"amount": order["estimated_charges"], "quality": "estimated"}
    return {"amount": None, "quality": "missing"}


def stock_attempt(order: Record) -> Record:
    return {**order, "order_id": order.get("broker_id")}


async def calculate(creds: Any, attempt: Record, context: Record) -> Record:
    """Call only fee/margin calculators; never place or change a broker order."""
    qty = quantity(attempt.get("filled_qty"))
    value = attempt.get("notional")
    if not qty or type(value) is not int or value <= 0 or not attempt.get("order_id"):
        raise PortfolioError("Confirmed quantity and fill value are required for fees")
    if not creds.client_id or attempt.get("client_id") != creds.client_id:
        raise PortfolioError("Fee account does not match the saved order")
    price = float(Decimal(value) / qty / 100)
    side = context.get("side")
    if side not in ("BUY", "SELL"):
        raise PortfolioError("Order side is required for fees")
    broker = creds.broker
    if broker == "kite":
        if not all(context.get(k) for k in ("symbol", "exchange", "product", "order_type", "variety")):
            raise PortfolioError("Broker contract details are required for fees")
        params = {
            "order_id": str(attempt["order_id"]),
            "exchange": context["exchange"],
            "tradingsymbol": context["symbol"],
            "transaction_type": side,
            "variety": context["variety"],
            "product": context["product"],
            "order_type": context["order_type"],
            "quantity": qty,
            "average_price": price,
        }
        response = await execution_brokers.request(creds, "/charges/orders", method="POST", json=[params])
        if not isinstance(response, list) or len(response) != 1:
            raise PortfolioError("Incomplete broker fee response")
        row = response[0]
        for key in (
            "exchange",
            "tradingsymbol",
            "transaction_type",
            "variety",
            "product",
            "order_type",
            "quantity",
        ):
            if row.get(key) != params[key]:
                raise PortfolioError("Broker fee response does not match the order")
        if paise(Decimal(str(row.get("price"))) * qty) != value:
            raise PortfolioError("Broker fee response has a different fill value")
        breakdown = row.get("charges")
        source = "kite_virtual_contract_note"
    elif broker == "upstox":
        if not context.get("key") or not context.get("product"):
            raise PortfolioError("Broker instrument and product are required for fees")
        params = {
            "instrument_token": context["key"],
            "quantity": qty,
            "product": context["product"],
            "transaction_type": side,
            "price": price,
        }
        response = await execution_brokers.request(creds, "/v2/charges/brokerage", params=params)
        breakdown = response.get("charges")
        source = "upstox_brokerage"
    elif broker == "indmoney":
        if not all(context.get(k) for k in ("security_id", "exchange", "segment", "product")):
            raise PortfolioError("Broker security and product are required for fees")
        params = {
            "securityID": context["security_id"],
            "exchange": context["exchange"],
            "segment": context["segment"],
            "product": context["product"],
            "txnType": side,
            "quantity": str(qty),
            "price": str(price),
        }
        response = await execution_brokers.request(creds, "/margin", json=params)
        breakdown = response.get("charges")
        source = "indmoney_margin_calculator"
    else:
        raise PortfolioError("Broker fee calculator is unavailable")
    if not isinstance(breakdown, dict):
        raise PortfolioError("Broker charges are unavailable")
    total = breakdown.get("total_charges" if broker == "indmoney" else "total")
    if total is None or isinstance(total, bool) or paise(total) < 0:
        raise PortfolioError("Broker returned invalid charges")
    # Delivery-sale calculators can omit daily DP/other back-office charges.
    # Even a complete calculator breakdown remains explicitly estimated.
    return {
        "order_id": attempt["order_id"],
        "client_id": creds.client_id,
        "filled_qty": qty,
        "notional": value,
        "total_paise": paise(total),
        "source": source,
        "calculated_at": utc_now(),
        "trade_date": context.get("trade_date"),
        "context": context,
        "request": params,
        "components": breakdown,
    }
