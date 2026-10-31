"""Cumulative intraday fills across explicitly approved exit attempts."""

from copy import deepcopy
from typing import Any
from .stocks.domain import PortfolioError, TERMINAL, paise

Record = dict[str, Any]


def current_attempt(order: Record) -> Record:
    return {
        **{
            k: order[k]
            for k in (
                "order_id",
                "client_tag",
                "client_id",
                "qty",
                "symbol",
                "broker_symbol",
                "contract",
                "execution_status",
                "created_at",
            )
            if k in order
        },
        "filled_qty": order.get("attempt_filled_qty", order.get("filled_qty", 0)),
        "notional": order.get(
            "attempt_notional", paise((order.get("fill_price") or 0) * order.get("filled_qty", 0))
        ),
        "charges_paise": order.get("attempt_charges_paise"),
    }


def combine(order: Record, current: Record, previous: list[Record] | None = None) -> Record:
    previous = deepcopy(order.get("attempts", [])) if previous is None else previous
    all_attempts = [*previous, current]
    qty = sum(a["filled_qty"] for a in all_attempts)
    value = sum(a["notional"] for a in all_attempts)
    out = {
        **order,
        "attempts": previous,
        "attempt_filled_qty": current["filled_qty"],
        "attempt_notional": current["notional"],
        "attempt_charges_paise": current.get("charges_paise"),
        "execution_status": current["execution_status"],
        "filled_qty": qty,
    }
    if qty:
        out["fill_price"] = value / qty / 100
    else:
        out.pop("fill_price", None)
    charged = [a for a in all_attempts if a["filled_qty"]]
    out.pop("charges", None)
    out.pop("charges_source", None)
    if charged and all(a.get("charges_paise") is not None for a in charged):
        out["charges"] = sum(a["charges_paise"] for a in charged) / 100
        out["charges_source"] = "operator_statement"
    return out


def apply_evidence(attempt: Record, row: Record) -> Record:
    if row["qty"] != attempt["qty"] or row["filled_qty"] < attempt.get("filled_qty", 0):
        raise PortfolioError("Fill quantity contradicts the recorded order")
    if row["status"] == "complete" and row["filled_qty"] != row["qty"]:
        raise PortfolioError("Completed order has inconsistent quantity")
    if attempt.get("execution_status") in TERMINAL and attempt["execution_status"] != row["status"]:
        raise PortfolioError("Terminal order status changed; review required")
    return {
        **attempt,
        "order_id": row["order_id"],
        "filled_qty": row["filled_qty"],
        "notional": row["notional"],
        "execution_status": row["status"],
        "charges_paise": row.get("charges_paise", attempt.get("charges_paise")),
    }
