"""SMC + EMA9/26 pullback as one strategy — ONE trade a day, 1 lot.

Each day both engines run (each limited to one trade) and the trade whose entry
comes first is taken; the other strategy's trade that day is dropped (a tie
goes to SMC). Trades are tagged [SMC] / [EMA9/26] in the reason column.

The two engines use the settings below — found by a joint search on 1 Apr –
30 Sep 2026 (46 SMC × 46 EMA9/26 settings, scored one trade a day), not the
standalone SMC / EMA pullback presets:
  SMC      entries 09:32–14:59, SL 50%, target 150%, breakeven 25%, reversal
           exit after +35% (rest as the SMC Tuned preset: ₹100–800, ±20 strikes,
           15:15 exit)
  EMA9/26  premium ₹200–<300, SL 50%, target 100% (rest as Mani's rules)
Backtest 1 Apr – 30 Sep 2026: net ₹2,05,873 for 1 lot after charges, 123 trades.
The form's rule fields don't apply here.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

from . import ema_pullback, smc, vwap
from .vwap import DayResult, Settings

LEGS = (
    ("SMC", "smc_tuned", smc.run_day,
     {"entry_start": "09:32", "last_entry": "14:59", "sl_pct": 50, "target_pct": 150, "breakeven_pct": 25,
      "rev_exit": True, "ep_rev_activate": 35, "max_trades": 1}),
    ("EMA9/26", "ema_pullback", ema_pullback.run_day,
     {"min_premium": 200, "sl_pct": 50, "target_pct": 100, "entry_start": "09:36", "last_entry": "14:59",
      "max_trades": 1}),
)


async def run_day(day: date, s: Settings, spot_bars: list) -> DayResult:
    parts = []
    for tag, preset, engine, over in LEGS:
        st = replace(vwap.Settings(start=s.start, end=s.end), **{**vwap.PRESETS[preset], **over})
        parts.append((tag, await engine(day, st, spot_bars)))
    res = DayResult(day.isoformat(), next((r.expiry for _, r in parts if r.expiry), None))
    first = None
    for tag, r in parts:  # SMC first, so it wins a tie
        for t in r.trades[:1]:
            if first is None or t.entry_time < first[1].entry_time:
                first = (tag, t)
    if first:
        tag, t = first
        t.why = f"[{tag}] {t.why}".strip()
        res.trades.append(t)
    else:
        res.note = " · ".join(f"{tag}: {r.note}" for tag, r in parts if r.note)
    return res
