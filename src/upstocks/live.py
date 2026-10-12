"""Strategy helpers. Runtime state and scanner lifecycle live in services/trading.py."""
from dataclasses import replace
from datetime import date, datetime, timedelta

from . import combo, ema_pullback, smc, upstox, vwap

PRESETS = ["frontend_live", "smc_live", "ema25_vwap", "ema_pullback", "smc_tuned", "smc_ema_combo"]


LABELS = {"frontend_live": "VWAP 9:30", "smc_live": "SMC", "ema25_vwap": "VWAP + EMA25 + SuperTrend",
          "ema_pullback": "EMA9/EMA26 pullback (Mani)", "smc_tuned": "SMC Tuned",
          "smc_ema_combo": "SMC + EMA pullback (1 trade a day)"}


FRESH_MIN = 3


KEEP = ("date", "expiry", "symbol", "strike", "type", "instrument_key", "entry_time", "entry", "sl", "target",
        "status", "exit_time", "exit", "pnl", "pnl_pct", "peak", "spot", "why")


def now_ist() -> datetime:
    return datetime.now(upstox.IST)


def market_open(t: datetime | None = None) -> bool:
    t = t or now_ist()
    return t.weekday() < 5 and "09:15" <= f"{t:%H:%M}" <= "15:30"


def trade_id(p: str, t: dict) -> str:
    return f"{p}|{t['date']}|{t['entry_time']}|{t['instrument_key']}"


async def run_engine(preset: str, day: date) -> list[dict] | None:
    """The preset's trades on `day`, or None if NIFTY has no candles that day (holiday)."""
    st = replace(vwap.Settings(start=day, end=day), **vwap.PRESETS[preset])
    spot = await vwap.trading_days_and_spot(day - timedelta(days=7), day)
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
    else:
        res = await vwap.run_day(day, st, spot[d], prev)
    return [vwap.trade_dict(t) for t in res.trades]
