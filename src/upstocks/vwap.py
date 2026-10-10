"""VWAP 9:30 strategy backtest on Upstox data.

Defaults = the user's frontend/backend (Kite) app exactly —
backend/src/services/vwap930Service.js runHistoricalVWAP930Scan + constants.js
(frontend/lib/constants.ts shows the same 09:36 / ₹300–500 / 6% / 30%):
entry from 09:36 with checkpoints every 5 min from there (09:36, 09:41, …),
close > VWAP AND a strong green candle, candidates only ATM, ATM∓50, ATM±100,
ATM±150 (in that order), exits checked from the minute AFTER the entry
candle, no breakeven move in the backtest. Every rule is a setting; the
older IND Money variant is the other preset:

- Decisions at checkpoints every N min from the entry start (as-is, or rounded
  up to N-min boundaries for the IND Money variant).
- At a checkpoint: ATM = NIFTY spot (the index's 1-min OPEN at the checkpoint)
  rounded to 50. Per side (CE, PE), walk that day's expiry's strikes nearest-ATM
  first and take the first one whose last completed N-min close is inside the
  premium band.
- A side qualifies if that close > its VWAP (cumulative typical-price × volume
  over N-min candles from 09:15), optionally also a strong green candle (the
  original Kite backend's extra rule). Both qualify → larger close−VWAP wins.
- Entry = that close; SL −sl%, target +target%; SL moves to entry once the
  peak gain reaches breakeven%; exits checked from the entry minute's own
  candle: SL, then target, then 15:20 time exit, then stagnant exit (after N
  hours with peak < M points); otherwise EOD at the last close.
- After a LOSS, re-enter at checkpoints from exit + cooldown, up to max trades.

Candles: 1-minute, market hours only (09:15–15:29). N-min candles are grouped
by index from 09:15 (as the original), so a day with a missing minute shifts
— same behaviour as before, kept for parity.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from . import upstox

STEP = 50


@dataclass
class Settings:
    start: date
    end: date
    min_premium: float = 300
    max_premium: float = 500
    sl_pct: float = 6
    target_pct: float = 30
    entry_start: str = "09:36"
    candle_minutes: int = 5
    breakeven_pct: float = 0          # 0 = off (the Kite backtest has none; live moved SL at +10%)
    max_trades: int = 2
    cooldown_min: int = 15
    stagnant_hours: float = 3
    stagnant_points: float = 30
    time_exit: str = "15:20"
    strong_green: bool = True
    atm_offsets_only: bool = True     # Kite: only ATM, ∓50, ±100, ±150; else every strike nearest-ATM first
    align_checkpoints: bool = False   # Kite: 09:36, 09:41…; IND Money: rounded up to 5-min boundaries
    include_entry_candle: bool = False  # Kite: exits checked from the minute AFTER entry
    # Live simulation (frontend app's runVWAP930Scan + doScan, cron every minute):
    # scan EVERY minute; per side pick the in-band strike nearest ATM by its live
    # price (last 1-min close) from the chain window (±15 strikes); qualify on its
    # latest COMPLETED N-min candle; enter at that live price; 15:20 checked first.
    live: bool = False
    chain_strikes: int = 15
    time_exit_first: bool = False
    # Which engine runs the day: "vwap" (this module) or "smc" (smc.py).
    strategy: str = "vwap"
    last_entry: str = "15:29"          # SMC: no new entries from 15:00 → 14:59
    target1_pct: float = 0             # SMC: +12% milestone (recorded, not an exit)


PRESETS = {
    # frontend app LIVE behaviour (backend routes/vwap930.js doScan → runVWAP930Scan)
    "frontend_live": {"live": True, "atm_offsets_only": False, "include_entry_candle": True,
                      "breakeven_pct": 10, "time_exit_first": True},
    # frontend app SMC live (backend services/smcService.js runSMCScan + routes/smc.js doScan)
    "smc_live": {"strategy": "smc", "live": True, "entry_start": "09:21", "last_entry": "14:59",
                 "sl_pct": 6, "target_pct": 24, "target1_pct": 12, "breakeven_pct": 10,
                 "include_entry_candle": True, "time_exit_first": True, "strong_green": False,
                 "atm_offsets_only": False, "stagnant_hours": 0},
}
KITE_OFFSETS = [0, -50, 50, -100, 100, -150, 150]


# Candles here: (hhmm, open, high, low, close, volume) for one day.
Bar = tuple


def _market(rows: list[list]) -> list[Bar]:
    out = []
    for r in rows:
        t = r[0][11:16]
        if "09:15" <= t <= "15:29":
            out.append((t, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0)))
    return out


def _add(hhmm: str, minutes: int) -> str:
    h, m = map(int, hhmm.split(":"))
    t = h * 60 + m + minutes
    return f"{t // 60:02d}:{t % 60:02d}"


def _mins(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def _round2(x: float) -> float:
    # JS +x.toFixed(2): half away from zero on the decimal value.
    from decimal import Decimal, ROUND_HALF_UP
    return float(Decimal(repr(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def aggregate(bars: list[Bar], size: int) -> list[Bar]:
    """Index-based groups of `size` from the first bar; drops a trailing partial.
    Each group's time is its LAST 1-min bar's time (as the original)."""
    out = []
    for i in range(0, len(bars) - size + 1, size):
        ch = bars[i:i + size]
        out.append((ch[-1][0], ch[0][1], max(c[2] for c in ch), min(c[3] for c in ch), ch[-1][4], sum(c[5] for c in ch)))
    return out


def vwap_series(bars: list[Bar]) -> list[float]:
    pv = vol = 0.0
    out = []
    for _, _o, h, l, c, v in bars:
        typ = (h + l + c) / 3
        pv += typ * v
        vol += v
        out.append(_round2(pv / vol if vol > 0 else typ))
    return out


def strong_green(c: Bar) -> bool:
    body = c[4] - c[1]
    if body <= 0:
        return False
    upper = c[2] - max(c[1], c[4])
    lower = min(c[1], c[4]) - c[3]
    return body > upper + lower


@dataclass
class Series:
    key: str
    bars: list[Bar]
    nm: list[Bar]
    vwap: list[float]


@dataclass
class Trade:
    date: str
    expiry: str
    symbol: str
    strike: float
    type: str
    instrument_key: str
    entry_time: str
    entry: float
    sl: float
    target: float
    candle: str
    close: float
    vwap: float
    other: str
    status: str = "EOD"
    exit_time: str | None = None
    exit: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    peak: float = 0.0
    sl_to_breakeven: bool = False
    why: str = ""                      # SMC: concepts/score; VWAP builds its text from candle/close/vwap
    t1_time: str | None = None         # SMC: when +target1% was first touched
    spot: float = 0.0                  # NIFTY spot used for ATM at entry
    score: int = 0                     # SMC: raw concept count (of 5)
    concepts: list = field(default_factory=list)  # SMC concepts
    trend_ok: bool = False             # SMC: EMA-20 trend aligned


@dataclass
class DayResult:
    date: str
    expiry: str | None
    trades: list[Trade] = field(default_factory=list)
    note: str = ""


async def day_rows(instrument_key: str, day: date, expiry: str, expired: bool) -> list[list]:
    """One day's 1-min candles for an option, fetched as part of ONE span call
    per contract (expiry−9 days → expiry): the engines visit a weekly contract
    on each of the ~5 days it's the nearest expiry, and Upstox counts calls,
    not candles. The span is cached (on disk once it's 2+ days old)."""
    exp = date.fromisoformat(expiry)
    span_end = min(exp, upstox.today_ist())
    span_start = min(day, exp - timedelta(days=9))
    rows = await upstox.candles(instrument_key, "1m", span_start, span_end, expired)
    d = day.isoformat()
    return [r for r in rows if r[0][:10] == d]


async def _nearest_expiry(day: date) -> str | None:
    for e in sorted(x["date"] for x in await upstox.expiries()):
        if e >= day.isoformat():
            return e
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
    cm = s.candle_minutes
    cache: dict[str, Series | None] = {}

    async def series(c: dict) -> Series | None:
        k = c["instrument_key"]
        if k not in cache:
            rows = await day_rows(k, day, expiry, expired)
            bars = _market(rows)
            if len(bars) >= cm * 2:
                nm = aggregate(bars, cm)
                cache[k] = Series(k, bars, nm, vwap_series(nm))
            else:
                cache[k] = None
        return cache[k]

    def bar_at_or_after(sr: Series, mark: str) -> int:
        return next((i for i, b in enumerate(sr.bars) if b[0] >= mark), -1)

    async def live_candidate(side: str, atm: float, mark: str):
        """Live: nearest-ATM strike whose LIVE price (last 1-min close before
        `mark`) is in band — chosen before looking at VWAP, exactly like
        findCandidateLegs — then its latest completed N-min candle."""
        window = [c for c in by_side[side] if abs(c["strike_price"] - atm) <= s.chain_strikes * STEP]
        for c in sorted(window, key=lambda c: abs(c["strike_price"] - atm)):
            sr = await series(c)
            if not sr:
                continue
            prev = [b for b in sr.bars if b[0] < mark]
            if not prev:
                continue
            ltp = prev[-1][4]
            if not (s.min_premium <= ltp <= s.max_premium):
                continue
            done = [i for i, b in enumerate(sr.nm) if _add(b[0], 1) <= mark]
            if not done:
                return None  # in-band leg has no completed candle yet → this side can't qualify
            idx = done[-1]
            entry_idx = bar_at_or_after(sr, mark)
            if entry_idx < 0:
                return None
            return {"c": c, "sr": sr, "premium": ltp, "close": sr.nm[idx][4], "vwap": sr.vwap[idx],
                    "candle": sr.nm[idx], "entry_idx": entry_idx}
        return None

    async def candidate(side: str, atm: float, mark: str):
        if s.live:
            return await live_candidate(side, atm, mark)
        if s.atm_offsets_only:
            by_k = {c["strike_price"]: c for c in by_side[side]}
            order = [by_k[atm + o] for o in KITE_OFFSETS if (atm + o) in by_k]
        else:
            order = sorted(by_side[side], key=lambda c: abs(c["strike_price"] - atm))
        for c in order:
            sr = await series(c)
            if not sr:
                continue
            idx = -1
            for i, b in enumerate(sr.nm):
                if b[0] <= mark:
                    idx = i
                else:
                    break
            if idx < 0:
                continue
            last = sr.nm[idx]
            # The N-min candle "closes" one minute after its last 1-min bar starts.
            if _add(last[0], 1) > mark:
                continue
            premium = last[4]
            if not (s.min_premium <= premium <= s.max_premium):
                continue
            entry_idx = next((i for i, b in enumerate(sr.bars) if b[0] > last[0]), -1)
            if entry_idx < 0:
                continue
            return {"c": c, "sr": sr, "premium": premium, "close": premium, "vwap": sr.vwap[idx], "candle": last, "entry_idx": entry_idx}
        return None

    # Checkpoints: candle closes from the entry start (rounded up) to 15:30.
    first = _mins(s.entry_start)
    if s.align_checkpoints:
        first = -(-first // cm) * cm
    step = 1 if s.live else cm  # live cron scans every minute
    last_mark = _mins("15:29") if s.live else _mins("15:30")
    marks = [f"{t // 60:02d}:{t % 60:02d}" for t in range(first, last_mark + 1, step)]

    async def try_mark(mark: str):
        if s.live:  # live chain spot = index LTP at scan time (last 1-min close)
            prev = [b for b in spot_bars if b[0] < mark]
            if not prev:
                return None
            spot = prev[-1][4]
        else:       # backtest: the index's 1-min open at the checkpoint
            spot_bar = next((b for b in spot_bars if b[0] >= mark), None)
            if not spot_bar:
                return None
            spot = spot_bar[1]
        atm = round(spot / STEP) * STEP
        ce, pe = await candidate("CE", atm, mark), await candidate("PE", atm, mark)

        def ok(x):
            return x and x["close"] > x["vwap"] and (not s.strong_green or strong_green(x["candle"]))
        ce_ok, pe_ok = ok(ce), ok(pe)
        if not ce_ok and not pe_ok:
            return None
        if ce_ok and pe_ok:  # both → larger distance above VWAP; a tie goes to CE
            side, pick = ("CE", ce) if ce["close"] - ce["vwap"] >= pe["close"] - pe["vwap"] else ("PE", pe)
        else:
            side, pick = ("CE", ce) if ce_ok else ("PE", pe)
        other_side, other = ("PE", pe) if side == "CE" else ("CE", ce)
        if other:
            green = "" if not s.strong_green or strong_green(other["candle"]) else " (not a strong green candle)"
            o = f"{other_side} {other['c']['strike_price']:g} close {other['close']:.2f} vs VWAP {other['vwap']:.2f}{green}"
        else:
            o = f"no {other_side} in band"
        return side, pick, mark, o, spot

    def resolve(side: str, pick: dict, mark: str, other: str, spot: float = 0.0) -> Trade:
        c, sr = pick["c"], pick["sr"]
        entry = pick["premium"]
        risk = _round2(entry * s.sl_pct / 100)
        reward = _round2(entry * s.target_pct / 100)
        sl0, tgt = _round2(entry - risk), _round2(entry + reward)
        sl, moved, peak = sl0, False, 0.0
        status, exit_p, exit_t = "EOD", entry, None
        cutoff = _mins(mark) + s.stagnant_hours * 60
        start_i = pick["entry_idx"] if s.include_entry_candle else pick["entry_idx"] + 1
        later = sr.bars[start_i:]
        for b in later:
            t, _o, h, l, cl, _v = b
            if s.time_exit_first and t >= s.time_exit:  # live: 15:20 is checked before price
                status, exit_p, exit_t = "TIME_EXIT", cl, t
                break
            peak = max(peak, _round2(h - entry))
            if s.breakeven_pct > 0 and not moved and peak > 0 and peak / entry * 100 >= s.breakeven_pct:
                sl, moved = entry, True
            if l <= sl:
                status, exit_p, exit_t = "SL", sl, t
                break
            if h >= tgt:
                status, exit_p, exit_t = "TARGET", tgt, t
                break
            if t >= s.time_exit:
                status, exit_p, exit_t = "TIME_EXIT", cl, t
                break
            if s.stagnant_hours > 0 and _mins(t) >= cutoff and peak < s.stagnant_points:
                status, exit_p, exit_t = "STAGNANT_EXIT", cl, t
                break
        if status == "EOD" and later:
            exit_p, exit_t = later[-1][4], later[-1][0]
        pnl = _round2(exit_p - entry)
        return Trade(
            date=day.isoformat(), expiry=expiry, symbol=c["trading_symbol"], strike=c["strike_price"], type=side,
            instrument_key=c["instrument_key"], entry_time=mark, entry=entry, sl=sl0, target=tgt,
            candle=f"{_add(pick['candle'][0], 1 - cm)}–{_add(pick['candle'][0], 1)}", close=pick["close"],
            vwap=pick["vwap"], other=other, status=status, exit_time=exit_t, exit=exit_p, pnl=pnl,
            pnl_pct=_round2(pnl / entry * 100), peak=peak, sl_to_breakeven=moved, spot=spot,
        )

    after = None
    while len(res.trades) < s.max_trades:
        picked = None
        for m in marks:
            if after is not None and _mins(m) <= after:
                continue
            picked = await try_mark(m)
            if picked:
                break
        if not picked:
            break
        tr = resolve(*picked)
        res.trades.append(tr)
        if not tr.pnl < 0:
            break
        after = _mins(tr.exit_time or tr.entry_time) + s.cooldown_min
    if not res.trades:
        res.note = (f"no CE/PE in ₹{s.min_premium:g}–{s.max_premium:g} with a "
                    f"{'strong green ' if s.strong_green else ''}{cm}-min candle closed above VWAP after {s.entry_start}")
    return res


async def trading_days_and_spot(start: date, end: date) -> dict[str, list[Bar]]:
    """NIFTY index 1-min bars per trading day in [start, end]."""
    rows = await upstox.candles(upstox.NIFTY, "1m", start, end, expired=False)
    days: dict[str, list[Bar]] = {}
    for r in rows:
        t = r[0][11:16]
        if "09:15" <= t <= "15:29":
            days.setdefault(r[0][:10], []).append((t, float(r[1]), float(r[2]), float(r[3]), float(r[4]), 0.0))
    return dict(sorted(days.items()))


def trade_dict(t: Trade) -> dict:
    return asdict(t)
