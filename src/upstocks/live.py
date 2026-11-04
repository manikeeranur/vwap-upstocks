"""Strategy helpers. Runtime state and scanner lifecycle live in services/trading.py."""
from dataclasses import replace
from datetime import date, datetime, timedelta

from . import combo, ema_pullback, harmonic, pdh, smc, upstox, vol_breakout, vwap, vwap_touch

PRESETS = ["frontend_live", "smc_live", "ema25_vwap", "ema_pullback", "smc_tuned", "smc_ema_combo", "harmonic", "harmonic_2y",
           "sensex_swing", "sensex_bigleg", "harmonic_power", "pdh_breakout", "harmonic_2y_compound", "pdh_compound",
           "vwap_touch", "vwap_touch_compound", "vwap_touch_expiry", "vol_breakout"]


LABELS = {"frontend_live": "VWAP 9:30", "smc_live": "SMC", "ema25_vwap": "VWAP + EMA25 + SuperTrend",
          "ema_pullback": "EMA9/EMA26 pullback (Mani)", "smc_tuned": "SMC Tuned",
          "smc_ema_combo": "SMC + EMA pullback (1 trade a day)", "harmonic": "Harmonic", "harmonic_2y": "Harmonic Optimized",
          "sensex_swing": "SENSEX Harmonic Swing", "sensex_bigleg": "SENSEX Harmonic Big Leg",
          "harmonic_power": "Harmonic Power Trail", "pdh_breakout": "Previous-day Breakout",
          "harmonic_2y_compound": "Harmonic Optimized Compounding", "pdh_compound": "Previous-day Breakout Compounding",
          "vwap_touch": "VWAP Touch (PE)", "vwap_touch_compound": "VWAP Touch Compounding",
          "vwap_touch_expiry": "VWAP Touch Expiry (EMA50)", "vol_breakout": "Vol Breakout (Expiry)"}


FRESH_MIN = 3

# Strategies whose trades are exactly another's (same settings, only the lots differ): a scan runs the
# engine once for both.
SAME_TRADES = {"harmonic_2y_compound": "harmonic_2y", "pdh_compound": "pdh_breakout", "vwap_touch_compound": "vwap_touch"}


KEEP = ("date", "expiry", "symbol", "strike", "type", "instrument_key", "entry_time", "entry", "sl", "target",
        "status", "exit_time", "exit", "pnl", "pnl_pct", "peak", "spot", "why")


def now_ist() -> datetime:
    return datetime.now(upstox.IST)


def market_open(t: datetime | None = None) -> bool:
    t = t or now_ist()
    return t.weekday() < 5 and "09:15" <= f"{t:%H:%M}" <= "15:30"


def underlying(preset: str) -> str:
    return vwap.PRESETS[preset].get("underlying", "NIFTY")


def lot_size(preset: str, nifty_lot: int) -> int:
    """Contract lot size for the preset's index: NIFTY uses the saved setting, SENSEX its exchange lot."""
    idx = underlying(preset)
    return nifty_lot if idx == "NIFTY" else upstox.LOT_SIZE[idx]


def paper_only(preset: str) -> bool:
    """SENSEX strategies place no broker orders yet (their broker contract lookup is NIFTY only)."""
    return underlying(preset) != "NIFTY"


def trade_id(p: str, t: dict) -> str:
    return f"{p}|{t['date']}|{t['entry_time']}|{t['instrument_key']}"


def filter_saved_smc_entries(view: dict, now: datetime) -> None:
    """Apply the scan's time boundary to a display copy of a pre-fix snapshot."""
    if view.get("day") != now.date().isoformat():
        return
    current = now.strftime("%H:%M")
    # Latest scans store HH:MM:SS; saved journal views store YYYY-MM-DD HH:MM.
    scan_time = (view.get("scanned_at") or current).split()[-1][:5]
    through = min(scan_time, current)
    for preset, trades in (view.get("results") or {}).items():
        if preset not in {"smc_live", "smc_tuned", "smc_ema_combo"}:
            continue
        visible = []
        after = None
        cooldown = vwap.PRESETS[preset].get("cooldown_min", vwap.Settings.cooldown_min)
        for trade in trades:
            entry = trade.get("entry_time")
            if not entry or entry > through or (after is not None and vwap._mins(entry) <= after):
                continue
            visible.append(trade)
            after = smc.reentry_blocked_through(trade.get("status", "EOD"), trade.get("pnl") or 0, trade.get("exit_time"), cooldown)
            if after is None:
                break
        view["results"][preset] = visible


async def run_engine(preset: str, day: date) -> list[dict] | None:
    """The preset's trades on `day`, or None if its index has no candles that day (holiday)."""
    st = replace(vwap.Settings(start=day, end=day), **vwap.PRESETS[preset])
    spot = await vwap.trading_days_and_spot(day - timedelta(days=30 if st.strategy == "vol_breakout" else 7), day, st.underlying)
    d = day.isoformat()
    if d not in spot:
        return None
    keys = sorted(spot)
    prev = spot[keys[keys.index(d) - 1]] if keys.index(d) else []
    if st.strategy == "smc":
        res = await smc.run_day(day, st, spot[d], as_of=now_ist())
    elif st.strategy == "ema_pullback":
        res = await ema_pullback.run_day(day, st, spot[d])
    elif st.strategy == "combo":
        res = await combo.run_day(day, st, spot[d])
    elif st.strategy == "harmonic":
        res = await harmonic.run_day(day, st, spot[d], prev)
    elif st.strategy == "pdh":
        res = await pdh.run_day(day, st, spot[d], prev)
    elif st.strategy == "vwap_touch":
        res = await vwap_touch.run_day(day, st, spot[d], prev)
    elif st.strategy == "vol_breakout":
        res = await vol_breakout.run_day(day, st, spot[d], [spot[k] for k in keys[:keys.index(d)]])
    else:
        res = await vwap.run_day(day, st, spot[d], prev)
    return [vwap.trade_dict(t) for t in res.trades]
