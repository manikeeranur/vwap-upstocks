"""Broker evidence used by execution, separate from UI account summaries.

See docs/execution-safety.md for API references and fail-closed semantics.
"""

import asyncio
from decimal import Decimal

import httpx

from . import brokers, upstox
from .stocks.domain import PortfolioError, TERMINAL, paise, quantity


async def request(creds, path, *, method="GET", data=None, json=None, base=None, params=None):
    if creds.broker == "indmoney":
        return (
            await brokers._ind(method, path, creds.access_token, json=json)
            if method != "GET" or json is not None
            else await brokers._ind(method, path, creds.access_token)
        )
    headers = (
        brokers._kite_headers(creds.api_key, creds.access_token)
        if creds.broker == "kite"
        else {"Authorization": f"Bearer {creds.access_token}", "Accept": "application/json"}
    )
    host = base or (brokers.KITE_API if creds.broker == "kite" else brokers.UPSTOX_API)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.request(
            method, host + path, headers=headers, data=data, json=json, params=params
        )
    body = await brokers._json(response, brokers.LABELS[creds.broker])
    if response.status_code != 200 or body.get("status") != "success" or body.get("data") is None:
        # Never echo raw broker response bodies, which may contain account details.
        raise brokers.BrokerError(
            f"{brokers.LABELS[creds.broker]} evidence unavailable ({response.status_code})"
        )
    return body["data"]


def normalize_order(broker, row):
    ind = broker == "indmoney"
    qty = quantity(row.get("traded_qty") if ind else row.get("filled_quantity"))
    requested = quantity(row.get("requested_qty") if ind else row.get("quantity"))
    price = row.get("traded_price") if ind else row.get("average_price")
    status = str(row.get("status") or "").lower()
    status = {
        "success": "complete",
        "traded": "complete",
        "canceled": "cancelled",
        "expired": "cancelled",
        "partially filled - cancelled": "cancelled",
        "partially filled - expired": "cancelled",
        "failed": "rejected",
        "aborted": "rejected",
    }.get(status, status)
    # Unknown broker statuses are unresolved, never assumed rejected or filled.
    status = status if status in TERMINAL else "open"
    oid = str((row.get("id") if ind else row.get("order_id")) or "")
    if not oid:
        raise PortfolioError("Broker order identity is unavailable")
    notional = paise(Decimal(str(price)) * qty) if qty else 0
    if qty and notional <= 0 or qty > requested:
        raise PortfolioError("Broker returned inconsistent fill evidence")
    return {
        "order_id": oid,
        "tag": row.get("remarks") if ind else row.get("tag"),
        "status": status,
        "qty": requested,
        "filled_qty": qty,
        "notional": notional,
        "average_price": float(price) if qty else None,
        "side": str(row.get("txn_type") if ind else row.get("transaction_type") or "").upper(),
        "symbol": row.get("name") if ind else row.get("tradingsymbol") or row.get("trading_symbol"),
        "key": row.get("instrument_token") or row.get("instrument_key"),
        "isin": row.get("isin"),
        "product": row.get("product"),
        "exchange": row.get("exchange"),
        "security_id": str(row.get("security_id") or ""),
    }


async def order_book(creds):
    path = {"kite": "/orders", "upstox": "/v2/order/retrieve-all", "indmoney": "/order-book"}[creds.broker]
    rows = await request(creds, path)
    if not isinstance(rows, list):
        raise PortfolioError("Broker order book is unavailable")
    return [normalize_order(creds.broker, row) for row in rows]


async def stock_snapshot(creds):
    paths = {
        "kite": ("/user/profile", "/user/margins/equity", "/portfolio/holdings", "/portfolio/positions"),
        "upstox": (
            "/v2/user/profile",
            "/v3/user/get-funds-and-margin",
            "/v2/portfolio/long-term-holdings",
            "/v2/portfolio/short-term-positions",
        ),
        "indmoney": ("/user/profile", "/funds", "/portfolio/holdings", "/portfolio/positions"),
    }
    profile, funds, holdings, positions, orders = await asyncio.gather(
        *(request(creds, p) for p in paths[creds.broker]), order_book(creds)
    )
    if creds.broker == "kite":
        available = funds["available"]
        cash = min(paise(funds["net"]), paise(available["live_balance"]))
        positions = positions["net"]
    elif creds.broker == "upstox":
        cash = paise(funds["available_to_trade"]["cash_available_to_trade"]["total"])
    else:
        cash = paise(funds["detailed_avl_balance"]["eq_cnc"])
    if not isinstance(holdings, list) or not isinstance(positions, list) or not profile.get("user_id"):
        raise PortfolioError("Incomplete broker portfolio evidence")
    return {
        "broker": creds.broker,
        "client_id": str(profile["user_id"]),
        "cash": max(0, cash),
        "holdings": holdings,
        "positions": positions,
        "orders": orders,
    }


def same_stock(row, instrument):
    return bool(
        row.get("isin") == instrument["isin"]
        or row.get("key") == instrument["key"]
        or row.get("instrument_token") == instrument["key"]
        or row.get("symbol") == instrument["symbol"]
        or row.get("tradingsymbol") == instrument["symbol"]
        or row.get("trading_symbol") == instrument["symbol"]
    )


def ownership(snapshot, instrument, expected, known_ids):
    """Reject shared-symbol/manual activity instead of selling fungible manual holdings."""
    matching_orders = [o for o in snapshot["orders"] if same_stock(o, instrument)]
    for order in matching_orders:
        if order["order_id"] not in known_ids and (order["filled_qty"] or order["status"] not in TERMINAL):
            raise PortfolioError("Manual or other-strategy activity exists for this stock")
    total = 0
    owned_sells = sum(
        o["filled_qty"] for o in matching_orders if o["order_id"] in known_ids and o.get("side") == "SELL"
    )
    for holding in snapshot["holdings"]:
        if not same_stock(holding, instrument):
            continue
        if (
            holding.get("discrepancy")
            or holding.get("collateral_quantity")
            or holding.get("short_quantity")
            or holding.get("product") == "MTF"
        ):
            raise PortfolioError("Pledged, financed or discrepant holding requires review")
        field = {"kite": "opening_quantity", "upstox": "quantity", "indmoney": "total_qty"}[
            snapshot["broker"]
        ]
        total += quantity(holding.get(field))
        used = (
            holding.get("used_qty") if snapshot["broker"] == "indmoney" else holding.get("cnc_used_quantity")
        )
        if used is not None and quantity(used) > owned_sells:
            raise PortfolioError("Holding contains shares reserved outside this strategy; review ownership")
    # Holdings are opening/demat inventory; today's delivery trades are in the order book.
    # Do not add T1 again: Upstox quantity / IND total_qty already include it.
    for order in matching_orders:
        if order["product"] in ("CNC", "D"):
            total += order["filled_qty"] * (1 if order["side"] == "BUY" else -1)
        elif order["filled_qty"] or order["status"] not in TERMINAL:
            raise PortfolioError("Non-delivery activity exists for this stock")
    for position in snapshot["positions"]:
        if same_stock(position, instrument) and position.get("product") not in ("CNC", "D"):
            if any(
                position.get(k)
                for k in (
                    "quantity",
                    "net_qty",
                    "buy_qty",
                    "sell_qty",
                    "day_buy_quantity",
                    "day_sell_quantity",
                )
            ):
                raise PortfolioError("Manual non-delivery position exists for this stock")
    if total != expected:
        raise PortfolioError(
            f"Ownership mismatch: strategy {expected}, broker {total}. Reconcile before trading."
        )
    return total


def match_order(intent, rows):
    matches = [
        r
        for r in rows
        if (intent.get("broker_id") and r["order_id"] == intent["broker_id"])
        or (r.get("tag") and r["tag"] == intent["id"])
    ]
    if len(matches) != 1:
        raise PortfolioError("Order outcome is unresolved; verify its unique reference with the broker")
    row = matches[0]
    if row["qty"] != intent["qty"] or row["side"] != intent["side"]:
        raise PortfolioError("Broker order does not match the reserved intent")
    instrument = {"isin": intent["isin"], "symbol": intent["symbol"], "key": intent["instrument_key"]}
    if not same_stock(row, instrument) or row["product"] not in ("CNC", "D"):
        raise PortfolioError("Broker order instrument or product mismatch")
    return row


async def place_stock_limit(creds, order):
    """A bounded LIMIT order; acknowledgements never alter portfolio balances."""
    price = order["limit_paise"] / 100
    if creds.broker == "kite":
        if order["symbol"] not in await brokers.kite_equity_symbols(creds):
            raise PortfolioError("NSE stock symbol is unavailable")
        result = await request(
            creds,
            "/orders/regular",
            method="POST",
            data={
                "tradingsymbol": order["symbol"],
                "exchange": "NSE",
                "transaction_type": order["side"],
                "order_type": "LIMIT",
                "quantity": order["qty"],
                "price": price,
                "product": "CNC",
                "validity": "DAY",
                "tag": order["id"],
            },
        )
    elif creds.broker == "upstox":
        result = await request(
            creds,
            "/v2/order/place",
            base=upstox.HFT,
            method="POST",
            json={
                "quantity": order["qty"],
                "product": "D",
                "validity": "DAY",
                "price": price,
                "tag": order["id"],
                "instrument_token": order["instrument_key"],
                "order_type": "LIMIT",
                "transaction_type": order["side"],
                "disclosed_quantity": 0,
                "trigger_price": 0,
                "is_amo": False,
            },
        )
    else:
        result = await request(
            creds,
            "/order",
            method="POST",
            json={
                "txn_type": order["side"],
                "exchange": "NSE",
                "segment": "EQUITY",
                "product": "CNC",
                "order_type": "LIMIT",
                "limit_price": price,
                "validity": "DAY",
                "security_id": await brokers.ind_equity_id(creds, order["isin"]),
                "qty": order["qty"],
                "is_amo": False,
                "algo_id": "99999",
                "remarks": order["id"],
            },
        )
    oid = (result or {}).get("order_id")
    if not oid:
        raise PortfolioError("Broker did not acknowledge an order ID")
    return str(oid)


async def order_detail(creds, order_id, *, equity=True):
    """Use an exact broker ID. No symbol/date guessing when old books disappear."""
    if creds.broker == "kite":
        from urllib.parse import quote

        rows = await request(creds, "/orders/" + quote(str(order_id), safe=""))
        if not isinstance(rows, list) or not rows:
            raise PortfolioError("Broker order history is unavailable; import its statement")
        row = rows[-1]
    elif creds.broker == "upstox":
        row = await request(creds, "/v2/order/details", params={"order_id": order_id})
    else:
        row = await request(
            creds, "/order", json={"order_id": order_id, "segment": "EQUITY" if equity else "DERIVATIVE"}
        )
    result = normalize_order(creds.broker, row)
    if result["order_id"] != str(order_id):
        raise PortfolioError("Broker detail returned a different order")
    return result
