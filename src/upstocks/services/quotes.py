"""Shared, bounded quote polling for display; never changes trading decisions."""
import asyncio
from datetime import datetime, timezone
import logging
import math
from time import monotonic

from .. import live, upstox

log = logging.getLogger(__name__)


class QuoteService:
    def __init__(self, trading):
        self.trading = trading
        self._task = None
        self._prices = {}
        self._next_attempt = 0.0
        self._failures = 0
        self._error = None
        self._day = None
        self._closed = False

    def _targets(self):
        view = self.trading.view
        day = upstox.today_ist().isoformat()
        if not live.market_open():
            return {}, "market_closed"
        if view.get("day") != day:
            return {}, "replay" if view.get("day") else "idle"
        targets = {}
        for trades in view.get("results", {}).values():
            for trade in trades:
                entry = trade.get("entry")
                if trade.get("open") and trade.get("id") and trade.get("instrument_key") and type(entry) in (int, float) and math.isfinite(entry) and entry > 0:
                    targets[trade["id"]] = {"key": trade["instrument_key"], "entry": entry}
        return targets, "active" if targets else "idle"

    async def get(self):
        targets, status = self._targets()
        day = self.trading.view.get("day")
        if self._closed:
            return self._snapshot({}, "unavailable")
        if self._day != day:
            self._prices.clear()
            self._day = day
        keys = {trade["key"] for trade in targets.values()}
        self._prices = {k: v for k, v in self._prices.items() if k in keys}
        if status != "active":
            return self._snapshot(targets, status)
        if self._task is None and monotonic() >= self._next_attempt:
            self._task = asyncio.create_task(self._fetch(keys, day), name="open-trade-quotes")
        if self._task:
            # A closing browser tab must not cancel the request shared by others.
            await asyncio.shield(self._task)
        targets, status = self._targets()  # a scan/exit may finish during the read
        return self._snapshot(targets, status)

    async def _fetch(self, keys, day):
        started = monotonic()
        # Allow minor browser-timer jitter; the gateway enforces the exact
        # one-second minimum. Otherwise a 999ms arrival skips a whole tick.
        self._next_attempt = started + 0.95
        try:
            async with asyncio.timeout(3.5):
                prices = await upstox.ltp_quotes(sorted(keys))
            current, status = self._targets()
            active_keys = {trade["key"] for trade in current.values()}
            if status != "active" or self.trading.view.get("day") != day:
                return
            at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            received = monotonic()
            for key, price in prices.items():
                if key in active_keys and type(price) in (int, float) and math.isfinite(price) and price > 0:
                    self._prices[key] = {"price": price, "received_at": at, "monotonic": received}
            if keys - prices.keys():
                raise upstox.QuoteUnavailable("Some live prices are unavailable. Retrying shortly.")
            if self._error:
                log.info("Live quote updates recovered")
            self._error, self._failures = None, 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failures += 1
            self._error = str(exc) if isinstance(exc, upstox.QuoteUnavailable) else "Live quotes unavailable. Retrying shortly."
            delay = max(min(30, 2 ** min(self._failures, 5)), getattr(exc, "retry_after", 0))
            self._next_attempt = monotonic() + delay
            if self._failures == 1:
                log.warning("Live quote updates delayed (%s)", type(exc).__name__)
        finally:
            self._task = None

    def _snapshot(self, targets, status):
        marks = {}
        now = monotonic()
        if status == "active":
            for tid, trade in targets.items():
                quote = self._prices.get(trade["key"])
                if not quote:
                    continue
                pnl = round(quote["price"] - trade["entry"], 2)
                marks[tid] = {"ltp": quote["price"], "pnl": pnl, "pnl_pct": round(pnl / trade["entry"] * 100, 2),
                              "received_at": quote["received_at"], "stale": bool(self._error) or now - quote["monotonic"] > 3}
            if self._error or len(marks) != len(targets) or any(m["stale"] for m in marks.values()):
                status = "delayed"
            else:
                status = "live"
        return {"day": self.trading.view.get("day"), "view_revision": self.trading.view_revision,
                "status": status, "trades": marks, "error": self._error if status == "delayed" else None,
                "poll_interval_ms": 1000, "source": "upstox_ltp_v3"}

    async def stop(self):
        self._closed = True
        if self._task:
            task = self._task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._prices.clear()
