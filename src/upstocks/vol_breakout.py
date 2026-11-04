"""Vol Breakout (Expiry) — NIFTY, only on its weekly expiry day, one trade.

- Levels: NIFTY's 09:15 open ± vb_k × ATR, ATR = the average (high − low) of the previous vb_atr_days sessions.
- Signal: vb_closes consecutive completed 1-min NIFTY closes above the upper level → CE, below the lower → PE;
  the first one from entry_start to last_entry (10:00) is taken.
- Option: that day's expiry, ~vb_itm_pct % of the level in the money (CE strike below, PE above), entry at the
  next minute's open.
- Exits: SL −sl_pct %; once the high is +trail_start %, the stop trails to give back trail_giveback % of the best
  gain; a target cap (target_pct, never reached in the test); otherwise out at the day's last minute (15:29).

Chosen 2026-11-05 from a 2-year test (Oct 2024 – Sep 2026, 1 lot, after charges, no slippage): +₹73,054 and
+₹71,476, max drawdown ₹14,623, 52 of 100 trades won; Oct 2026 (unseen) +₹6,732. ₹2 slippage a side: +₹1,25,273 in
all. 63 % of ~29,000 neighbouring settings were profitable in all three periods. Tried and worse: the day(s) before
expiry (more trades, 2.5–5× the drawdown), SENSEX (more drawdown than profit), a first-touch entry, small targets.
"""
from __future__ import annotations

from datetime import date, datetime

from . import upstox
from .vwap import DayResult, Settings, Trade, _market, _nearest_expiry, _round2, span_rows

STEP = upstox.STRIKE_STEP["NIFTY"]


def atr(history: list[list]) -> float | None:
    """Average daily (high − low) of the given sessions' 1-min bars."""
    days = [d for d in history if d]
    if not days:
        return None
    return sum(max(b[2] for b in d) - min(b[3] for b in d) for d in days) / len(days)


def signal(bars: list, up: float, dn: float, n: int, start: str, last: str):
    """(index of the confirming 1-min bar, 'CE'/'PE') for the first n closes in a row beyond a level, or None."""
    for i in range(1, len(bars)):
        t = bars[i][0]
        if t > last:
            return None
        if t < start or i < n:
            continue
        w = bars[i - n + 1:i + 1]
        if all(b[4] > up for b in w):
            return i, "CE"
        if all(b[4] < dn for b in w):
            return i, "PE"
    return None


def resolve(ob: list, ei: int, s: Settings):
    """Walk the option from the entry minute: (status, exit price, exit minute, peak gain, trailed)."""
    entry = ob[ei][1]
    sl0, tg = entry * (1 - s.sl_pct / 100), entry * (1 + s.target_pct / 100)
    stop, peak = sl0, entry
    for t, o, h, l, *_ in ob[ei:]:
        if l <= stop:
            return ("TRAIL" if stop > sl0 else "SL"), min(o, stop), t, peak - entry, stop > sl0
        if h >= tg:
            return "TARGET", max(o, tg), t, peak - entry, stop > sl0
        peak = max(peak, h)
        if s.trail_start and peak >= entry * (1 + s.trail_start / 100):
            stop = max(stop, peak - (peak - entry) * s.trail_giveback / 100)
    t, c = ob[-1][0], ob[-1][4]
    return ("TIME_EXIT" if t >= s.time_exit else "EOD"), c, t, peak - entry, stop > sl0


async def run_day(day: date, s: Settings, spot_bars: list, history: list[list]) -> DayResult:
    """`history`: the previous sessions' NIFTY 1-min bars (oldest first), at least vb_atr_days of them."""
    expiry = await _nearest_expiry(day)
    res = DayResult(day.isoformat(), expiry)
    d = day.isoformat()
    if not expiry:
        res.note = "no expiry"
        return res
    if expiry != d:
        res.note = f"not an expiry day (next expiry {expiry}) — trades only on NIFTY's expiry day"
        return res
    a = atr(history[-s.vb_atr_days:]) if len(history) >= s.vb_atr_days else None
    if not a or not spot_bars:
        res.note = f"needs {s.vb_atr_days} previous sessions for the ATR"
        return res
    cutoff = "99:99"
    if day == upstox.today_ist():  # live: leave out the minute still forming
        cutoff = f"{datetime.now(upstox.IST):%H:%M}"
    bars = [b for b in spot_bars if b[0] < cutoff]
    up, dn = spot_bars[0][1] + s.vb_k * a, spot_bars[0][1] - s.vb_k * a
    sig = signal(bars, up, dn, s.vb_closes, s.entry_start, s.last_entry)
    if not sig:
        res.note = (f"no {s.vb_closes} closes beyond {dn:.2f} / {up:.2f} (open ± {s.vb_k:g} × ATR {a:.0f}) "
                    f"{s.entry_start}–{s.last_entry}")
        return res
    i, side = sig
    lvl = up if side == "CE" else dn
    atm, itm = round(lvl / STEP) * STEP, round(lvl * s.vb_itm_pct / 100 / STEP) * STEP
    strike = atm - itm if side == "CE" else atm + itm
    c = next((c for c in await upstox.contracts(expiry) if c["strike_price"] == strike and c["instrument_type"] == side), None)
    if not c:
        res.note = f"no {strike:g} {side} contract"
        return res
    expired = expiry in set(await upstox.expired_expiries())
    ob = [b for b in _market([r for r in await span_rows(c["instrument_key"], day, expiry, expired) if r[0][:10] == d])
          if b[0] < cutoff]
    mark = bars[i + 1][0] if i + 1 < len(bars) else None
    ei = next((k for k, b in enumerate(ob) if mark and b[0] >= mark), None)
    if ei is None:
        res.note = f"{c['trading_symbol']}: signal at {bars[i][0]}, waiting for the entry minute"
        return res
    entry = ob[ei][1]
    status, exit_p, exit_t, peak, trailed = resolve(ob, ei, s)
    pnl = _round2(exit_p - entry)
    res.trades.append(Trade(
        date=d, expiry=expiry, symbol=c["trading_symbol"], strike=c["strike_price"], type=side,
        instrument_key=c["instrument_key"], entry_time=ob[ei][0], entry=_round2(entry),
        sl=_round2(entry * (1 - s.sl_pct / 100)), target=_round2(entry * (1 + s.target_pct / 100)),
        candle=bars[i][0], close=bars[i][4], vwap=0.0, other=f"levels {dn:.2f} / {up:.2f}",
        status=status, exit_time=exit_t, exit=_round2(exit_p), pnl=pnl, pnl_pct=_round2(pnl / entry * 100),
        peak=_round2(peak), sl_to_breakeven=trailed, spot=bars[i][4],
        why=f"{s.vb_closes} closes {'above' if side == 'CE' else 'below'} {lvl:.2f} (open ± {s.vb_k:g} × ATR {a:.0f}) · "
            f"ITM {itm:g}",
    ))
    return res
