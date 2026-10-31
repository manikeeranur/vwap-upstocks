"""Pure execution rules. All money is integer paise."""

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any

TERMINAL = frozenset({"complete", "cancelled", "rejected"})


class PortfolioError(ValueError):
    """A command cannot safely proceed."""


class StaleRevision(PortfolioError):
    pass


def paise(value: object) -> int:
    try:
        n = Decimal(str(value))
    except InvalidOperation as exc:
        raise PortfolioError("A finite monetary value is required") from exc
    if not n.is_finite() or abs(n) > Decimal("90000000000000"):
        raise PortfolioError("A finite monetary value is required")
    return int((n * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def quantity(value: object) -> int:
    if isinstance(value, bool) or value is None:
        raise PortfolioError("Broker quantity is unavailable")
    try:
        n = Decimal(str(value))
    except InvalidOperation as exc:
        raise PortfolioError("Invalid broker quantity") from exc
    if not n.is_finite() or n < 0 or n != int(n):
        raise PortfolioError("Invalid broker quantity")
    return int(n)


def fee_provision(notional: int) -> int:
    """Conservative allocation, not the broker's final charges."""
    return (notional + 99) // 100 + 5000 if notional else 0


def buy_quantity(budget: int, limit: int) -> int:
    if limit <= 0:
        raise PortfolioError("A positive limit price is required")
    qty = max(0, (budget - 5000) * 100 // (limit * 101))
    while qty and qty * limit + fee_provision(qty * limit) > budget:
        qty -= 1
    return qty


def decide(
    ranking: list[dict[str, Any]], held: str | None, exit_rank: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """Missing held-stock data must never cause an exit."""
    by_symbol = {r["symbol"]: r for r in ranking}
    sell, reason = None, ""
    if held:
        row = by_symbol.get(held)
        if row is None:
            raise PortfolioError(f"Ranking for {held} is missing; keep the holding and refresh data")
        if row["rank"] <= exit_rank and row["above_sma"]:
            return None, None, f"Keep {held}: rank {row['rank']}, above its average"
        sell = row
        reason = (
            f"Rank {row['rank']} worse than {exit_rank}"
            if row["rank"] > exit_rank
            else "Closed below its average"
        )
    buy = next((r for r in ranking if r["symbol"] != held and r["above_sma"]), None)
    return sell, buy, reason or (f"Buy {buy['symbol']}" if buy else "Stay in cash")
