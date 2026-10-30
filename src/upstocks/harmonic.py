"""Harmonic (Fibonacci pullback) — "Harmonic B-Pro", one trade a day.

- Index 1-min candles (NIFTY, or SENSEX for the sensex_* presets; previous session + today) → zigzag swings: a swing high/low
  counts only once price has reversed harm_zz % from it (no look-ahead).
- Last confirmed leg P1→P2. Up-leg: once price has pulled back to within harm_tol
  of the leg around P2 − harm_fib·leg and a green candle closes back above that
  level (close ≥ previous close), with price never below P1 → CE. Down-leg mirrored
  → PE. Signals from entry_start to last_entry; skipped on expiry days.
- Option harm_itm points in the money (CE ATM−400 / PE ATM+400), nearest expiry,
  entry at the next minute's open.
- Exits: SL sl_pct % (preset 50 % — a safety stop for crash days; it was never hit in the
  tuning year), target target_pct %, trailing stop once the
  high reached +trail_start % (give back trail_giveback % of the gain), the
  EMA9/26 pullback's reversal exit once +ep_rev_activate % (out at the next
  minute's open), time exit at time_exit (at that minute's open).

Tuned 2026-10-13 on 1 Oct 2025 – 30 Sep 2026 (1 lot, after charges): net ₹3,10,815,
117 trades, 81 wins, every month profitable, max drawdown ₹37,664.
"""
from __future__ import annotations

from datetime import date

from . import upstox
from .ema_pullback import _bars_oi, reversal
from .vwap import DayResult, Settings, Trade, _add, _mins, _nearest_expiry, _round2, span_rows


def zigzag(cs: list, pct: float) -> list:
    """Pivots (index of the extreme, price, 'H'/'L', index where it got confirmed)."""
    piv, trend = [], 0
    hi_i, hi_p, lo_i, lo_p = 0, cs[0][2], 0, cs[0][3]
    ext_i = ext_p = None
    for i, c in enumerate(cs):
        if trend == 0:
            if c[2] > hi_p:
                hi_i, hi_p = i, c[2]
            if c[3] < lo_p:
                lo_i, lo_p = i, c[3]
            if hi_p >= lo_p * (1 + pct / 100):
                if lo_i < hi_i:
                    piv.append((lo_i, lo_p, "L", i)); trend, ext_i, ext_p = 1, hi_i, hi_p
                else:
                    piv.append((hi_i, hi_p, "H", i)); trend, ext_i, ext_p = -1, lo_i, lo_p
        elif trend == 1:
            if c[2] >= ext_p:
                ext_i, ext_p = i, c[2]
            elif c[3] <= ext_p * (1 - pct / 100):
                piv.append((ext_i, ext_p, "H", i)); trend, ext_i, ext_p = -1, i, c[3]
        else:
            if c[3] <= ext_p:
                ext_i, ext_p = i, c[3]
            elif c[2] >= ext_p * (1 + pct / 100):
                piv.append((ext_i, ext_p, "L", i)); trend, ext_i, ext_p = 1, i, c[2]
    return piv


def first_signal(cs: list, piv: list, fib: float, tol: float, i0: int, start_t: str, last_t: str):
    """(candle index, 'CE'/'PE', leg) of the first pullback signal in [start_t, last_t], or None."""
    for i in range(i0, len(cs)):
        c, pc = cs[i], cs[i - 1]
        if c[0] < start_t:
            continue
        if c[0] > last_t:
            return None
        conf = [p for p in piv if p[3] <= i]
        if len(conf) < 2:
            continue
        p1, p2 = conf[-2], conf[-1]
        leg = abs(p2[1] - p1[1])
        win = cs[p2[3]:i + 1]
        if p2[2] == "H":
            lvl = p2[1] - fib * leg
            if any(x[3] <= lvl + tol * leg for x in win[:-1]) and min(x[3] for x in win) > p1[1] \
                    and c[4] > lvl and c[4] > c[1] and c[4] >= pc[4]:
                return i, "CE", (p1[1], p2[1])
        else:
            lvl = p2[1] + fib * leg
            if any(x[2] >= lvl - tol * leg for x in win[:-1]) and max(x[2] for x in win) < p1[1] \
                    and c[4] < lvl and c[4] < c[1] and c[4] <= pc[4]:
                return i, "PE", (p1[1], p2[1])
    return None


def resolve(ob: list, ei: int, mark: str, s: Settings):
    """Walk the option's minutes from the entry minute: (status, exit price, exit minute, peak gain, trailed)."""
    entry = ob[ei][1]
    sl0 = entry * (1 - s.sl_pct / 100)
    sl, tg = sl0, entry * (1 + s.target_pct / 100)
    armed = pending = False
    peak, by_t = entry, None
    for k in range(ei, len(ob)):
        t, o, h, l = ob[k][:4]
        if t >= s.time_exit:
            return "TIME_EXIT", o, t, peak - entry, sl > sl0
        if pending:
            return "REVERSAL", o, t, peak - entry, sl > sl0
        if l <= sl:
            return ("TRAIL" if sl > sl0 else "SL"), min(sl, o), t, peak - entry, sl > sl0
        if h >= tg:
            return "TARGET", max(tg, o), t, peak - entry, sl > sl0
        peak = max(peak, h)
        if s.trail_start and peak >= entry * (1 + s.trail_start / 100):
            sl = max(sl, peak - (peak - entry) * s.trail_giveback / 100)
        if s.rev_exit:
            armed = armed or h >= entry * (1 + s.ep_rev_activate / 100)
            # a clock 5-min candle formed wholly from the entry minute on completes now
            if armed and (_mins(t) - 555) % 5 == 4 and k >= 4 and ob[k - 4][0] == _add(t, -4) and ob[k - 4][0] >= mark:
                by_t = by_t or {b[0]: b for b in ob}
                if reversal(ob, by_t, k - 4):
                    pending = True
    return "EOD", ob[-1][4], ob[-1][0], peak - entry, sl > sl0


def resolve_elliott(ob: list, ei: int, s: Settings, spot: dict, side: str, p1: float, p2: float):
    """Harmonic Power Trail exit: the option's SL, target cap and time exit as in resolve(); profit is
    trailed and booked by Elliott waves on the index (decided on each completed index minute, out at
    the option's next minute open). The signal leg is wave 1 and the entry pullback wave 2.
    - Wave 3 runs from the entry; once it has passed wave 1's end, an index CLOSE back past wave 1's end
      exits (wave 4 may not overlap wave 1) — a wick alone doesn't.
    - Wave 4 is the first harm_zz % pullback from wave 3's extreme.
    - Wave 5 starts when wave 3's extreme is broken: an index close past wave 4's extreme exits, and
      profit is booked when wave 5 = wave 1 (wave 4's extreme ± the wave-1 length).
    CE uses the index as is; PE mirrors it. Returns (status, exit price, exit minute, peak gain, trailed)."""
    entry = ob[ei][1]
    sl0, tg = entry * (1 - s.sl_pct / 100), entry * (1 + s.target_pct / 100)
    sg = 1 if side == "CE" else -1                    # work in the trade's favour: x = sg × index
    w1_end, w1 = sg * p2, abs(p2 - p1)
    phase, h3, l4, w5_target, pending, peak = 3, None, None, None, None, entry
    for k in range(ei, len(ob)):
        t, o, h, l = ob[k][:4]
        if t >= s.time_exit:
            return "TIME_EXIT", o, t, peak - entry, phase >= 4
        if pending:
            return pending, o, t, peak - entry, phase >= 4
        if l <= sl0:
            return "SL", min(sl0, o), t, peak - entry, phase >= 4
        if h >= tg:
            return "TARGET", max(tg, o), t, peak - entry, phase >= 4
        peak = max(peak, h)
        b = spot.get(t)
        if not b:
            continue
        hi, lo, cl = (b[2], b[3], b[4]) if sg > 0 else (-b[3], -b[2], -b[4])
        h3 = hi if h3 is None else h3
        if phase in (3, 4) and h3 > w1_end and cl < w1_end:
            pending = "W4_OVERLAP"
        elif phase == 3:
            h3 = max(h3, hi)
            if h3 > w1_end and h3 - lo >= abs(h3) * s.harm_zz / 100:
                phase, l4 = 4, lo
        elif phase == 4:
            if hi > h3:
                phase, w5_target = 5, l4 + w1
                if hi >= w5_target:
                    pending = "W5_TARGET"
            else:
                l4 = min(l4, lo)
        elif phase == 5:
            if cl < l4:
                pending = "W4_BREAK"
            elif hi >= w5_target:
                pending = "W5_TARGET"
    return "EOD", ob[-1][4], ob[-1][0], peak - entry, phase >= 4


async def run_day(day: date, s: Settings, spot_bars: list, prev_spot: list | None = None) -> DayResult:
    idx = s.underlying
    expiry = await _nearest_expiry(day, idx)
    res = DayResult(day.isoformat(), expiry)
    if not expiry:
        res.note = "no expiry"
        return res
    d = day.isoformat()
    if s.harm_skip_expiry and expiry == d:
        res.note = "expiry day — no harmonic trade"
        return res
    prev = list(prev_spot or [])
    if not prev:  # backtests pass only today's bars: fetch the previous session for the swings
        days = await vwap_days(day, idx)
        prev = days
    cs = prev + list(spot_bars)
    if len(cs) < 3:
        res.note = f"not enough {idx} candles"
        return res
    sig = first_signal(cs, zigzag(cs, s.harm_zz), s.harm_fib, s.harm_tol, len(prev) + 1, s.entry_start, s.last_entry)
    if not sig:
        res.note = f"no {s.harm_fib:g} Fib pullback signal {s.entry_start}–{s.last_entry}"
        return res
    i, side, (p1, p2) = sig
    if s.harm_min_leg and abs(p2 - p1) < s.harm_min_leg:
        res.note = f"leg {abs(p2 - p1):.0f} pts is under {s.harm_min_leg:g} — no harmonic trade"
        return res
    t, spot = cs[i][0], cs[i][4]
    step = upstox.STRIKE_STEP[idx]
    atm = round(spot / step) * step
    strike = atm - s.harm_itm if side == "CE" else atm + s.harm_itm
    expired = expiry in set(await upstox.expired_expiries(idx))
    c = next((c for c in await upstox.contracts(expiry, idx)
              if c["strike_price"] == strike and c["instrument_type"] == side), None)
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
    if s.harm_elliott:
        status, exit_p, exit_t, peak, trailed = resolve_elliott(ob, ei, s, {b[0]: b for b in spot_bars}, side, p1, p2)
    else:
        status, exit_p, exit_t, peak, trailed = resolve(ob, ei, mark, s)
    pnl = _round2(exit_p - entry)
    res.trades.append(Trade(
        date=d, expiry=expiry, symbol=c["trading_symbol"], strike=c["strike_price"], type=side,
        instrument_key=c["instrument_key"], entry_time=ob[ei][0], entry=_round2(entry),
        sl=_round2(entry * (1 - s.sl_pct / 100)), target=_round2(entry * (1 + s.target_pct / 100)),
        candle=f"{t}–{mark}", close=spot, vwap=0.0,
        other=f"leg {p1:.2f} → {p2:.2f}", status=status, exit_time=exit_t, exit=_round2(exit_p), pnl=pnl,
        pnl_pct=_round2(pnl / entry * 100), peak=_round2(peak), sl_to_breakeven=trailed, spot=spot,
        why=f"{s.harm_fib:g} Fib pullback · leg {p1:.0f}→{p2:.0f} · ITM {s.harm_itm:g}",
    ))
    return res


async def vwap_days(day: date, index: str = "NIFTY") -> list:
    """The previous session's index 1-min bars (for the zigzag), from the cached span."""
    from datetime import timedelta
    from . import vwap
    spot = await vwap.trading_days_and_spot(day - timedelta(days=10), day, index)
    keys = [k for k in sorted(spot) if k < day.isoformat()]
    return spot[keys[-1]] if keys else []
