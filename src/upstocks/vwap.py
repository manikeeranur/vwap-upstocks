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
    # Live SL check: True = on the candle LOW (VWAP's exchange SL-M order fires
    # inside the minute), False = once a minute on the close (SMC's cron).
    sl_on_low: bool = True
    # Which engine runs the day: "vwap" (this module) or "smc" (smc.py).
    strategy: str = "vwap"
    last_entry: str = "15:29"          # SMC: no new entries from 15:00 → 14:59
    target1_pct: float = 0             # SMC: +12% milestone (recorded, not an exit)
    # EMA25 + VWAP: on top of the VWAP rules, the signal N-min candle must be
    # where the EMA of N-min closes crosses above VWAP (previous candle EMA ≤
    # VWAP, this one EMA > VWAP). EMA warmed up on the previous session.
    ema_cross: bool = False
    ema_period: int = 25
    # Sideways filter: skip the signal if the entry price was already visited
    # this many times earlier in the day (separate runs of 1-min candles whose
    # low–high range holds it, before the signal candle). 0 = off.
    max_entry_visits: int = 0
    # Live strike pick: False = only the nearest-ATM in-band strike is checked
    # (the frontend app); True = every in-band strike, nearest-ATM first, and
    # the first that meets the entry rules is taken.
    any_strike_in_band: bool = False
    # SuperTrend (TradingView ta.supertrend on N-min candles, warmed up on the
    # previous session): the signal candle must close in an uptrend (green).
    supertrend: bool = False
    st_period: int = 10
    st_mult: float = 3.0
    # NIFTY trend: the index's 1-min SuperTrend (st_period, st_mult; warmed up on
    # the previous session) on its last completed candle — green → CE only,
    # red → PE only.
    index_trend: bool = False
    # …and only once that colour has held for this many completed candles in a
    # row (a 1–2 minute flip is noise: 7 Oct 10:02–10:03 flipped red for 2 min
    # inside a rally and a PE was taken).
    index_trend_bars: int = 1
    # Trend entry (replaces the VWAP rules): the signal candle is where "EMA
    # rising (EMA > previous EMA) AND SuperTrend green" first becomes true —
    # the previous candle didn't have both. VWAP isn't checked.
    trend_entry: bool = False
    # EMA9/26 pullback (ema_pullback.py): option confirmation over 5 min, the
    # reversal exit's activation gain, and ₹ slippage on each fill.
    ep_prem_rise: float = 2.0
    ep_oi_rise: float = 0.5
    ep_rev_activate: float = 20.0
    # SMC: the EMA9/26 pullback's reversal exit (price up / OI down at a 5-min
    # candle's peak minute, volume ≥ 1.5× median, upper wick ≥ 40%, fresh 30-min
    # high) once the gain reached ep_rev_activate %; exit at the next minute's open.
    rev_exit: bool = False
    # Harmonic (harmonic.py): Fib level, zigzag swing %, touch tolerance (fraction of
    # the leg), strike points in the money, trailing stop, expiry-day skip.
    harm_fib: float = 0.58
    harm_zz: float = 0.25
    harm_tol: float = 0.06
    harm_itm: float = 400
    harm_skip_expiry: bool = True
    harm_min_leg: float = 0             # skip the day when the signal's leg is smaller (NIFTY points); 0 = any
    trail_start: float = 0              # 0 = no trailing stop
    trail_giveback: float = 40
    slippage: float = 0.0


PRESETS = {
    # frontend app LIVE behaviour (backend routes/vwap930.js doScan → runVWAP930Scan)
    # Breakeven 15%: reproduces the October live trades (6 Oct went to TARGET
    # with a +13.5% high; at 10% it would have been stopped at entry). The live
    # Account-tab setting reads 10% now — saved 9 Oct 18:11, after those trades.
    "frontend_live": {"live": True, "atm_offsets_only": False, "include_entry_candle": True,
                      "breakeven_pct": 15, "time_exit_first": True},
    # frontend app SMC live (backend services/smcService.js runSMCScan + routes/smc.js doScan)
    # Premium band ₹300–400 (sweet spot ₹350): reproduces the live SMC strikes
    # exactly up to 7 Oct; after that the live Account-tab band was set to
    # ₹200–300 — type that in to match those days.
    "smc_live": {"strategy": "smc", "live": True, "entry_start": "09:21", "last_entry": "14:59",
                 "min_premium": 300, "max_premium": 400,
                 "sl_pct": 6, "target_pct": 24, "target1_pct": 12, "breakeven_pct": 15,
                 "include_entry_candle": True, "time_exit_first": True, "strong_green": False,
                 "atm_offsets_only": False, "stagnant_hours": 0, "sl_on_low": False},
}
# EMA25 + VWAP: the VWAP live rules on 1-min candles, plus EMA25 crossing above
# VWAP on the signal candle (the 1-min lines on the trade chart), entry the
# minute that candle closes (e.g. the 09:40 candle → 09:41). No strong-green
# rule: the cross itself is the signal (it blocked 9 Oct's 22150 CE cross).
# (trend_entry was tried instead of the cross on 1–10 Oct: 3 wins / 7 losses,
# +107.91 pts vs 4 / 3, +399.21 — kept as an option, not the preset.)
PRESETS["ema25_vwap"] = {**PRESETS["frontend_live"], "ema_cross": True, "candle_minutes": 1,
                         "strong_green": False, "max_entry_visits": 4, "any_strike_in_band": True,
                         "min_premium": 280,  # a cross at ₹294 (8 Oct 22750 PE 09:39) is still taken
                         "supertrend": True, "stagnant_hours": 0, "index_trend": True}
# Mani's EMA9/EMA26 pullback (ema_pullback.py): ₹150–<300 closest to ₹225,
# SL 25%, target 100%, one trade a day, ₹1 slippage each side.
PRESETS["ema_pullback"] = {"strategy": "ema_pullback", "min_premium": 150, "max_premium": 300,
                           "sl_pct": 25, "target_pct": 100, "entry_start": "09:36", "last_entry": "14:59",
                           "max_trades": 1, "time_exit": "15:20", "breakeven_pct": 0, "stagnant_hours": 0,
                           "strong_green": False, "candle_minutes": 1, "slippage": 1.0}
# SMC Tuned: the same SMC engine, tuned 2026-10-13 for ONE trade a day on 1 Apr – 30 Sep
# 2026 (exactly 6 months), plus the EMA9/26 reversal exit: net ₹1,56,361 for 1 lot after
# charges, 98 trades, max drawdown ₹35,028. (The 2-trades-a-day version — SL 33%,
# breakeven 20%, 09:28 start, 15:25 exit, max 2, cooldown 30 — made ₹1,66,456 on that
# window.) The plain "smc_live" preset above keeps the original live values.
PRESETS["smc_tuned"] = {"strategy": "smc", "live": False, "entry_start": "09:32", "last_entry": "12:00",
                        "min_premium": 100, "max_premium": 800,
                        "sl_pct": 50, "target_pct": 120, "target1_pct": 12, "breakeven_pct": 25,
                        "rev_exit": True, "ep_rev_activate": 35,
                        "max_trades": 1, "cooldown_min": 30, "chain_strikes": 20, "time_exit": "15:15",
                        "include_entry_candle": False, "time_exit_first": True, "strong_green": False,
                        "atm_offsets_only": False, "stagnant_hours": 0, "sl_on_low": False}
# Harmonic (harmonic.py) — "B-Pro": 0.58 Fib pullback on NIFTY 1-min swings, ITM-400,
# one trade a day 09:30–11:15, no expiry days, safety SL 50% (never hit in the tuning
# year — it's there for crash days), target 150%, trailing after
# +40% (give back 40%), reversal exit after +30%, 15:00 exit. Tuned on 1 Oct 2025 – 30 Sep
# 2026: net ₹3,10,815 for 1 lot after charges, 117 trades, every month profitable.
PRESETS["harmonic"] = {"strategy": "harmonic", "harm_fib": 0.58, "harm_zz": 0.25, "harm_tol": 0.06,
                       "harm_itm": 400, "harm_skip_expiry": True, "entry_start": "09:30", "last_entry": "11:15",
                       "sl_pct": 50, "target_pct": 150, "trail_start": 40, "trail_giveback": 40,
                       "rev_exit": True, "ep_rev_activate": 30, "time_exit": "15:00", "max_trades": 1,
                       "live": False, "strong_green": False, "stagnant_hours": 0, "breakeven_pct": 0}
# Harmonic Optimized — the same engine, one trade a day, chosen 2026-10-29 to be profitable in BOTH Oct 2024 –
# Sep 2025 and Oct 2025 – Sep 2026 (the original preset above lost ₹68,211 in Oct 2024 – Sep 2025).
# 0.20 % swings, 0.60 Fib pullback within 4 % of the leg, signals 09:45–14:30, ITM-400, no expiry days;
# SL 20 %, target 150 %, trailing after +40 % (give back 30 %), reversal exit after +30 %, 15:20 exit.
# 1 lot (65), after charges: +₹1,21,871 and +₹1,57,995 (₹2,79,866 over the two years), max drawdown
# ₹44,858 (exits at 15:00 / 15:15 give almost the same). No weekday or signal hour lost in both years.
# Skipping days whose leg is under 60 NIFTY points (small swings): +₹1,22,861 and +₹1,87,671, max drawdown
# ₹37,082 (50–70 points all improve; a previous-day-range filter only worked at one exact value, so it isn't used). Neighbouring settings were profitable in both years too. Tried and not better: deeper ITM
# (500–800), trading expiry days, EMA-trend / FVG / strong-candle filters.
PRESETS["harmonic_2y"] = {**PRESETS["harmonic"], "harm_fib": 0.60, "harm_zz": 0.20, "harm_tol": 0.04,
                          "entry_start": "09:45", "last_entry": "14:30",
                          "sl_pct": 20, "target_pct": 150, "trail_start": 40, "trail_giveback": 30,
                          "rev_exit": True, "ep_rev_activate": 30, "time_exit": "15:20", "max_trades": 1,
                          "harm_min_leg": 60}
# SMC + EMA9/26 pullback as one strategy (combo.py): one trade a day — whichever signals first.
PRESETS["smc_ema_combo"] = {"strategy": "combo"}
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


def ema_series(closes: list[float], n: int) -> list[float | None]:
    """EMA seeded with the SMA of the first n closes (as TradingView); None before that."""
    k, out, ema, total = 2 / (n + 1), [], None, 0.0
    for i, c in enumerate(closes):
        if i < n:
            total += c
            ema = total / n if i == n - 1 else None
        else:
            ema = c * k + ema * (1 - k)
        out.append(ema)
    return out


def ema_crossed(sr: "Series", idx: int) -> bool:
    """EMA crosses above VWAP on N-min candle idx (VWAP restarts daily, so not on the first)."""
    if idx < 1 or sr.ema[idx] is None or sr.ema[idx - 1] is None:
        return False
    return _round2(sr.ema[idx - 1]) <= sr.vwap[idx - 1] and _round2(sr.ema[idx]) > sr.vwap[idx]


def supertrend_series(bars: list[Bar], n: int = 10, mult: float = 3.0) -> list[tuple[float, bool] | None]:
    """TradingView ta.supertrend(mult, n) on hl2 with Wilder's ATR: (line, uptrend)
    per bar; None until the ATR has n bars."""
    out, atr, trs = [], None, []
    up_b = dn_b = st = None
    prev_c = None
    for _t, _o, h, l, c, _v in bars:
        tr = h - l if prev_c is None else max(h - l, abs(h - prev_c), abs(l - prev_c))
        if atr is None:
            trs.append(tr)
            if len(trs) == n:
                atr = sum(trs) / n
        else:
            atr = (atr * (n - 1) + tr) / n
        if atr is None:
            out.append(None)
            prev_c = c
            continue
        hl2 = (h + l) / 2
        lo, hi = hl2 - mult * atr, hl2 + mult * atr
        if up_b is not None:
            lo = lo if lo > up_b or prev_c < up_b else up_b
            hi = hi if hi < dn_b or prev_c > dn_b else dn_b
        if st is None:
            uptrend = False                       # Pine: first bar with an ATR is a downtrend
        elif st == dn_b:
            uptrend = c > hi
        else:
            uptrend = c >= lo
        up_b, dn_b = lo, hi
        st = lo if uptrend else hi
        out.append((st, uptrend))
        prev_c = c
    return out


def trend_started(sr: "Series", idx: int) -> bool:
    """EMA rising + SuperTrend green on N-min candle idx, but not both on idx-1."""
    def up(i):
        return (i >= 1 and sr.ema[i] is not None and sr.ema[i - 1] is not None
                and sr.ema[i] > sr.ema[i - 1] and bool(sr.st[i] and sr.st[i][1]))
    return up(idx) and not up(idx - 1)


def entry_visits(bars: list[Bar], price: float, before: str) -> int:
    """Separate runs of 1-min candles (before `before`) whose low–high holds `price`."""
    n, prev = 0, False
    for b in bars:
        if b[0] >= before:
            break
        hit = b[3] <= price <= b[2]
        n += hit and not prev
        prev = hit
    return n


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
    ema: list[float | None] = field(default_factory=list)  # aligned with nm, previous session warm-up
    st: list = field(default_factory=list)                   # SuperTrend (line, uptrend) aligned with nm


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
    ema: float = 0.0                   # EMA25 + VWAP: EMA at the signal candle
    st: float = 0.0                    # SuperTrend line at the signal candle
    nifty_trend: str = ""              # index_trend: "UP" / "DOWN" (NIFTY SuperTrend at entry)


@dataclass
class DayResult:
    date: str
    expiry: str | None
    trades: list[Trade] = field(default_factory=list)
    note: str = ""


def resolve_live(bars: list[Bar], entry_i: int, entry: float, mark: str, s: Settings,
                 sl0: float, tgt: float, t1: float | None = None):
    """Exit the way the live app does with auto-trade on (verified on the
    October live trades):
    - SL is a real SL-M order resting at the exchange, so it fires on the first
      touch INSIDE a minute — the candle's LOW (1 Oct 09:45, 8 Oct 10:13).
    - Breakeven, target, 15:20 and stagnant are only checked by the cron once a
      minute on the LTP = the previous candle's CLOSE (6 Oct: a high of 352.50
      vs the 352.11 breakeven level never moved the SL, and the target was
      taken at the 12:44 close, not on the 12:38 wick). Breakeven then moves
      the SL-M order to entry for the following minutes.
    Returns (status, exit_price, exit_minute, peak, moved, t1_time)."""
    sl, moved, peak, t1_time = sl0, False, 0.0, None
    cutoff = _mins(mark) + s.stagnant_hours * 60
    for b in bars[entry_i:]:
        t, o, h, l, c, _v = b
        # exchange SL-M during this minute (a gap below SL fills at the open)
        if s.sl_on_low and l <= sl:
            return "SL", min(sl, o), t, peak, moved, t1_time
        now = _add(t, 1)              # the cron minute that sees this candle's close
        if now >= s.time_exit:
            return "TIME_EXIT", c, now, peak, moved, t1_time
        peak = max(peak, _round2(c - entry))
        if s.breakeven_pct > 0 and not moved and peak > 0 and peak / entry * 100 >= s.breakeven_pct:
            sl, moved = entry, True
        if t1 is not None and t1_time is None and c >= t1:
            t1_time = now
        if not s.sl_on_low and c <= sl:   # SMC: cron sees the close at/below SL
            return "SL", c, now, peak, moved, t1_time
        if c >= tgt:
            return "TARGET", c, now, peak, moved, t1_time
        if s.stagnant_hours > 0 and _mins(now) >= cutoff and peak < s.stagnant_points:
            return "STAGNANT_EXIT", c, now, peak, moved, t1_time
    last = bars[-1] if bars else None
    return "EOD", (last[4] if last else entry), (last[0] if last else mark), peak, moved, t1_time


async def day_rows(instrument_key: str, day: date, expiry: str, expired: bool) -> list[list]:
    """One day's 1-min candles for an option, fetched as part of ONE span call
    per contract (expiry−9 days → expiry): the engines visit a weekly contract
    on each of the ~5 days it's the nearest expiry, and Upstox counts calls,
    not candles. The span is cached (on disk once it's 2+ days old)."""
    d = day.isoformat()
    return [r for r in await span_rows(instrument_key, day, expiry, expired) if r[0][:10] == d]


async def span_rows(instrument_key: str, day: date, expiry: str, expired: bool) -> list[list]:
    """The whole cached span `day_rows` slices from (all its days' 1-min rows)."""
    exp = date.fromisoformat(expiry)
    span_end = min(exp, upstox.today_ist())
    span_start = min(day, exp - timedelta(days=9))
    return await upstox.candles(instrument_key, "1m", span_start, span_end, expired)


async def _nearest_expiry(day: date) -> str | None:
    for e in sorted(x["date"] for x in await upstox.expiries()):
        if e >= day.isoformat():
            return e
    return None


async def run_day(day: date, s: Settings, spot_bars: list[Bar], prev_spot: list[Bar] | None = None) -> DayResult:
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
    prev_spot = prev_spot or []
    idx_st = supertrend_series(prev_spot + spot_bars, s.st_period, s.st_mult)[len(prev_spot):] if s.index_trend else []

    async def series(c: dict) -> Series | None:
        k = c["instrument_key"]
        if k not in cache:
            span = await span_rows(k, day, expiry, expired)
            d = day.isoformat()
            bars = _market([r for r in span if r[0][:10] == d])
            if len(bars) >= cm * 2:
                nm = aggregate(bars, cm)
                ema, st = [], []
                if s.ema_cross or s.supertrend or s.trend_entry:
                    prev_day = max((r[0][:10] for r in span if r[0][:10] < d), default=None)
                    prev = aggregate(_market([r for r in span if r[0][:10] == prev_day]), cm) if prev_day else []
                    if s.ema_cross or s.trend_entry:
                        ema = ema_series([b[4] for b in prev + nm], s.ema_period)[len(prev):]
                    if s.supertrend or s.trend_entry:
                        st = supertrend_series(prev + nm, s.st_period, s.st_mult)[len(prev):]
                cache[k] = Series(k, bars, nm, vwap_series(nm), ema, st)
            else:
                cache[k] = None
        return cache[k]

    def bar_at_or_after(sr: Series, mark: str) -> int:
        return next((i for i, b in enumerate(sr.bars) if b[0] >= mark), -1)

    def sideways(x):
        return s.max_entry_visits > 0 and entry_visits(
            x["sr"].bars, x["premium"], _add(x["candle"][0], 1 - cm)) >= s.max_entry_visits

    def st_up(x):
        v = x["sr"].st[x["idx"]]
        return bool(v and v[1])

    def ok(x):
        if x and s.trend_entry:
            return trend_started(x["sr"], x["idx"]) and not sideways(x)
        return bool(x and x["close"] > x["vwap"] and (not s.strong_green or strong_green(x["candle"]))
                    and (not s.ema_cross or ema_crossed(x["sr"], x["idx"])) and not sideways(x)
                    and (not s.supertrend or st_up(x)))

    async def live_candidate(side: str, atm: float, mark: str):
        """Live: nearest-ATM strike whose LIVE price (last 1-min close before
        `mark`) is in band — chosen before looking at VWAP, exactly like
        findCandidateLegs — then its latest completed N-min candle."""
        window = [c for c in by_side[side] if abs(c["strike_price"] - atm) <= s.chain_strikes * STEP]
        first = None  # any_strike_in_band: the nearest in-band leg, shown when none qualifies
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
            # Live reads Kite's 1-min history at hh:mm:00 and drops the last
            # candle as "forming" — but the newest one returned then is the
            # (complete) m-1 candle, so only candles up to m-2 are used. That's
            # why live entries land at 09:36, 09:41, 09:46 … (verified against
            # the live alerts in Mongo).
            # EMA25 + VWAP isn't bound by that quirk: it acts on the candle
            # the minute it closes.
            lag = 1 if s.ema_cross or s.trend_entry else 2
            done = [i for i, b in enumerate(sr.nm) if _add(b[0], lag) <= mark]
            if not done:
                if s.any_strike_in_band:
                    continue
                return None  # in-band leg has no completed candle yet → this side can't qualify
            idx = done[-1]
            entry_idx = bar_at_or_after(sr, mark)
            if entry_idx < 0:
                if s.any_strike_in_band:
                    continue
                return None
            x = {"c": c, "sr": sr, "premium": ltp, "close": sr.nm[idx][4], "vwap": sr.vwap[idx],
                 "candle": sr.nm[idx], "entry_idx": entry_idx, "idx": idx}
            if not s.any_strike_in_band or ok(x):
                return x
            first = first or x
        return first

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
            return {"c": c, "sr": sr, "premium": premium, "close": premium, "vwap": sr.vwap[idx], "candle": last,
                    "entry_idx": entry_idx, "idx": idx}
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
        trend = ""
        if s.index_trend:  # NIFTY's last completed 1-min candle (same lag as the option's)
            lag = 1 if s.ema_cross or s.trend_entry else 2
            done = [i for i, b in enumerate(spot_bars) if _add(b[0], lag) <= mark]
            n = s.index_trend_bars
            last = [idx_st[i] for i in done[-n:]] if len(done) >= n else []
            if not last or not all(last) or len({v[1] for v in last}) > 1:
                return None  # not warmed up, or the colour hasn't held n candles
            trend = "UP" if last[-1][1] else "DOWN"
        ce, pe = await candidate("CE", atm, mark), await candidate("PE", atm, mark)

        ce_ok, pe_ok = ok(ce) and trend != "DOWN", ok(pe) and trend != "UP"
        if not ce_ok and not pe_ok:
            return None
        if ce_ok and pe_ok:  # both → larger distance above VWAP; a tie goes to CE
            side, pick = ("CE", ce) if ce["close"] - ce["vwap"] >= pe["close"] - pe["vwap"] else ("PE", pe)
        else:
            side, pick = ("CE", ce) if ce_ok else ("PE", pe)
        other_side, other = ("PE", pe) if side == "CE" else ("CE", ce)
        if other:
            green = "" if not s.strong_green or strong_green(other["candle"]) else " (not a strong green candle)"
            if s.ema_cross and not ema_crossed(other["sr"], other["idx"]):
                green += f" (EMA{s.ema_period} not crossing VWAP)"
            if s.trend_entry and not trend_started(other["sr"], other["idx"]):
                green += f" (EMA{s.ema_period} rising + SuperTrend green didn't just start)"
            if s.supertrend and not st_up(other):
                green += " (SuperTrend not green)"
            if sideways(other):
                green += f" (entry price hit {s.max_entry_visits}+ times earlier)"
            o = f"{other_side} {other['c']['strike_price']:g} close {other['close']:.2f} vs VWAP {other['vwap']:.2f}{green}"
        else:
            o = f"no {other_side} in band"
        if trend:
            o += f" · NIFTY SuperTrend {trend}"
        return side, pick, mark, o, spot, trend

    def resolve(side: str, pick: dict, mark: str, other: str, spot: float = 0.0, trend: str = "") -> Trade:
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
        if s.live:
            status, exit_p, exit_t, peak, moved, _t1 = resolve_live(sr.bars, pick["entry_idx"], entry, mark, s, sl0, tgt)
            later = []
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
            ema=_round2(sr.ema[pick["idx"]]) if sr.ema and sr.ema[pick["idx"]] is not None else 0.0,
            st=_round2(sr.st[pick["idx"]][0]) if sr.st and sr.st[pick["idx"]] else 0.0, nifty_trend=trend,
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
        # Live re-enters only after a loss; an SL hit at breakeven fills a bit
        # below entry live (e.g. −6.50, −1.35), so it counts as a loss too.
        if not (tr.pnl < 0 or tr.status == "SL"):
            break
        after = _mins(tr.exit_time or tr.entry_time) + s.cooldown_min
    if not res.trades and s.trend_entry:
        res.note = (f"no CE/PE in ₹{s.min_premium:g}–{s.max_premium:g} where EMA{s.ema_period} rising + "
                    f"SuperTrend green started after {s.entry_start}"
                    f"{' on the NIFTY SuperTrend side' if s.index_trend else ''}")
    elif not res.trades:
        res.note = (f"no CE/PE in ₹{s.min_premium:g}–{s.max_premium:g} with a "
                    f"{'strong green ' if s.strong_green else ''}{cm}-min candle closed above VWAP"
                    f"{f' with EMA{s.ema_period} crossing above VWAP' if s.ema_cross else ''}"
                    f"{f' (entry price hit under {s.max_entry_visits} times earlier)' if s.max_entry_visits else ''}"
                    f"{' and SuperTrend green' if s.supertrend else ''}"
                    f"{' on the NIFTY SuperTrend side' if s.index_trend else ''}"
                    f" after {s.entry_start}")
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
