"""Previous-day Breakout — NIFTY options, one trade a day.

- Yesterday's NIFTY high / low is the level; the day trades only when yesterday's range is
  pdh_width_min–pdh_width_max % of price (skips flat and very wild days).
- Entry: the first 1-min NIFTY close above yesterday's high → CE, below yesterday's low → PE,
  up to last_entry; option pdh_itm points in the money (0 = ATM), nearest expiry, at the next
  minute's open.
- Exits, decided on the completed NIFTY minute and taken at the option's next minute open:
  stop when NIFTY closes back past the middle of yesterday's range; target when NIFTY reaches the
  breakout level ± pdh_target × yesterday's range; the EMA9/26 pullback's reversal exit once the
  option is up ep_rev_activate %; plus a safety stop at −sl_pct % on the option and a time exit.

Chosen 2026-11-01 from a 2-year test (Oct 2024 – Sep 2026, 1 lot, after charges): +₹48,680 and
+₹10,598 (₹59,278), max drawdown ₹14,027. Without the reversal exit it lost money in year 2;
opening-range (15 / 30 min) versions lost in both years.
"""
from __future__ import annotations

from datetime import date

from . import upstox
from .ema_pullback import _bars_oi, reversal
from .vwap import DayResult, Settings, Trade, _add, _mins, _nearest_expiry, _round2, span_rows


def signal(bars: list, prev: list, s: Settings):
    """(index, 'CE'|'PE', yesterday's high, low) of the first close beyond yesterday's range, or None."""
    if not prev or not bars:
        return None
    hi, lo = max(b[2] for b in prev), min(b[3] for b in prev)
    width = (hi - lo) / ((hi + lo) / 2) * 100
    if not s.pdh_width_min <= width <= s.pdh_width_max:
        return None
    for i, b in enumerate(bars):
        if b[0] < s.entry_start:
            continue
        if b[0] > s.last_entry:
            return None
        if b[4] > hi:
            return i, "CE", hi, lo
        if b[4] < lo:
            return i, "PE", hi, lo
    return None


def resolve(ob: list, ei: int, s: Settings, spot: dict, side: str, hi: float, lo: float):
    """(status, exit price, exit minute, peak gain) — see the module note."""
    entry = ob[ei][1]
    hard, mark, rng = entry * (1 - s.sl_pct / 100), ob[ei][0], hi - lo
    stop = (hi + lo) / 2
    target = hi + s.pdh_target * rng if side == "CE" else lo - s.pdh_target * rng
    pending, armed, by_t, peak = None, False, None, entry
    for k in range(ei, len(ob)):
        t, o, h, l = ob[k][:4]
        if t >= s.time_exit:
            return "TIME_EXIT", o, t, peak - entry
        if pending:
            return pending, o, t, peak - entry
        if l <= hard:
            return "SL", min(hard, o), t, peak - entry
        peak = max(peak, h)
        if s.rev_exit:
            armed = armed or h >= entry * (1 + s.ep_rev_activate / 100)
            if armed and (_mins(t) - 555) % 5 == 4 and k >= 4 and ob[k - 4][0] == _add(t, -4) and ob[k - 4][0] >= mark:
                by_t = by_t or {b[0]: b for b in ob}
                if reversal(ob, by_t, k - 4):
                    pending = "REVERSAL"
                    continue
        b = spot.get(t)
        if not b:
            continue
        if side == "CE":
            pending = "RANGE_STOP" if b[4] < stop else ("TARGET" if b[2] >= target else None)
        else:
            pending = "RANGE_STOP" if b[4] > stop else ("TARGET" if b[3] <= target else None)
    return "EOD", ob[-1][4], ob[-1][0], peak - entry


async def run_day(day: date, s: Settings, spot_bars: list, prev_spot: list | None = None) -> DayResult:
    expiry = await _nearest_expiry(day)
    res = DayResult(day.isoformat(), expiry)
    if not expiry:
        res.note = "no expiry"
        return res
    prev = list(prev_spot or [])
    if not prev:  # backtests pass only today's bars: fetch the previous session
        from .harmonic import vwap_days
        prev = await vwap_days(day)
    sig = signal(list(spot_bars), prev, s)
    d = day.isoformat()
    if not sig:
        res.note = f"no close beyond yesterday's high / low by {s.last_entry} (or yesterday's range outside {s.pdh_width_min:g}–{s.pdh_width_max:g} %)"
        return res
    i, side, hi, lo = sig
    t, spot = spot_bars[i][0], spot_bars[i][4]
    atm = round(spot / 50) * 50
    strike = atm - s.pdh_itm if side == "CE" else atm + s.pdh_itm
    expired = expiry in set(await upstox.expired_expiries())
    c = next((c for c in await upstox.contracts(expiry) if c["strike_price"] == strike and c["instrument_type"] == side), None)
    if not c:
        res.note = f"no {strike:g} {side} contract"
        return res
    ob = _bars_oi(await span_rows(c["instrument_key"], day, expiry, expired), d)
    mark = _add(t, 1)
    ei = next((k for k, b in enumerate(ob) if b[0] >= mark), -1)
    if ei < 0:
        res.note = f"no {strike:g} {side} candle at {mark}"
        return res
    entry = ob[ei][1]
    status, exit_p, exit_t, peak = resolve(ob, ei, s, {b[0]: b for b in spot_bars}, side, hi, lo)
    pnl = _round2(exit_p - entry)
    rng = hi - lo
    level = hi if side == "CE" else lo
    tgt_idx = hi + s.pdh_target * rng if side == "CE" else lo - s.pdh_target * rng
    res.trades.append(Trade(
        date=d, expiry=expiry, symbol=c["trading_symbol"], strike=c["strike_price"], type=side,
        instrument_key=c["instrument_key"], entry_time=ob[ei][0], entry=_round2(entry),
        sl=_round2(entry * (1 - s.sl_pct / 100)), target=0.0,
        candle=f"{t}–{mark}", close=spot, vwap=0.0,
        other=f"yesterday {lo:.2f}–{hi:.2f}", status=status, exit_time=exit_t, exit=_round2(exit_p), pnl=pnl,
        pnl_pct=_round2(pnl / entry * 100), peak=_round2(peak), spot=spot,
        why=f"close {'above' if side == 'CE' else 'below'} yesterday's {'high' if side == 'CE' else 'low'} {level:.0f} · "
            f"stop NIFTY {(hi + lo) / 2:.0f} · target NIFTY {tgt_idx:.0f}",
    ))
    return res
