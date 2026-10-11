"""Paper + live auto-trade for the four strategy presets.

Every minute in market hours (a few seconds after the candle closes) each
switched-on preset's backtest engine is run on today's data so far. The
engines only ever look at completed candles, so the trades they return are
what the strategy would have done up to now: a trade still "EOD" while the
market is open is an open position, marked at the last close.

- Paper: those trades, P&L = points × lots × lot size. No orders.
- Live: a NEW trade (entry within the last FRESH_MIN minutes, so a restart or
  switching Live on mid-trade never buys a stale signal) sends a BUY MARKET
  intraday order for lots × lot size; when the engine closes that trade, a
  SELL MARKET order. Exits are always sent for positions this app bought, even
  if Live was switched off afterwards. Square-off sells at once.
  A trade is bought at most once: every order is saved to live_state.json
  before it is sent, and checked on each scan.

Exits are market orders at the scan after the engine's exit minute, so live
fills differ from the engine's SL/target prices by that minute's movement.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from . import ema_pullback, smc, upstox, vwap

PRESETS = ["frontend_live", "smc_live", "ema25_vwap", "ema_pullback"]
LABELS = {"frontend_live": "VWAP 9:30", "smc_live": "SMC", "ema25_vwap": "VWAP + EMA25 + SuperTrend",
          "ema_pullback": "EMA9/EMA26 pullback (Mani)"}
STATE_FILE = Path(__file__).resolve().parents[2] / "live_state.json"
FRESH_MIN = 3

_lock = asyncio.Lock()
_task: asyncio.Task | None = None
STATE: dict = {}
VIEW: dict = {"day": None, "scanned_at": None, "results": {}, "errors": {}, "running": False}


def now_ist() -> datetime:
    return datetime.now(upstox.IST)


def _load() -> None:
    global STATE
    try:
        STATE = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        STATE = {}
    STATE.setdefault("lot_size", 65)
    STATE.setdefault("config", {})
    for p in PRESETS:
        cfg = STATE["config"].setdefault(p, {})
        cfg.setdefault("on", True)      # paper
        cfg.setdefault("live", False)
        cfg.setdefault("lots", 1)
    STATE.setdefault("orders", {})      # trade id → {"buy": {...}, "sell": {...}}
    STATE.setdefault("log", [])


def _save() -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE, indent=1))
    tmp.replace(STATE_FILE)


def _log(msg: str) -> None:
    STATE["log"].append(f"{now_ist():%Y-%m-%d %H:%M:%S} {msg}")
    STATE["log"] = STATE["log"][-300:]


def market_open(t: datetime | None = None) -> bool:
    t = t or now_ist()
    return t.weekday() < 5 and "09:15" <= f"{t:%H:%M}" <= "15:30"


def trade_id(p: str, t: dict) -> str:
    return f"{p}|{t['date']}|{t['entry_time']}|{t['instrument_key']}"


def _open_positions(p: str) -> list[str]:
    return [k for k, o in STATE["orders"].items()
            if k.startswith(p + "|") and "order_id" in o.get("buy", {}) and "sell" not in o]


async def run_engine(preset: str, day: date) -> list[dict]:
    st = replace(vwap.Settings(start=day, end=day), **vwap.PRESETS[preset])
    spot = await vwap.trading_days_and_spot(day - timedelta(days=7), day)
    d = day.isoformat()
    if d not in spot:
        return []
    keys = sorted(spot)
    prev = spot[keys[keys.index(d) - 1]] if keys.index(d) else []
    if st.strategy == "smc":
        res = await smc.run_day(day, st, spot[d])
    elif st.strategy == "ema_pullback":
        res = await ema_pullback.run_day(day, st, spot[d])
    else:
        res = await vwap.run_day(day, st, spot[d], prev)
    return [vwap.trade_dict(t) for t in res.trades]


async def scan(day: date | None = None) -> dict:
    """Run every switched-on preset for `day` (default today); on today, act on Live."""
    day = day or upstox.today_ist()
    is_today = day == upstox.today_ist()
    live_now = is_today and market_open()
    async with _lock:
        VIEW["running"] = True
        try:
            results, errors = {}, {}
            for p in PRESETS:
                cfg = STATE["config"][p]
                if not (cfg["on"] or cfg["live"] or (is_today and _open_positions(p))):
                    continue
                try:
                    trades = await run_engine(p, day)
                except Exception as e:  # one strategy failing must not stop the others
                    errors[p] = str(e)
                    continue
                for t in trades:
                    t["open"] = live_now and t["status"] == "EOD"
                    t["id"] = trade_id(p, t)
                if is_today:
                    await _act(p, trades)
                for t in trades:
                    t["orders"] = STATE["orders"].get(t["id"], {}) if is_today else {}
                results[p] = trades
            VIEW.update(day=day.isoformat(), scanned_at=f"{now_ist():%H:%M:%S}", results=results, errors=errors)
            _save()
            return VIEW
        finally:
            VIEW["running"] = False


async def _order(p: str, tid: str, t: dict, side: str, why: str) -> None:
    buy = STATE["orders"].get(tid, {}).get("buy", {})
    qty = buy.get("qty") if side == "SELL" and buy.get("qty") else STATE["config"][p]["lots"] * STATE["lot_size"]
    rec = {"at": f"{now_ist():%H:%M:%S}", "qty": qty, "why": why, "symbol": t["symbol"]}
    # Saved before sending: a crash or restart mid-call can never lead to a resend.
    STATE["orders"].setdefault(tid, {})[side.lower()] = rec
    _save()
    try:
        rec["order_id"] = await upstox.place_market_order(t["instrument_key"], qty, side, f"upstocks-{p[:6]}")
        _log(f"{LABELS[p]}: {side} {qty} {t['symbol']} ({why}) → order {rec['order_id']}")
    except Exception as e:
        rec["error"] = str(e)
        _log(f"{LABELS[p]}: {side} {qty} {t['symbol']} ({why}) FAILED: {e}")
    _save()


async def _act(p: str, trades: list[dict]) -> None:
    now = now_ist()
    for t in trades:
        tid, o = t["id"], STATE["orders"].get(t["id"], {})
        if "buy" not in o:
            h, m = map(int, t["entry_time"].split(":"))
            age = (now - now.replace(hour=h, minute=m, second=0, microsecond=0)).total_seconds() / 60
            if STATE["config"][p]["live"] and t["open"] and 0 <= age <= FRESH_MIN:
                await _order(p, tid, t, "BUY", f"entry {t['entry_time']} @ ₹{t['entry']:.2f}")
        elif "order_id" in o["buy"] and "sell" not in o and not t["open"]:
            await _order(p, tid, t, "SELL", f"{t['status']} {t.get('exit_time') or ''}".strip())


async def square_off(p: str) -> int:
    """Sell every open live position of preset p now."""
    n = 0
    async with _lock:
        for tid in _open_positions(p):
            o = STATE["orders"][tid]
            t = {"symbol": o["buy"]["symbol"], "instrument_key": tid.split("|", 3)[3]}
            await _order(p, tid, t, "SELL", "manual square-off")
            n += 1
    return n


def set_config(p: str | None = None, on: bool | None = None, live: bool | None = None,
               lots: int | None = None, lot_size: int | None = None) -> None:
    if p:
        cfg = STATE["config"][p]
        if on is not None:
            cfg["on"] = on
        if live is not None and live != cfg["live"]:
            cfg["live"] = live
            _log(f"{LABELS[p]}: LIVE {'ON' if live else 'OFF'}")
        if lots is not None:
            cfg["lots"] = max(1, int(lots))
    if lot_size is not None:
        STATE["lot_size"] = max(1, int(lot_size))
    _save()


async def _loop() -> None:
    while True:
        n = now_ist()
        nxt = (n + timedelta(minutes=1)).replace(second=5, microsecond=0)  # candle closed + a few s
        await asyncio.sleep((nxt - n).total_seconds())
        if market_open(nxt) and nxt.strftime("%H:%M") >= "09:16":
            try:
                await scan()
            except Exception as e:
                _log(f"scan failed: {e}")
                VIEW["errors"] = {"scan": str(e)}


def start() -> None:
    global _task
    _load()
    if _task is None:
        _task = asyncio.create_task(_loop())
