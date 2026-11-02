"""VWAP Touch — NIFTY PE options, one trade a day.

- Strike: at touch_pick (09:36), the PE nearest ATM (NIFTY's last close, ±chain_strikes strikes)
  whose last 1-min close is inside the premium band (₹300–500); kept for the day.
- Signal on that option's N-min candles (3-min, grouped from 09:15) and their VWAP:
  1. a candle closes at/above VWAP with its high above it (armed);
  2. a later candle's low comes within touch_tol % of VWAP while it still closes at/above it (touch);
  3. a later candle closes above the touch candle's high (confirmation) → enter at that close, the
     minute the candle completes. A close below VWAP before that starts over.
  The first confirmation from entry_start (10:00) to last_entry (14:30) is taken.
- Exits as the live VWAP app: SL −sl_pct % on the candle low (exchange SL-M), SL to entry once
  the close is +breakeven_pct %, target +target_pct % on the minute close, time_exit.

Chosen 2026-11-02 from a 2-year test (Oct 2024 – Sep 2026, 1 lot, after charges, no slippage):
+₹2,13,063 and +₹1,81,626, max drawdown ₹25,610; Oct 2026 (after the test, unseen) about ₹0.
₹1 slippage a side: +₹1,91,750 and +₹1,59,142. Entry starts 09:30–11:00 were all profitable in both
years (10:00 best). CE with the same rules lost money (−₹84,197 in year 2), so PE only.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

from . import upstox
from .vwap import (STEP, DayResult, Settings, Trade, _add, _market, _nearest_expiry, _round2, aggregate,
                   resolve_live, span_rows, vwap_series)


def signal(bars: list, cm: int, tol: float, start: str, last: str):
    """(1-min entry index, entry price, candle, its VWAP, touch candle) of the first confirmation, or None."""
    nm = aggregate(bars, cm)
    vw = vwap_series(nm)
    armed, touch = False, None
    for i, c in enumerate(nm):
        t, _o, h, l, close, _v = c
        w = vw[i]
        if close < w:
            armed, touch = False, None
            continue
        if not armed:
            armed = i > 0 and h >= w
            continue
        if touch is None:
            if l <= w * (1 + tol / 100):
                touch = c
            continue
        if close > touch[2]:
            mark = _add(t, 1)  # the N-min candle is complete at this minute
            if mark > last:
                return None
            if mark >= start:
                j = next((k for k, b in enumerate(bars) if b[0] >= mark), None)
                if j is not None:
                    return j, close, c, w, touch
            armed, touch = False, None
    return None


async def run_day(day: date, s: Settings, spot_bars: list, prev_spot: list | None = None) -> DayResult:
    expiry = await _nearest_expiry(day)
    res = DayResult(day.isoformat(), expiry)
    if not expiry:
        res.note = "no expiry"
        return res
    d = day.isoformat()
    cutoff = "99:99"
    if day == upstox.today_ist():  # live: leave out the minute still forming
        cutoff = f"{datetime.now(upstox.IST):%H:%M}"
    before = [b for b in spot_bars if b[0] < min(s.touch_pick, cutoff)]
    if not before or cutoff < s.touch_pick:
        res.note = f"strike is picked at {s.touch_pick}"
        return res
    atm = round(before[-1][4] / STEP) * STEP
    expired = expiry in set(await upstox.expired_expiries())
    side = s.touch_side
    window = sorted((c for c in await upstox.contracts(expiry)
                     if c["instrument_type"] == side and abs(c["strike_price"] - atm) <= s.chain_strikes * STEP),
                    key=lambda c: abs(c["strike_price"] - atm))
    pick = bars = None
    for c in window:
        ob = [b for b in _market([r for r in await span_rows(c["instrument_key"], day, expiry, expired) if r[0][:10] == d])
              if b[0] < cutoff]
        prev = [b for b in ob if b[0] < s.touch_pick]
        if prev and s.min_premium <= prev[-1][4] <= s.max_premium:
            pick, bars = c, ob
            break
    if not pick:
        res.note = f"no {side} in ₹{s.min_premium:g}–{s.max_premium:g} at {s.touch_pick}"
        return res
    sig = signal(bars, s.candle_minutes, s.touch_tol, s.entry_start, s.last_entry)
    if not sig:
        res.note = (f"{pick['trading_symbol']}: no {s.candle_minutes}-min VWAP touch + close above the touch candle's high "
                    f"{s.entry_start}–{s.last_entry}")
        return res
    j, entry, candle, w, touch = sig
    mark = bars[j][0]
    sl0 = _round2(entry - _round2(entry * s.sl_pct / 100))
    tgt = _round2(entry + _round2(entry * s.target_pct / 100))
    st = replace(s, sl_on_low=True, stagnant_hours=0)
    status, exit_p, exit_t, peak, moved, _t1 = resolve_live(bars, j, entry, mark, st, sl0, tgt)
    pnl = _round2(exit_p - entry)
    cm = s.candle_minutes
    res.trades.append(Trade(
        date=d, expiry=expiry, symbol=pick["trading_symbol"], strike=pick["strike_price"], type=side,
        instrument_key=pick["instrument_key"], entry_time=mark, entry=entry, sl=sl0, target=tgt,
        candle=f"{_add(candle[0], 1 - cm)}–{_add(candle[0], 1)}", close=entry, vwap=w,
        other=f"touch {_add(touch[0], 1 - cm)}–{_add(touch[0], 1)} low {touch[3]:.2f}", status=status,
        exit_time=exit_t, exit=exit_p, pnl=pnl, pnl_pct=_round2(pnl / entry * 100), peak=peak, sl_to_breakeven=moved,
        spot=before[-1][4],
        why=f"{cm}-min VWAP touch at {_add(touch[0], 1)} (low {touch[3]:.2f} vs VWAP) · close {entry:.2f} above its high "
            f"{touch[2]:.2f} · VWAP {w:.2f}",
    ))
    return res
