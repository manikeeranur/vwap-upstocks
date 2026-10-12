"""Own the scanner lifecycle and serialize live-order decisions."""
import asyncio
from copy import deepcopy
from datetime import date, timedelta
import logging
import uuid

from .. import live, upstox
from ..observability import redact

log = logging.getLogger(__name__)


class TradingService:
    def __init__(self, repository):
        self.repository = repository
        self.state = repository.state()
        self.view = repository.get_view()
        self.view_revision = uuid.uuid4().hex
        self._scan_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._task = None

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="market-scanner")

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def snapshot(self):
        return {"state": deepcopy(self.state), "view": deepcopy(self.view), "market_open": live.market_open(),
                "now": live.now_ist().strftime("%a %d %b %H:%M:%S"), "view_revision": self.view_revision}

    async def _activity(self, message):
        message = redact(f"{live.now_ist():%Y-%m-%d %H:%M:%S} {message}")
        await asyncio.to_thread(self.repository.activity, message)
        self.state["log"] = (self.state["log"] + [message])[-300:]
        log.info(message)

    async def configure(self, preset=None, **changes):
        # A user can disable live orders while market data is being fetched.
        # This lock protects the final decision through the durable reservation.
        async with self._config_lock:
            revision = await asyncio.to_thread(self.repository.configure, preset, **changes)
            before = self.state["config"][preset]["live"] if preset else None
            if preset:
                for key in ("on", "live", "lots"):
                    if changes.get(key) is not None:
                        self.state["config"][preset][key] = changes[key]
            if changes.get("lot_size") is not None:
                self.state["lot_size"] = changes["lot_size"]
            self.state["revision"] = revision
            if changes.get("entries_paused") is not None:
                self.state["entries_paused"] = changes["entries_paused"]
            if preset and changes.get("live") is not None and before != changes["live"]:
                await self._activity(f"{live.LABELS[preset]}: LIVE {'ON' if changes['live'] else 'OFF'}")
        return deepcopy(self.state)

    def _open_positions(self, preset):
        return [tid for tid, orders in self.state["orders"].items()
                if tid.startswith(preset + "|") and orders.get("buy", {}).get("order_id") and "sell" not in orders]

    async def scan(self, day: date | None = None):
        day = day or upstox.today_ist()
        is_today = day == upstox.today_ist()
        async with self._scan_lock:
            self.view["running"] = True
            try:
                results, errors = {}, {}
                for preset in live.PRESETS:
                    cfg = self.state["config"][preset]
                    if not (cfg["on"] or cfg["live"] or (is_today and self._open_positions(preset))):
                        continue
                    try:
                        trades = await live.run_engine(preset, day)
                    except Exception as exc:
                        errors[preset] = redact(str(exc))
                        log.exception("Strategy scan failed", extra={"preset": preset})
                        continue
                    if trades is None:
                        continue
                    for trade in trades:
                        trade["open"] = is_today and live.market_open() and trade["status"] == "EOD"
                        trade["id"] = live.trade_id(preset, trade)
                    if is_today:
                        await self._act(preset, trades)
                    for trade in trades:
                        trade["orders"] = deepcopy(self.state["orders"].get(trade["id"], {})) if is_today else {}
                    results[preset] = trades
                view = {"day": day.isoformat(), "scanned_at": f"{live.now_ist():%H:%M:%S}",
                        "results": results, "errors": errors, "running": False}
                sessions = None
                if is_today and live.market_open():
                    sessions = {p: {"lots": self.state["config"][p]["lots"], "lot_size": self.state["lot_size"],
                                    "updated": f"{live.now_ist():%Y-%m-%d %H:%M}",
                                    "trades": [{**{k: t.get(k) for k in live.KEEP}, "id": t["id"], "open": t["open"],
                                                "live": bool(t["orders"].get("buy", {}).get("order_id"))} for t in trades]}
                                for p, trades in results.items()}
                await asyncio.to_thread(self.repository.save_scan, view, sessions,
                                        {"config": deepcopy(self.state["config"]), "lot_size": self.state["lot_size"],
                                         "entries_paused": self.state["entries_paused"], "revision": self.state["revision"],
                                         "is_replay": not is_today, "market_open": live.market_open()})
                self.view = view
                self.view_revision = uuid.uuid4().hex
                log.info("Scan completed", extra={"status_code": 200 if not errors else 207})
                return deepcopy(view)
            finally:
                self.view["running"] = False

    async def _order(self, preset, tid, trade, side, why):
        async with self._config_lock:
            if side == "BUY" and (self.state["entries_paused"] or not self.state["config"][preset]["live"] or not live.market_open()):
                return False
            buy = self.state["orders"].get(tid, {}).get("buy", {})
            qty = buy.get("qty") if side == "SELL" and buy.get("qty") else self.state["config"][preset]["lots"] * self.state["lot_size"]
            record = {"at": f"{live.now_ist():%H:%M:%S}", "qty": qty, "why": why, "symbol": trade["symbol"], "status": "pending"}
            context = {"trade": deepcopy(trade), "config": deepcopy(self.state["config"][preset]),
                       "lot_size": self.state["lot_size"], "revision": self.state["revision"]}
            if not await asyncio.to_thread(self.repository.reserve_order, tid, preset, side, record, context):
                return False
            self.state["orders"].setdefault(tid, {})[side.lower()] = record
        # No transaction spans a network request. Unknown outcomes are never retried.
        try:
            order_id = await upstox.place_market_order(trade["instrument_key"], qty, side, f"upstocks-{preset[:6]}")
            if not order_id:
                raise RuntimeError("Broker did not return an order ID")
            record.update(order_id=order_id, status="submitted")
            message = f"{live.LABELS[preset]}: {side} {qty} {trade['symbol']} ({why}) → order {order_id}"
        except asyncio.CancelledError:
            record.update(status="unknown", error="Order interrupted. Verify its status with the broker; it will not be retried automatically.")
            await asyncio.to_thread(self.repository.finish_order, tid, side, record,
                                    f"Order needs reconciliation: {tid} {side}")
            raise
        except Exception as exc:
            record.update(status="unknown", error=redact(str(exc)))
            message = f"{live.LABELS[preset]}: {side} {qty} {trade['symbol']} needs broker review: {record['error']}"
            log.exception("Order outcome unknown", extra={"trade_id": tid, "preset": preset})
        message = redact(f"{live.now_ist():%Y-%m-%d %H:%M:%S} {message}")
        await asyncio.to_thread(self.repository.finish_order, tid, side, record, message)
        self.state["log"] = (self.state["log"] + [message])[-300:]
        log.info(message)
        return record["status"] == "submitted"

    async def _act(self, preset, trades):
        for trade in trades:
            tid = trade["id"]
            orders = self.state["orders"].get(tid, {})
            if "buy" not in orders:
                now = live.now_ist()
                h, m = map(int, trade["entry_time"].split(":"))
                age = (now - now.replace(hour=h, minute=m, second=0, microsecond=0)).total_seconds() / 60
                if trade["open"] and 0 <= age <= live.FRESH_MIN:
                    await self._order(preset, tid, trade, "BUY", f"entry {trade['entry_time']} @ ₹{trade['entry']:.2f}")
            elif orders["buy"].get("order_id") and "sell" not in orders and not trade["open"]:
                await self._order(preset, tid, trade, "SELL", f"{trade['status']} {trade.get('exit_time') or ''}".strip())

    async def square_off(self, preset):
        sent = 0
        async with self._scan_lock:
            for tid in self._open_positions(preset):
                buy = self.state["orders"][tid]["buy"]
                trade = {"symbol": buy["symbol"], "instrument_key": tid.split("|", 3)[3]}
                sent += await self._order(preset, tid, trade, "SELL", "manual square-off")
        return sent

    async def _loop(self):
        while True:
            now = live.now_ist()
            nxt = (now + timedelta(minutes=1)).replace(second=5, microsecond=0)
            await asyncio.sleep((nxt - now).total_seconds())
            if live.market_open(nxt) and nxt.strftime("%H:%M") >= "09:16":
                try:
                    await self.scan()
                except Exception as exc:
                    log.exception("Scheduled scan failed")
                    self.view["errors"] = {"scan": redact(str(exc))}
