"""Monthly momentum rotation over NSE equities — pure calculation, no I/O.

Rules (one decision per month):
- Universe each day: the `universe` stocks with the highest 6-month (126-day)
  median turnover (close × volume), among stocks with 1 year of history.
- Month-end, after the close, score each stock (rank 1 is best) by either
  risk-adjusted: mean of the cross-sectional z-scores of 6-month and 12-month
  return ÷ annualised 1-year volatility, or
  12-1: the raw return from 252 to 21 trading days ago (skips the latest month).
- A holding is sold if its rank is worse than `exit_rank` (or it left the
  universe) or it closed below its `sma_days` average; the rest are kept.
- Free slots are filled from the top of the ranking, skipping stocks already
  held and stocks below their average.
- Next trading day at the open: sells first, then buys of cash ÷ free slots
  each, whole shares only. `cost_pct` is charged on every buy and sell.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
import math

import numpy as np
import pandas as pd

R6, R12, VOL_DAYS, TURNOVER_DAYS, HISTORY_DAYS = 126, 252, 252, 126, 252
SKIP = 21  # 12-1 momentum leaves out the latest month
SCORES = ("risk_adjusted", "12-1", "tuned")
# "tuned" ranks like 12-1; the page fills these rule values for it (chosen 2026-10-18:
# ₹50,000 from Nov 2019 → ₹1.75 crore in the backtest, worst drop −74%).
TUNED = {"slots": 1, "exit_rank": 5, "sma_days": 150, "universe": 1000}
# Fills use daily candles: the open price is set at 09:15:00 IST, the close at 15:30:00 (or `fills`, below).
OPEN_TIME, CLOSE_TIME = "09:15:00", "15:30:00"
# Momentum Tuned trades the switch day at these times (sell first, then buy with the proceeds); a backtest
# given their minute prices (`fills`) uses them instead of the open.
SELL_TIME, BUY_TIME = "11:00:00", "12:00:00"


@dataclass(frozen=True)
class Settings:
    start: date
    end: date
    capital: float = 1_000_000
    slots: int = 1
    universe: int = 500
    exit_rank: int = 20
    sma_days: int = 100
    score: str = "risk_adjusted"  # or "12-1" / "tuned": close 21 days ago ÷ close 252 days ago − 1
    cost_pct: float = 0.1


def history_start(start: date) -> date:
    """First day of data needed so the first month-end can be scored."""
    return start - timedelta(days=400)


@dataclass
class Features:
    """Per decision day (rows) × stock (columns)."""
    turnover: np.ndarray
    r6: np.ndarray
    r12: np.ndarray
    r12_1: np.ndarray
    vol: np.ndarray
    sma: np.ndarray
    close: np.ndarray
    eligible: np.ndarray


def last_weekday_of_month(d: date) -> bool:
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.month != d.month


def month_ends(dates: list[date]) -> list[int]:
    return [i for i in range(len(dates)) if i + 1 == len(dates) or dates[i + 1].month != dates[i].month]


def features(close: np.ndarray, volume: np.ndarray, rows: list[int], sma_days: int) -> Features:
    """Rolling statistics per stock, sampled at `rows` (decision days)."""
    n = close.shape[1]
    out = {k: np.full((len(rows), n), np.nan) for k in ("turnover", "r6", "r12", "r12_1", "vol", "sma", "close")}
    eligible = np.zeros((len(rows), n), dtype=bool)
    for j in range(n):
        raw = pd.Series(close[:, j], dtype="float64")
        traded = raw.notna()
        if not traded.any():
            continue
        c = raw.ffill()
        turnover = (raw * pd.Series(volume[:, j], dtype="float64")).rolling(TURNOVER_DAYS, min_periods=TURNOVER_DAYS // 2).median()
        returns = c.pct_change(fill_method=None)
        cols = {"turnover": turnover, "r6": c / c.shift(R6) - 1, "r12": c / c.shift(R12) - 1,
                "r12_1": c.shift(SKIP) / c.shift(R12) - 1,
                "vol": returns.rolling(VOL_DAYS, min_periods=VOL_DAYS * 3 // 4).std() * math.sqrt(252),
                "sma": c.rolling(sma_days, min_periods=sma_days).mean(), "close": raw}
        for k, s in cols.items():
            out[k][:, j] = s.to_numpy()[rows]
        first = int(np.argmax(traded.to_numpy()))
        eligible[:, j] = (np.asarray(rows) - first >= HISTORY_DAYS) & traded.to_numpy()[rows]
    f = Features(eligible=eligible, **out)
    f.eligible &= np.isfinite(f.turnover) & (f.turnover > 0) & np.isfinite(f.r6) & np.isfinite(f.r12) \
        & np.isfinite(f.vol) & (f.vol > 0) & np.isfinite(f.sma) & np.isfinite(f.r12_1)
    return f


def _z(x: np.ndarray) -> np.ndarray:
    sd = x.std()
    return (x - x.mean()) / sd if sd > 0 else np.zeros_like(x)


def rank_day(f: Features, k: int, universe: int, score_by: str = "risk_adjusted") -> list[dict]:
    """Universe for decision row k, best first: [{col, rank, score, ...}]."""
    cols = np.flatnonzero(f.eligible[k])
    if not len(cols):
        return []
    cols = cols[np.argsort(-f.turnover[k, cols], kind="stable")][:universe]
    if score_by in ("12-1", "tuned"):
        score = f.r12_1[k, cols]  # raw %, no volatility adjustment
    else:
        s6, s12 = f.r6[k, cols] / f.vol[k, cols], f.r12[k, cols] / f.vol[k, cols]
        score = (_z(s6) + _z(s12)) / 2
    order = np.argsort(-score, kind="stable")
    return [{"col": int(cols[o]), "rank": r, "score": float(score[o]), "r6": float(f.r6[k, cols[o]]),
             "r12": float(f.r12[k, cols[o]]), "r12_1": float(f.r12_1[k, cols[o]]), "vol": float(f.vol[k, cols[o]]), "close": float(f.close[k, cols[o]]),
             "sma": float(f.sma[k, cols[o]]), "above_sma": bool(f.close[k, cols[o]] >= f.sma[k, cols[o]])}
            for r, o in enumerate(order, 1)]


def decide(ranking: list[dict], held: list[int], slots: int, exit_rank: int) -> tuple[list[dict], list[int], list[dict]]:
    """Month-end decision: (sells with reasons, kept columns, buys)."""
    by_col = {r["col"]: r for r in ranking}
    sells, keep = [], []
    for col in held:
        r = by_col.get(col)
        if r is None:
            sells.append({"col": col, "rank": None, "reason": "Left the universe"})
        elif r["rank"] > exit_rank:
            sells.append({"col": col, "rank": r["rank"], "reason": f"Rank {r['rank']} worse than {exit_rank}"})
        elif not r["above_sma"]:
            sells.append({"col": col, "rank": r["rank"], "reason": "Closed below its average"})
        else:
            keep.append(col)
    free = slots - len(keep)
    held_set, buys = set(held), []
    for r in ranking:
        if len(buys) >= free:
            break
        if r["col"] not in held_set and r["above_sma"]:
            buys.append(r)
    return sells, keep, buys


def _r(x, nd=2):
    return None if x is None or not math.isfinite(x) else round(float(x), nd)


def run(dates: list[date], symbols: list[str], open_: np.ndarray, close: np.ndarray, volume: np.ndarray,
        st: Settings, benchmark: np.ndarray | None = None, fills: dict | None = None) -> dict:
    """Backtest over aligned [day × stock] arrays (NaN = no trade that day). `fills` = {"sell": arr, "buy": arr}
    of the same shape: where finite, a sale fills at SELL_TIME / a purchase at BUY_TIME at that price."""
    fill = lambda kind, i, col: (fills[kind][i, col] if fills and np.isfinite(fills[kind][i, col]) else None)
    cost = st.cost_pct / 100
    lo = next((i for i, d in enumerate(dates) if d >= st.start), len(dates))
    hi = max((i for i, d in enumerate(dates) if d <= st.end), default=-1)
    if lo > hi:
        raise ValueError("No trading days in the selected range")
    rows = [i for i in month_ends(dates) if lo <= i <= hi]
    # Data ending mid-month: its last day is a month-end only if no weekday is left in that month.
    if rows and rows[-1] == len(dates) - 1 and not last_weekday_of_month(dates[-1]):
        rows = rows[:-1]
    latest = hi
    sample = rows + ([latest] if latest not in rows else [])
    f = features(close, volume, sample, st.sma_days)
    last_close = pd.DataFrame(close).ffill().to_numpy()

    cash, pos = float(st.capital), {}  # col -> {qty, price, date, rank}
    equity, trades, months, costs = [], [], [], 0.0
    decisions = {row: k for k, row in enumerate(rows)}
    pending = None  # decision waiting for the next open
    first_day = lo

    for i in range(lo, hi + 1):
        if pending is not None:
            sells, buys, free, meta = pending
            pending = None
            sold, bought = [], []
            for s in sells:
                col, p = s["col"], pos.pop(s["col"])
                j = i
                while j <= hi and not np.isfinite(open_[j, col]):
                    j += 1  # no trade at this open: sell at the stock's next open
                px, when, at = (open_[j, col], dates[j], OPEN_TIME) if j <= hi else (last_close[hi, col], dates[hi], CLOSE_TIME)
                if j <= hi and fill("sell", j, col) is not None:
                    px, at = fill("sell", j, col), SELL_TIME
                gross = p["qty"] * px
                fee = gross * cost
                cash += gross - fee
                costs += fee
                pnl = gross - fee - p["cost_basis"]
                # Portfolio right after this sale: cash + other holdings at that day's open (else last close).
                k = min(j, hi)
                capital = cash + sum(q["qty"] * (open_[k, c] if np.isfinite(open_[k, c]) else last_close[k, c])
                                     for c, q in pos.items() if np.isfinite(last_close[k, c]))
                trades.append({"symbol": symbols[col], "entry_date": p["date"].isoformat(), "entry_time": p["time"], "entry": _r(p["price"]),
                               "exit_date": when.isoformat(), "exit_time": at, "exit": _r(px), "qty": p["qty"], "pnl": _r(pnl),
                               "capital_after": _r(capital), "pnl_pct": _r(pnl / p["cost_basis"] * 100), "days": (when - p["date"]).days,
                               "entry_rank": p["rank"], "exit_rank": s["rank"], "reason": s["reason"]})
                sold.append({"symbol": symbols[col], "rank": s["rank"], "reason": s["reason"], "price": _r(px), "at": f"{when} {at}",
                             "qty": p["qty"], "pnl": _r(pnl)})
            amount = cash / free if free > 0 else 0
            for b in buys:
                col, px = b["col"], open_[i, b["col"]]
                at = OPEN_TIME
                if fill("buy", i, col) is not None:
                    px, at = fill("buy", i, col), BUY_TIME
                if not np.isfinite(px) or px <= 0:
                    bought.append({"symbol": symbols[col], "rank": b["rank"], "skipped": "No opening price"})
                    continue
                qty = int(amount // (px * (1 + cost)))
                if qty <= 0:
                    bought.append({"symbol": symbols[col], "rank": b["rank"], "skipped": "Price above the slot amount"})
                    continue
                spend, fee = qty * px, qty * px * cost
                cash -= spend + fee
                costs += fee
                pos[col] = {"qty": qty, "price": px, "date": dates[i], "time": at, "rank": b["rank"], "cost_basis": spend + fee}
                bought.append({"symbol": symbols[col], "rank": b["rank"], "price": _r(px), "at": f"{dates[i]} {at}", "qty": qty, "amount": _r(spend)})
            times = sorted({x["at"].split(" ")[1] for x in sold + bought if x.get("at")})
            meta.update(executed=f"{dates[i]} {' / '.join(times) or OPEN_TIME}", sold=sold, bought=bought, cash_after=_r(cash))
        value = cash + sum(p["qty"] * last_close[i, c] for c, p in pos.items() if np.isfinite(last_close[i, c]))
        equity.append([dates[i].isoformat(), _r(value)])
        if i in decisions:
            ranking = rank_day(f, decisions[i], st.universe, st.score)
            sells, keep, buys = decide(ranking, list(pos), st.slots, st.exit_rank)
            rank_of = {r["col"]: r["rank"] for r in ranking}
            meta = {"date": dates[i].isoformat(), "universe": len(ranking), "value": _r(value),
                    "holdings": [{"symbol": symbols[c], "rank": rank_of.get(c)} for c in pos],
                    "kept": [symbols[c] for c in keep], "free": st.slots - len(keep),
                    "planned_sells": [{"symbol": symbols[s["col"]], "rank": s["rank"], "reason": s["reason"]} for s in sells],
                    "planned_buys": [{"symbol": symbols[b["col"]], "rank": b["rank"]} for b in buys]}
            months.append(meta)
            if i < hi and (sells or buys):
                pending = (sells, buys, st.slots - len(keep), meta)
            elif i == hi:
                meta["pending"] = True

    # Open positions at the end, marked at the last close (not sold).
    holdings = [{"symbol": symbols[c], "qty": p["qty"], "entry_date": p["date"].isoformat(), "entry_time": p["time"], "entry": _r(p["price"]),
                 "close": _r(last_close[hi, c]), "value": _r(p["qty"] * last_close[hi, c]),
                 "pnl": _r(p["qty"] * last_close[hi, c] - p["cost_basis"]),
                 "pnl_pct": _r((p["qty"] * last_close[hi, c] / p["cost_basis"] - 1) * 100), "entry_rank": p["rank"]}
                for c, p in pos.items()]
    k_latest = sample.index(latest)
    held_cols = set(pos)
    latest_rank = [{"symbol": symbols[r["col"]], "rank": r["rank"], "score": _r(r["score"], 3), "r6": _r(r["r6"] * 100, 1),
                    "r12": _r(r["r12"] * 100, 1), "r12_1": _r(r["r12_1"] * 100, 1), "vol": _r(r["vol"] * 100, 1), "close": _r(r["close"]),
                    "sma": _r(r["sma"]), "above_sma": r["above_sma"], "held": r["col"] in held_cols}
                   for r in rank_day(f, k_latest, st.universe, st.score)[:50]]
    bench = None
    if benchmark is not None:
        b = pd.Series(benchmark[lo:hi + 1]).ffill().to_numpy()
        base = next((x for x in b if np.isfinite(x)), None)
        if base:
            bench = [[dates[lo + k].isoformat(), _r(st.capital * x / base)] for k, x in enumerate(b) if np.isfinite(x)]
    return {"equity": equity, "benchmark": bench, "trades": trades, "months": months, "holdings": holdings,
            "cash": _r(cash), "latest": {"date": dates[latest].isoformat(), "ranking": latest_rank,
                                        "is_month_end": latest in decisions},
            "stats": stats(equity, bench, trades, st, costs, dates[first_day], dates[hi]),
            "monthly": monthly_returns(equity), "settings": {**asdict(st), "start": st.start.isoformat(), "end": st.end.isoformat()}}


def _curve_stats(values: np.ndarray, years: float) -> dict:
    rets = np.diff(values) / values[:-1] if len(values) > 1 else np.array([])
    peak = np.maximum.accumulate(values)
    dd = (values / peak - 1).min() if len(values) else 0
    cagr = (values[-1] / values[0]) ** (1 / years) - 1 if years > 0 and values[0] > 0 else 0
    vol = rets.std() * math.sqrt(252) if len(rets) > 1 else 0
    sharpe = rets.mean() / rets.std() * math.sqrt(252) if len(rets) > 1 and rets.std() > 0 else 0
    return {"cagr": _r(cagr * 100), "max_dd": _r(dd * 100), "vol": _r(vol * 100), "sharpe": _r(sharpe),
            "total": _r((values[-1] / values[0] - 1) * 100)}


def stats(equity, bench, trades, st: Settings, costs: float, first: date, last: date) -> dict:
    values = np.array([v for _, v in equity], dtype=float)
    years = (last - first).days / 365.25
    out = {"final": _r(values[-1]), "years": _r(years), "trades": len(trades), "costs": _r(costs),
           **_curve_stats(np.concatenate([[st.capital], values]), years)}
    wins = [t for t in trades if t["pnl"] > 0]
    out["win_rate"] = _r(len(wins) / len(trades) * 100, 1) if trades else None
    out["avg_days"] = _r(sum(t["days"] for t in trades) / len(trades), 0) if trades else None
    if bench:
        b = _curve_stats(np.array([v for _, v in bench], dtype=float), years)
        out["bench_cagr"], out["bench_max_dd"], out["bench_total"] = b["cagr"], b["max_dd"], b["total"]
    return out


def monthly_returns(equity) -> list[dict]:
    """Month-over-month % change of month-end values: [{year, month, pct}]."""
    s = pd.Series([v for _, v in equity], index=pd.to_datetime([d for d, _ in equity]))
    ends = s.groupby([s.index.year, s.index.month]).last()
    prev = s.iloc[0]
    out = []
    for (y, m), v in ends.items():
        out.append({"year": int(y), "month": int(m), "pct": _r((v / prev - 1) * 100)})
        prev = v
    return out
