"""EMA9/EMA26 pullback — Mani's strategy, replayed on Upstox data.

- Signals on completed NIFTY 1-min candles 09:36–14:59; EMA9 / EMA26 restart
  from the day's first close (no warm-up). Entry at the next minute's open
  (09:37–15:00); the first fully qualifying signal, one trade a day.
- CE: EMA9 > EMA26, EMA26 > EMA26 5 min earlier, previous candle's low ≤ its
  EMA9, close > previous high, close > EMA9. PE: the mirror image.
- Option: nearest expiry on/after the day; strikes within 2,000 pts of NIFTY;
  signal-minute close ≥ ₹150 and < ₹300, closest to ₹225 (ties → higher
  volume, then lower strike); signal-minute volume ≥ 65; a candle 5 min
  earlier must exist. Confirmation on that strike only: close up ≥ 2% and OI
  up ≥ 0.5% over 5 min (OI > 0 at both). If it fails, wait for the next signal.
- Fill: next minute's open + ₹1 slippage, rounded up to the ₹0.05 tick.
  SL = entry × 0.75 (rounded down), target = entry × 2 (rounded up).
- Each minute from entry: 15:20 → exit at the open. Otherwise a gap through
  SL/target, or a pending reversal, exits at the open; then SL (first, if both
  are touched in the same minute) / target inside the minute.
- Reversal exit, once a completed option candle's high reached entry × 1.2:
  on each completed clock 5-min option candle formed wholly after entry, all of
  - peak minute (highest high in it): close > close 5 min earlier, OI < OI then
  - peak minute volume ≥ 1.5 × median volume of the 20 minutes before it
  - upper wick ≥ 40% of the candle's range
  - its high ≥ the highest high of the 30 minutes before it + ₹0.05
  → exit at the next minute's open.
- Exits fill ₹1 worse (rounded down to the tick). Brokerage/taxes are left to
  the results table's charges, as for the other strategies.
"""
from __future__ import annotations

import math
from datetime import date
from statistics import median

from . import upstox
from .vwap import DayResult, Settings, Trade, _add, _mins, _nearest_expiry, _round2, span_rows

TICK = 0.05


def _ticks_down(x: float) -> float:
    return _round2(math.floor(round(x / TICK, 6)) * TICK)


def _ticks_up(x: float) -> float:
    return _round2(math.ceil(round(x / TICK, 6)) * TICK)


def _ema(vals: list[float], p: int) -> list[float]:
    k, out = 2 / (p + 1), []
    for i, v in enumerate(vals):
        out.append(v if i == 0 else out[-1] + k * (v - out[-1]))
    return out


def _bars_oi(rows: list[list], d: str) -> list[tuple]:
    """(hhmm, open, high, low, close, volume, oi) for day d, market hours."""
    out = []
    for r in rows:
        t = r[0][11:16]
        if r[0][:10] == d and "09:15" <= t <= "15:29":
            out.append((t, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0),
                        float(r[6] or 0) if len(r) > 6 else 0.0))
    return out


def signal(spot: list[tuple], e9: list[float], e26: list[float], i: int) -> str | None:
    if i < 5:
        return None
    c, p = spot[i], spot[i - 1]
    if e9[i] > e26[i] and e26[i] > e26[i - 5] and p[3] <= e9[i - 1] and c[4] > p[2] and c[4] > e9[i]:
        return "CE"
    if e9[i] < e26[i] and e26[i] < e26[i - 5] and p[2] >= e9[i - 1] and c[4] < p[3] and c[4] < e9[i]:
        return "PE"
    return None


def reversal(bars: list[tuple], by_t: dict, j0: int) -> bool:
    """Clock 5-min candle bars[j0:j0+5] shows the price-up/OI-down volume-spike rejection."""
    ch = bars[j0:j0 + 5]
    o, hi, lo, cl = ch[0][1], max(b[2] for b in ch), min(b[3] for b in ch), ch[-1][4]
    if hi - lo <= 0 or (hi - max(o, cl)) / (hi - lo) < 0.4:
        return False
    pk = max(range(5), key=lambda k: (ch[k][2], -k))  # highest high, first one on a tie
    p = ch[pk]
    p5 = by_t.get(_add(p[0], -5))
    if not p5 or not (p[4] > p5[4] and p[6] < p5[6]):
        return False
    prior = [by_t[t][5] for t in (_add(p[0], -k) for k in range(1, 21)) if t in by_t]
    if len(prior) < 20 or p[5] < 1.5 * median(prior):
        return False
    before = [by_t[t][2] for t in (_add(ch[0][0], -k) for k in range(1, 31)) if t in by_t]
    return len(before) == 30 and hi >= max(before) + TICK - 1e-9


async def run_day(day: date, s: Settings, spot_bars: list[tuple]) -> DayResult:
    expiry = await _nearest_expiry(day)
    res = DayResult(day.isoformat(), expiry)
    if not expiry:
        res.note = "no expiry"
        return res
    expired = expiry in set(await upstox.expired_expiries())
    contracts = await upstox.contracts(expiry)
    d = day.isoformat()
    cache: dict[str, list[tuple]] = {}

    async def bars(c: dict) -> list[tuple]:
        k = c["instrument_key"]
        if k not in cache:
            cache[k] = _bars_oi(await span_rows(k, day, expiry, expired), d)
        return cache[k]

    spot = spot_bars
    closes = [b[4] for b in spot]
    e9, e26 = _ema(closes, 9), _ema(closes, 26)
    lo_p, hi_p, want = s.min_premium, s.max_premium, (s.min_premium + s.max_premium) / 2

    async def pick(side: str, t_sig: str, nifty: float):
        """In-band strike closest to the middle of the band at the signal minute."""
        legs = sorted((c for c in contracts if c["instrument_type"] == side
                       and abs(c["strike_price"] - nifty) <= 2000), key=lambda c: c["strike_price"])
        atm_i = min(range(len(legs)), key=lambda i: abs(legs[i]["strike_price"] - nifty)) if legs else 0
        found = []
        # Walk out from ATM both ways; premiums move one way with strike, so stop
        # a direction once it's past the band (CE: ITM ≥ max, OTM < min; PE mirrored).
        for step in (-1, 1):
            i = atm_i if step == -1 else atm_i + 1
            while 0 <= i < len(legs):
                c = legs[i]
                b = next((x for x in await bars(c) if x[0] == t_sig), None)
                if b:
                    if lo_p <= b[4] < hi_p and b[5] >= 65:
                        found.append((c, b))
                    itm = (step == -1) == (side == "CE")
                    if (itm and b[4] >= hi_p) or (not itm and b[4] < lo_p):
                        break
                i += step
        if not found:
            return None
        return min(found, key=lambda cb: (abs(cb[1][4] - want), -cb[1][5], cb[0]["strike_price"]))

    first, last = _mins(s.entry_start), _mins(s.last_entry)
    seen: list[str] = []
    for i, b in enumerate(spot):
        if len(res.trades) >= s.max_trades:
            break
        if not (first <= _mins(b[0]) <= last):
            continue
        side = signal(spot, e9, e26, i)
        if not side:
            continue
        got = await pick(side, b[0], b[4])
        if not got:
            seen.append(f"{b[0]} {side}: no strike ₹{lo_p:g}–{hi_p:g} with volume ≥ 65")
            continue
        c, ob = got
        ob5 = next((x for x in await bars(c) if x[0] == _add(b[0], -5)), None)
        if not ob5:
            seen.append(f"{b[0]} {side} {c['strike_price']:g}: no candle 5 min earlier")
            continue
        prem = (ob[4] / ob5[4] - 1) * 100
        oi = (ob[6] / ob5[6] - 1) * 100 if ob[6] > 0 and ob5[6] > 0 else None
        if prem < s.ep_prem_rise or oi is None or oi < s.ep_oi_rise:
            seen.append(f"{b[0]} {side} {c['strike_price']:g}: premium {prem:+.1f}%, OI "
                        + (f"{oi:+.2f}%" if oi is not None else "n/a"))
            continue
        obars = await bars(c)
        ei = next((k for k, x in enumerate(obars) if x[0] == _add(b[0], 1)), -1)
        if ei < 0:
            seen.append(f"{b[0]} {side} {c['strike_price']:g}: no entry candle")
            continue
        entry = _ticks_up(obars[ei][1] + s.slippage)
        sl = _ticks_down(entry * (1 - s.sl_pct / 100))
        tgt = _ticks_up(entry * (1 + s.target_pct / 100))
        by_t = {x[0]: x for x in obars}
        status, exit_p, exit_t, peak, armed, pending = "EOD", obars[-1][4], obars[-1][0], 0.0, False, False
        for k in range(ei, len(obars)):
            t, o, h, l, cl = obars[k][:5]
            if t >= s.time_exit:
                status, exit_p, exit_t = "TIME_EXIT", o, t
                break
            if k > ei and o <= sl:
                status, exit_p, exit_t = "SL", o, t
                break
            if k > ei and o >= tgt:
                status, exit_p, exit_t = "TARGET", o, t
                break
            if pending:
                status, exit_p, exit_t = "REVERSAL", o, t
                break
            peak = max(peak, _round2(h - entry))
            if l <= sl:
                status, exit_p, exit_t = "SL", sl, t
                break
            if h >= tgt:
                status, exit_p, exit_t = "TARGET", tgt, t
                break
            armed = armed or h >= entry * (1 + s.ep_rev_activate / 100)
            # a clock 5-min candle (09:15, 09:20 …) completes with this minute
            if armed and (_mins(t) - 555) % 5 == 4:
                j0 = k - 4
                if j0 >= ei and obars[j0][0] == _add(t, -4) and reversal(obars, by_t, j0):
                    pending = True
        exit_p = _ticks_down(exit_p - s.slippage)
        pnl = _round2(exit_p - entry)
        res.trades.append(Trade(
            date=d, expiry=expiry, symbol=c["trading_symbol"], strike=c["strike_price"], type=side,
            instrument_key=c["instrument_key"], entry_time=_add(b[0], 1), entry=entry, sl=sl, target=tgt,
            candle=f"{b[0]}–{_add(b[0], 1)}", close=ob[4], vwap=0.0,
            other="; ".join(seen[-3:]) if seen else "first signal",
            status=status, exit_time=exit_t, exit=exit_p, pnl=pnl, pnl_pct=_round2(pnl / entry * 100), peak=peak,
            why=f"EMA9 {e9[i]:.1f} / EMA26 {e26[i]:.1f} · prem {prem:+.1f}% · OI {oi:+.2f}%", spot=b[4],
        ))
    if not res.trades:
        res.note = (f"{len(seen)} signal(s), none confirmed — " + "; ".join(seen[-4:])) if seen else "no EMA9/26 pullback signal 09:36–14:59"
    return res
