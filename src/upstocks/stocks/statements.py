"""Strict, bounded reconciliation input. A statement never creates a new manual trade."""

import csv
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import io
from typing import Any
from .domain import PortfolioError, TERMINAL, paise, quantity

COLUMNS = (
    "client_id",
    "order_id",
    "intent_id",
    "symbol",
    "isin",
    "side",
    "quantity",
    "filled_quantity",
    "average_price",
    "status",
    "trade_date",
    "total_charges",
)


def parse_statement(content: str, today: date) -> list[dict[str, Any]]:
    if len(content.encode("utf-8")) > 262144:
        raise PortfolioError("Statement exceeds 256 KB")
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    if reader.fieldnames != list(COLUMNS):
        raise PortfolioError("Use the reconciliation CSV template; column names and order must match")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line, row in enumerate(reader, 2):
        if line > 501 or None in row or any(value is None for value in row.values()):
            raise PortfolioError("Statement is too large or has malformed rows")
        values = {key: str(value).strip() for key, value in row.items()}
        if not values["client_id"] or not values["order_id"] or not values["symbol"]:
            raise PortfolioError(f"Row {line}: account, broker order ID and symbol are required")
        if any(len(v) > 200 for v in values.values()):
            raise PortfolioError(f"Row {line}: a field is too long")
        if values["order_id"] in seen:
            raise PortfolioError("Duplicate broker order ID in statement")
        seen.add(values["order_id"])
        try:
            trade_day = date.fromisoformat(values["trade_date"])
        except ValueError as exc:
            raise PortfolioError(f"Row {line}: use YYYY-MM-DD for trade date") from exc
        if trade_day > today:
            raise PortfolioError("Statement contains a future trade date")
        side, status = values["side"].upper(), values["status"].lower()
        if side not in ("BUY", "SELL") or status not in TERMINAL:
            raise PortfolioError(f"Row {line}: final status and BUY/SELL are required")
        qty, filled = quantity(values["quantity"]), quantity(values["filled_quantity"])
        if qty < 1 or filled > qty or status == "complete" and filled != qty:
            raise PortfolioError(f"Row {line}: inconsistent quantity")
        try:
            price = Decimal(values["average_price"] or "0")
        except InvalidOperation as exc:
            raise PortfolioError(f"Row {line}: invalid fill price") from exc
        if not price.is_finite() or price < 0:
            raise PortfolioError(f"Row {line}: invalid fill price")
        value = paise(price * filled)
        if filled and value <= 0:
            raise PortfolioError(f"Row {line}: invalid fill price")
        charges = paise(values["total_charges"]) if values["total_charges"] else None
        if charges is not None and (charges < 0 or not filled and charges):
            raise PortfolioError("Charges must be nonnegative")
        out.append(
            {
                **values,
                "side": side,
                "status": status,
                "qty": qty,
                "filled_qty": filled,
                "notional": value,
                "average_price": float(price),
                "charges_paise": charges,
            }
        )
    if not out:
        raise PortfolioError("Statement contains no orders")
    return out


def fingerprint(value: Any) -> str:
    import json

    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
