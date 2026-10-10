"""SMC strategy — the frontend app's LIVE behaviour, replayed on Upstox data.

Ported from backend/src/services/smcService.js (runSMCScan, detectors,
findBestLeg, updateAlertPnL) and the gates in backend/src/routes/smc.js
(doScan), cron every minute 09:21–15:30:

- Each minute m (from 09:21): NIFTY index 1-min candles from 09:15 that are
  COMPLETE at m (the forming candle excluded); spot = last close.
- Five detectors (Liquidity Grab, FVG, Order Block, Breaker, SMT trap) score
  bull and bear; EMA-20 trend adds a bonus point. ≥2 raw concepts needed;
  direction by the same if/else chain as the original.
- No new entries from 15:00. Strike: the chain window (ATM ±15 strikes) by the
  option's live price (last 1-min close): ₹min–max, else ×0.75–×1.75, else
  ×0.5–×2; closest to ATM, ties within 50 pts → closest to the sweet spot.
- Entry at that live price. SL −6%, target2 +24% (exit), target1 +12% recorded
  as a milestone; SL → entry once the peak gain reaches 10%; 15:20 exit checked
  before price. Gates: one trade at a time, max 2/day, 2nd only after a loss
  and 15 min after its exit, no repeat of the same direction+strike within 3 min.

Index points thresholds in the detectors (isStrong 5/8/10/12/15 pts, ±1/±2
sweeps, OB/breaker zones) are the original's, applied to NIFTY index candles.
"""
from __future__ import annotations

from datetime import date
from functools import cmp_to_key

from . import upstox
from .vwap import STEP, Bar, DayResult, Settings, Trade, _add, _market, _mins, _nearest_expiry, _round2, day_rows, resolve_live

# Bar = (hhmm, open, high, low, close, volume)
O, H, L, C = 1, 2, 3, 4


def body(c: Bar) -> float: return abs(c[C] - c[O])
def rng(c: Bar) -> float: return max(c[H] - c[L], 0.01)
def bull(c: Bar) -> bool: return c[C] > c[O]
def bear(c: Bar) -> bool: return c[C] < c[O]
def strong(c: Bar, min_pts: float = 8) -> bool: return body(c) >= min_pts and body(c) / rng(c) >= 0.35


def ema(vals: list[float], p: int) -> list[float]:
    k = 2 / (p + 1)
    out: list[float] = []
    for i, v in enumerate(vals):
        out.append(v if i == 0 else out[i - 1] * (1 - k) + v * k)
    return out


def liq_grab(cs: list[Bar]) -> dict:
    if len(cs) < 12:
        return {"bull": False, "bear": False}
    n = len(cs)
    c0, c1 = cs[n - 1], cs[n - 2]
    look = cs[max(0, n - 26):n - 3]
    lo, hi = min(c[L] for c in look), max(c[H] for c in look)
    return {"bull": c1[L] < lo - 1 and c0[C] > lo and bull(c0) and strong(c0, 5),
            "bear": c1[H] > hi + 1 and c0[C] < hi and bear(c0) and strong(c0, 5)}


def fvg(cs: list[Bar]) -> dict:
    if len(cs) < 3:
        return {"bull": False, "bear": False}
    a, b, c = cs[-3], cs[-2], cs[-1]
    return {"bull": a[H] < c[L] and bull(b) and strong(b, 15),
            "bear": a[L] > c[H] and bear(b) and strong(b, 15)}


def order_block(cs: list[Bar], spot: float) -> dict:
    if len(cs) < 8:
        return {"bull": False, "bear": False}
    look = cs[max(0, len(cs) - 40):]
    bu = be = False
    for i in range(len(look) - 2):
        a, b, c2 = look[i], look[i + 1], look[i + 2]
        if bear(a) and strong(a, 5) and bull(b) and strong(b, 10) and bull(c2):
            if a[L] - 30 <= spot <= a[H] + 40:
                bu = True
        if bull(a) and strong(a, 5) and bear(b) and strong(b, 10) and bear(c2):
            if a[L] - 40 <= spot <= a[H] + 30:
                be = True
    return {"bull": bu, "bear": be}


def breaker(cs: list[Bar], spot: float) -> dict:
    if len(cs) < 18:
        return {"bull": False, "bear": False}
    look = cs[max(0, len(cs) - 60):]
    bu = be = False
    for i in range(len(look) - 4):
        a, b, c2 = look[i], look[i + 1], look[i + 2]
        later = look[i + 3:]
        if bear(a) and strong(a, 5) and bull(b) and strong(b, 10) and bull(c2):
            if any(c[C] > a[H] + 5 for c in later) and a[L] - 20 <= spot <= a[H] + 25:
                bu = True
        if bull(a) and strong(a, 5) and bear(b) and strong(b, 10) and bear(c2):
            if any(c[C] < a[L] - 5 for c in later) and a[L] - 25 <= spot <= a[H] + 20:
                be = True
    return {"bull": bu, "bear": be}


def smt(cs: list[Bar]) -> dict:
    if len(cs) < 10:
        return {"bull": False, "bear": False}
    n = len(cs)
    c0, c1, c2 = cs[n - 1], cs[n - 2], cs[n - 3]
    look = cs[max(0, n - 22):n - 3]
    lo, hi = min(c[L] for c in look), max(c[H] for c in look)
    return {"bull": c1[L] < lo - 2 and bull(c0) and c0[C] > c2[H] and strong(c0, 12),
            "bear": c1[H] > hi + 2 and bear(c0) and c0[C] < c2[L] and strong(c0, 12)}


def ema_trend(cs: list[Bar]) -> dict:
    if len(cs) < 5:
        return {"bull": False, "bear": False}
    closes = [c[C] for c in cs]
    e = ema(closes, 20)
    last, last_e = closes[-1], e[-1]
    prev_e = e[-3] if len(e) >= 3 else last_e
    return {"bull": last > last_e and last_e > prev_e, "bear": last < last_e and last_e < prev_e}


def analyze(cs: list[Bar], spot: float) -> dict:
    lg, fv, ob, bb, sm, tr = liq_grab(cs), fvg(cs), order_block(cs, spot), breaker(cs, spot), smt(cs), ema_trend(cs)

    def side(k):
        concepts = [n for n, d in (("LiqGrab", lg), ("FVG", fv), ("OrdBlock", ob), ("Breaker", bb), ("SMTrap", sm)) if d[k]]
        return {"score": len(concepts), "bonus": 1 if tr[k] else 0, "concepts": concepts, "trendOk": tr[k]}
    return {"bull": side("bull"), "bear": side("bear")}


def pick_direction(sig: dict):
    b, r = sig["bull"], sig["bear"]
    be, re = b["score"] + b["bonus"], r["score"] + r["bonus"]
    if b["score"] >= 2 and be >= re:
        return "CE", b, be
    if r["score"] >= 2 and re > be:
        return "PE", r, re
    if b["score"] >= 2:
        return "CE", b, be
    if r["score"] >= 2:
        return "PE", r, re
    return None


async def run_day(day: date, s: Settings, spot_bars: list[Bar]) -> DayResult:
    expiry = await _nearest_expiry(day)
    res = DayResult(day.isoformat(), expiry)
    if not expiry:
        res.note = "no expiry"
        return res
    expired = expiry in set(await upstox.expired_expiries())
    contracts = await upstox.contracts(expiry)
    by_side: dict[str, list[dict]] = {"CE": [], "PE": []}
    for c in contracts:
        if c["instrument_type"] in by_side:
            by_side[c["instrument_type"]].append(c)
    bars_cache: dict[str, list[Bar]] = {}

    async def bars(c: dict) -> list[Bar]:
        k = c["instrument_key"]
        if k not in bars_cache:
            bars_cache[k] = _market(await day_rows(k, day, expiry, expired))
        return bars_cache[k]

    lo, hi = s.min_premium, s.max_premium
    bands = [(lo, hi), (round(lo * 0.75), round(hi * 1.75)), (round(lo * 0.5), round(hi * 2.0))]
    sweet = round((lo + hi) / 2)

    async def best_leg(direction: str, spot: float, mark: str):
        atm = round(spot / STEP) * STEP
        legs = []
        for c in by_side[direction]:
            if abs(c["strike_price"] - atm) > s.chain_strikes * STEP:
                continue
            prev = [b for b in await bars(c) if b[0] < mark]
            if prev:
                legs.append((c, prev[-1][C]))
        for blo, bhi in bands:
            cands = [(c, ltp) for c, ltp in legs if blo <= ltp <= bhi]
            if cands:
                def cmp(a, b):
                    da, db = abs(a[0]["strike_price"] - atm), abs(b[0]["strike_price"] - atm)
                    if abs(da - db) > 50:
                        return -1 if da < db else (1 if da > db else 0)
                    x, y = abs(a[1] - sweet), abs(b[1] - sweet)
                    return -1 if x < y else (1 if x > y else 0)
                cands.sort(key=cmp_to_key(cmp))
                return cands[0], (blo, bhi)
        return None, None

    def resolve(c: dict, direction: str, entry: float, mark: str, why: str, spot: float, side: dict) -> Trade:
        risk = _round2(entry * s.sl_pct / 100)
        reward = _round2(entry * s.target_pct / 100)
        sl0, tgt = _round2(entry - risk), _round2(entry + reward)
        t1 = _round2(entry * (1 + s.target1_pct / 100)) if s.target1_pct else None
        sl, moved, peak, t1_time = sl0, False, 0.0, None
        status, exit_p, exit_t = "EOD", entry, None
        all_bars = bars_cache[c["instrument_key"]]
        later = [b for b in all_bars if b[0] >= mark]
        if s.live:  # live: once-a-minute LTP checks, like updateAlertPnL on the cron (see vwap.resolve_live)
            ei = next((i for i, b in enumerate(all_bars) if b[0] >= mark), len(all_bars))
            status, exit_p, exit_t, peak, moved, t1_time = resolve_live(all_bars, ei, entry, mark, s, sl0, tgt, t1)
            later = []
        for t, _o, h, l, cl, _v in later:
            if s.time_exit_first and t >= s.time_exit:
                status, exit_p, exit_t = "TIME_EXIT", cl, t
                break
            peak = max(peak, _round2(h - entry))
            if s.breakeven_pct > 0 and not moved and peak > 0 and peak / entry * 100 >= s.breakeven_pct:
                sl, moved = entry, True
            if t1 is not None and t1_time is None and h >= t1:
                t1_time = t
            if l <= sl:
                status, exit_p, exit_t = "SL", sl, t
                break
            if h >= tgt:
                status, exit_p, exit_t = "TARGET", tgt, t
                break
        if status == "EOD" and later:
            exit_p, exit_t = later[-1][C], later[-1][0]
        pnl = _round2(exit_p - entry)
        return Trade(
            date=day.isoformat(), expiry=expiry, symbol=c["trading_symbol"], strike=c["strike_price"], type=direction,
            instrument_key=c["instrument_key"], entry_time=mark, entry=entry, sl=sl0, target=tgt,
            candle="", close=entry, vwap=0.0, other="", status=status, exit_time=exit_t, exit=exit_p, pnl=pnl,
            pnl_pct=_round2(pnl / entry * 100), peak=peak, sl_to_breakeven=moved, why=why, t1_time=t1_time,
            spot=spot, score=side["score"], concepts=list(side["concepts"]), trend_ok=bool(side["trendOk"]),
        )

    recent: list[tuple[str, float, int]] = []  # (dir, strike, minute) for the 3-min duplicate rule
    after = None
    for minute in range(_mins(s.entry_start), _mins(s.last_entry) + 1):
        if len(res.trades) >= s.max_trades:
            break
        if after is not None and minute <= after:
            continue
        mark = f"{minute // 60:02d}:{minute % 60:02d}"
        cs = [b for b in spot_bars if _add(b[0], 2) <= mark]  # what live sees: up to m-2 (see vwap.py)
        if len(cs) < 10:
            continue
        spot = cs[-1][C]
        picked = pick_direction(analyze(cs, spot))
        if not picked:
            continue
        direction, side, eff = picked
        (leg, band) = await best_leg(direction, spot, mark)
        if not leg:
            continue
        c, ltp = leg
        if any(d == direction and k == c["strike_price"] and minute - m0 < 3 for d, k, m0 in recent):
            continue
        strength = "STRONG" if eff >= 5 else "GOOD" if eff >= 3 else "MODERATE"
        why = (f"{'bull' if direction == 'CE' else 'bear'} {side['score']}/5: {'+'.join(side['concepts'])}"
               f"{' +EMA✓' if side['trendOk'] else ''} (eff {eff}, {strength}) · spot {spot:.2f} · "
               f"premium band ₹{band[0]}–{band[1]}")
        tr = resolve(c, direction, ltp, mark, why, spot, side)
        res.trades.append(tr)
        recent.append((direction, c["strike_price"], minute))
        if not (tr.pnl < 0 or tr.status == "SL"):  # SL at breakeven fills below entry live → a loss
            break
        after = _mins(tr.exit_time or mark) + s.cooldown_min
    if not res.trades:
        res.note = "no SMC confluence (≥2 concepts) with an option in the premium bands before 15:00"
    return res
