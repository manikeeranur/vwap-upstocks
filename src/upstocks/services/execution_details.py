"""Read-only intraday price excursions; never changes execution or paper results."""
import asyncio
from datetime import datetime
import hashlib
import json
import logging
import math
import time

from .. import upstox
from ..stocks.domain import TERMINAL

log = logging.getLogger(__name__)


def timestamp(value, day):
    """Legacy order clocks are IST; full timestamps retain their explicit offset."""
    if not value:
        return None
    try:
        text = str(value)
        at = datetime.fromisoformat(f"{day}T{text}" if len(text) <= 8 else text)
        return at.replace(tzinfo=upstox.IST) if at.tzinfo is None else at.astimezone(upstox.IST)
    except (ValueError, TypeError):
        return None


def order_time(order, day, *, last=False):
    attempts = [*order.get("attempts", []), order]
    times = [timestamp(a.get("created_at") or a.get("at"), day) for a in attempts
             if a.get("attempt_filled_qty", a.get("filled_qty", 0))]
    valid = [t for t in times if t is not None]
    return (max(valid) if last else min(valid)) if valid else None


def candle_peaks(rows, start, end, entry):
    """Minute-level approximation, including the entry/exit minute; first tie wins."""
    first, last = start.replace(second=0, microsecond=0), end.replace(second=0, microsecond=0)
    window = []
    for row in rows:
        try:
            at = timestamp(row[0], start.date().isoformat())
            high, low = float(row[2]), float(row[3])
            if at is not None and first <= at <= last and math.isfinite(high) and math.isfinite(low) and 0 < low <= high:
                window.append((at, high, low))
        except (ValueError, TypeError, IndexError):
            continue
    if not window:
        return None
    window.sort()
    hi = max(window, key=lambda r: r[1])
    lo = min(window, key=lambda r: r[2])
    high = (hi[1], hi[0], "candle") if hi[1] > entry else (entry, start, "entry")
    low = (lo[2], lo[0], "candle") if lo[2] < entry else (entry, start, "entry")
    def peak(item):
        price, at, source = item
        return {"price": round(price, 2), "at": at.isoformat(), "points": round(price-entry, 2), "source": source}
    return {"high": peak(high), "low": peak(low), "through": window[-1][0].isoformat(),
            "starts_at_entry": window[0][0] == first, "ends_at_exit": window[-1][0] == last}


class ExecutionDetails:
    def __init__(self, repository):
        self.repository = repository
        self._lock = asyncio.Lock()
        self._recent = {}

    async def get(self, snapshot):
        # Snapshot is already scoped to the signed-in user's accounts.
        session = snapshot.get("intraday_session") or {}
        day = session.get("day")
        now = timestamp(session.get("server_now"), day) or datetime.now(upstox.IST)
        result = {"day": day, "accounts": {}}
        async with self._lock:
            self._recent = {k: v for k, v in self._recent.items() if v[0] > time.monotonic()}
            work = []
            for account, orders in snapshot["state"].get("orders", {}).items():
                for tid, sides in orders.items():
                    parts = tid.split("|", 3)
                    if len(parts) != 4:
                        continue
                    _, trade_day, _, instrument = parts
                    buy, sell = sides.get("buy") or {}, sides.get("sell") or {}
                    bought, sold = buy.get("filled_qty") or 0, sell.get("filled_qty") or 0
                    pending = any(o and (o.get("filled_qty") is None or o.get("execution_status") not in TERMINAL) for o in (buy, sell))
                    start, exit_at = order_time(buy, trade_day), order_time(sell, trade_day, last=True)
                    if trade_day != day and bought <= sold and not pending and not (exit_at and exit_at.date().isoformat() == day):
                        continue
                    info = {"entry_at": start.isoformat() if start else None,
                            "exit_at": exit_at.isoformat() if exit_at else None, "time_basis": "order",
                            "points": round(sell["fill_price"]-buy["fill_price"], 2) if sold and isinstance(buy.get("fill_price"), (int, float)) and isinstance(sell.get("fill_price"), (int, float)) else None,
                            "status": "unavailable", "high": None, "low": None,
                            "buy_qty": bought, "sell_qty": sold, "buy_price": buy.get("fill_price"), "sell_price": sell.get("fill_price")}
                    result["accounts"].setdefault(account, {})[tid] = info
                    if not bought or start is None or not isinstance(buy.get("fill_price"), (int, float)) or not math.isfinite(buy["fill_price"]) or buy["fill_price"] <= 0:
                        continue
                    closed = sold >= bought and not pending
                    end = exit_at if closed else now
                    if end is None or end < start or end.date() != start.date():
                        continue  # No invented multi-day range for an intraday record.
                    work.append((info, instrument, start, end, buy["fill_price"], closed, buy.get("contract", {}).get("expiry")))
            semaphore = asyncio.Semaphore(3)
            async def one(item):
                async with semaphore:
                    info, instrument, start, end, entry, closed, expiry = item
                    key = "execution_peaks:v1:" + hashlib.sha256(json.dumps([instrument, start.isoformat(), end.isoformat() if closed else "open", entry, closed, expiry]).encode()).hexdigest()
                    try:
                        saved = self._recent.get(key)
                        if saved:
                            info.update(saved[1])
                            return
                        stored = await asyncio.to_thread(self.repository.get_json, key) if closed else None
                        if stored:
                            info.update(stored)
                            self._recent[key] = (time.monotonic()+60, stored)
                            return
                        expired = bool(expiry and expiry < now.date().isoformat())
                        rows = await asyncio.wait_for(upstox.candles(instrument, "1m", start.date(), end.date(), expired), 6)
                        peaks = candle_peaks(rows, start, end, entry)
                        if peaks and peaks["starts_at_entry"] and (not closed or peaks["ends_at_exit"]):
                            details = {"status": "available", "candle_source": "upstox", **peaks}
                            if closed:
                                await asyncio.to_thread(self.repository.set_json, key, details)
                        else:
                            details = {"status": "incomplete" if peaks else "unavailable"}
                    except Exception:
                        log.warning("Execution candle details unavailable", extra={"instrument": instrument})
                        details = {"status": "unavailable"}
                    self._recent[key] = (time.monotonic()+60, details)
                    info.update(details)
            await asyncio.gather(*(one(item) for item in work))
        return result
