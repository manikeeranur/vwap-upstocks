"""Deterministic stock accounting from cumulative execution evidence."""

from typing import Any
from .domain import PortfolioError

Record = dict[str, Any]


def balances(capital: int, orders: list[Record]) -> Record:
    cash, realized = capital, 0
    position: Record | None = None
    missing_charges = 0
    for order in orders:
        qty = int(order["filled_qty"])
        if not qty:
            continue
        value = int(order["notional"])
        charge = order.get("charges_paise")
        if charge is None:
            missing_charges += 1
            charge = int(order["fee_provision"])
        if order["side"] == "BUY":
            if position and position["isin"] != order["isin"]:
                raise PortfolioError("Execution evidence would mix two strategy holdings")
            cash -= value + charge
            if position:
                position["qty"] += qty
                position["cost"] += value + charge
            else:
                position = {
                    "symbol": order["symbol"],
                    "isin": order["isin"],
                    "instrument_key": order["instrument_key"],
                    "qty": qty,
                    "cost": value + charge,
                    "opened_at": order["created_at"],
                }
        else:
            if not position or position["isin"] != order["isin"] or qty > position["qty"]:
                raise PortfolioError("Execution evidence sells more than the strategy owns")
            basis = position["cost"] * qty // position["qty"]
            cash += value - charge
            realized += value - charge - basis
            position["qty"] -= qty
            position["cost"] -= basis
            if not position["qty"]:
                position = None
    return {
        "cash": max(0, cash),
        "cash_shortfall": max(0, -cash),
        "realized": realized,
        "position": position,
        "missing_charges": missing_charges,
        "net_realized": realized if not missing_charges else None,
    }
