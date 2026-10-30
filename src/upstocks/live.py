"""Strategy helpers. Runtime state and scanner lifecycle live in services/trading.py."""
from dataclasses import replace
from datetime import date, datetime, timedelta

from . import combo, ema_pullback, harmonic, smc, upstox, vwap

PRESETS = ["frontend_live", "smc_live", "ema25_vwap", "ema_pullback", "smc_tuned", "smc_ema_combo", "harmonic", "harmonic_2y",
           "sensex_swing", "sensex_bigleg"]


LABELS = {"frontend_live": "VWAP 9:30", "smc_live": "SMC", "ema25_vwap": "VWAP + EMA25 + SuperTrend",
          "ema_pullback": "EMA9/EMA26 pullback (Mani)", "smc_tuned": "SMC Tuned",
          "smc_ema_combo": "SMC + EMA pullback (1 trade a day)", "harmonic": "Harmonic", "harmonic_2y": "Harmonic Optimized",
          "sensex_swing": "SENSEX Harmonic Swing", "sensex_bigleg": "SENSEX Harmonic Big Leg"}


FRESH_MIN = 3


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


async def run_engine(preset: str, day: date) -> list[dict] | None:
    """The preset's trades on `day`, or None if its index has no candles that day (holiday)."""
    st = replace(vwap.Settings(start=day, end=day), **vwap.PRESETS[preset])
    spot = await vwap.trading_days_and_spot(day - timedelta(days=7), day, st.underlying)
    d = day.isoformat()
    if d not in spot:
        return None
    keys = sorted(spot)
    prev = spot[keys[keys.index(d) - 1]] if keys.index(d) else []
    if st.strategy == "smc":
        res = await smc.run_day(day, st, spot[d])
    elif st.strategy == "ema_pullback":
        res = await ema_pullback.run_day(day, st, spot[d])
    elif st.strategy == "combo":
        res = await combo.run_day(day, st, spot[d])
    elif st.strategy == "harmonic":
        res = await harmonic.run_day(day, st, spot[d], prev)
    else:
        res = await vwap.run_day(day, st, spot[d], prev)
    return [vwap.trade_dict(t) for t in res.trades]
