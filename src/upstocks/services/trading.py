"""Own the scanner lifecycle and serialize live-order decisions.

One scan produces each strategy's trades. Every person's broker account ("<user id>:<broker>")
with that strategy's live switch on sends its own orders with its own daily token; accounts
never share an order. Each order is reserved durably before the broker call, never sent
twice, and an unknown outcome is never retried.
"""
import asyncio
from copy import deepcopy
from datetime import date, timedelta
import logging
import uuid

from .. import brokers, live, upstox
from ..observability import redact
from ..repository import account_id

log = logging.getLogger(__name__)


class TradingService:
    def __init__(self, repository, vault=None):
        self.repository = repository
        self.vault = vault
        self.state = repository.state()
        self.view = repository.get_view()
        self.view_revision = uuid.uuid4().hex
        self._scan_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._task = None
        self._skipped = set()   # (account, trade id) already reported as "not logged in"
        self._peak_cache = {}   # closed trade -> (peak high, peak low)

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="market-scanner")

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def reload_people(self):
        """Pick up people or switches added outside this service (new user, keys)."""
        fresh = self.repository.state()
        for key in ("users", "accounts"):
            self.state[key] = fresh[key]

    def snapshot(self, user_id=None):
        """What one person sees: shared strategy settings and scan, plus only their own accounts and orders."""
        state, view = deepcopy(self.state), deepcopy(self.view)
        if user_id is not None:
            mine = {a: v for a, v in state["accounts"].items() if v["user"] == user_id}
            state["accounts"] = mine
            state["orders"] = {a: o for a, o in state["orders"].items() if a in mine}
            state["entries_paused"] = state["users"].get(user_id, {}).get("paused", False)
            name = state["users"].get(user_id, {}).get("username", "")
            others = [u["username"] for uid, u in state["users"].items() if uid != user_id]
            state["log"] = [m for m in state["log"] if not any(f" {o} · " in m for o in others)] if others else state["log"]
            state["users"] = {user_id: state["users"].get(user_id, {"username": name})}
            for trades in (view.get("results") or {}).values():
                for trade in trades:
                    trade["orders"] = {a: o for a, o in (trade.get("orders") or {}).items() if a in mine}
        return {"state": state, "view": view, "market_open": live.market_open(),
                "now": live.now_ist().strftime("%a %d %b %H:%M:%S"), "view_revision": self.view_revision}

    async def _activity(self, message):
        message = redact(f"{live.now_ist():%Y-%m-%d %H:%M:%S} {message}")
        await asyncio.to_thread(self.repository.activity, message)
        self.state["log"] = (self.state["log"] + [message])[-300:]
        log.info(message)

    def _who(self, account):
        acc = self.state["accounts"][account]
        return f"{self.state['users'].get(acc['user'], {}).get('username', acc['user'])} · {brokers.LABELS[acc['broker']]}"

    async def configure(self, preset=None, user=None, broker=None, **changes):
        # A user can disable live orders while market data is being fetched.
        # This lock protects the final decision through the durable reservation.
        async with self._config_lock:
            revision = await asyncio.to_thread(self.repository.configure, preset, user=user, broker=broker, **changes)
            if preset and changes.get("on") is not None:
                self.state["config"][preset]["on"] = changes["on"]
            account = account_id(user, broker) if user is not None and broker else None
            before = None
            if preset and account and account in self.state["accounts"]:
                acc = self.state["accounts"][account]
                before = acc["live"][preset]
                if changes.get("live") is not None:
                    acc["live"][preset] = changes["live"]
                if changes.get("lots") is not None:
                    acc["lots"][preset] = changes["lots"]
            if changes.get("lot_size") is not None:
                self.state["lot_size"] = changes["lot_size"]
            if changes.get("entries_paused") is not None and user in self.state["users"]:
                self.state["users"][user]["paused"] = changes["entries_paused"]
            self.state["revision"] = revision
            if account and changes.get("live") is not None and before != changes["live"]:
                await self._activity(f"{self._who(account)} · {live.LABELS[preset]}: LIVE {'ON' if changes['live'] else 'OFF'}")
        return deepcopy(self.state)

    def _open_positions(self, preset, account=None):
        out = []
        for acc, trades in self.state["orders"].items():
            if account and acc != account:
                continue
            out += [(acc, tid) for tid, orders in trades.items()
                    if tid.startswith(preset + "|") and orders.get("buy", {}).get("order_id") and "sell" not in orders]
        return out

    async def _peaks(self, day, trades):
        """The option's highest and lowest price from entry to exit (or now, while open), from 1-minute candles.
        Closed trades never change, so they are worked out once."""
        async def one(t):
            key = (day.isoformat(), t.get("id"), t.get("exit_time"), t.get("open"))
            if not t.get("open") and key in self._peak_cache:
                t["peak_high"], t["peak_low"] = self._peak_cache[key]
                return
            try:
                expired = str(t.get("expiry") or "")[:10] < upstox.today_ist().isoformat()
                rows = await upstox.candles(t["instrument_key"], "1m", day, day, expired)
            except Exception:
                log.warning("Peak candles unavailable", extra={"trade": t.get("id")})
                return
            end = "23:59" if t.get("open") else (t.get("exit_time") or "23:59")
            window = [r for r in rows if t["entry_time"] <= r[0][11:16] <= end]
            if not window:
                return
            hi = max([float(r[2]) for r in window] + [float(t["entry"])])
            lo = min([float(r[3]) for r in window] + [float(t["entry"])])
            t["peak_high"], t["peak_low"] = round(hi, 2), round(lo, 2)
            if not t.get("open"):
                self._peak_cache[key] = (t["peak_high"], t["peak_low"])
        await asyncio.gather(*(one(t) for t in trades if t.get("instrument_key") and t.get("entry_time")))

    def _any_live(self, preset):
        return any(acc["live"].get(preset) for acc in self.state["accounts"].values())

    async def scan(self, day: date | None = None):
        day = day or upstox.today_ist()
        is_today = day == upstox.today_ist()
        async with self._scan_lock:
            self.view["running"] = True
            try:
                results, errors = {}, {}
                for preset in live.PRESETS:
                    cfg = self.state["config"][preset]
                    if not (cfg["on"] or self._any_live(preset) or (is_today and self._open_positions(preset))):
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
                        trade["orders"] = ({acc: deepcopy(o[trade["id"]]) for acc, o in self.state["orders"].items() if trade["id"] in o}
                                           if is_today else {})
                    await self._peaks(day, trades)   # after orders, so it never delays them
                    results[preset] = trades
                view = {"day": day.isoformat(), "scanned_at": f"{live.now_ist():%H:%M:%S}",
                        "results": results, "errors": errors, "running": False}
                sessions = None
                if is_today and live.market_open():
                    sessions = {p: {"lots": 1, "lot_size": self.state["lot_size"],
                                    "updated": f"{live.now_ist():%Y-%m-%d %H:%M}",
                                    "trades": [{**{k: t.get(k) for k in live.KEEP}, "id": t["id"], "open": t["open"],
                                                "live": any(o.get("buy", {}).get("order_id") for o in t["orders"].values())}
                                               for t in trades]}
                                for p, trades in results.items()}
                await asyncio.to_thread(self.repository.save_scan, view, sessions,
                                        {"config": deepcopy(self.state["config"]), "accounts": deepcopy(self.state["accounts"]),
                                         "paused": {uid: u["paused"] for uid, u in self.state["users"].items()},
                                         "lot_size": self.state["lot_size"], "revision": self.state["revision"],
                                         "is_replay": not is_today, "market_open": live.market_open()})
                self.view = view
                self.view_revision = uuid.uuid4().hex
                log.info("Scan completed", extra={"status_code": 200 if not errors else 207})
                return deepcopy(view)
            finally:
                self.view["running"] = False

    def _credentials(self, account):
        acc = self.state["accounts"][account]
        row = self.repository.broker_account(acc["user"], acc["broker"]) or {}
        open_ = self.vault.open if self.vault else (lambda v: v)
        return brokers.Credentials(acc["broker"], row.get("api_key", ""), open_(row.get("api_secret", "")),
                                   open_(row.get("access_token", "")), row.get("token_issued"))

    async def _order(self, account, preset, tid, trade, side, why):
        acc = self.state["accounts"].get(account)
        if acc is None:
            return False
        async with self._config_lock:
            if side == "BUY" and (self.state["users"].get(acc["user"], {}).get("paused") or not acc["live"].get(preset)
                                  or not live.market_open()):
                return False
            creds = await asyncio.to_thread(self._credentials, account)
            if not creds.token_valid(live.now_ist()):
                # Not logged in today: nothing reserved, nothing sent.
                if (account, tid, side) not in self._skipped:
                    self._skipped.add((account, tid, side))
                    await self._activity(f"{self._who(account)} · {live.LABELS[preset]}: {side} {trade.get('symbol', '')} skipped — "
                                         f"not logged in to {brokers.LABELS[acc['broker']]} today")
                return False
            buy = self.state["orders"].get(account, {}).get(tid, {}).get("buy", {})
            exit_qty = buy.get("qty") if side == "SELL" and buy.get("qty") else None
            lots = acc["lots"].get(preset, 1)
            qty = exit_qty or lots * self.state["lot_size"]
            record = {"at": f"{live.now_ist():%H:%M:%S}", "qty": qty, "why": why, "symbol": trade.get("symbol", ""),
                      "broker": acc["broker"], "status": "pending",
                      # Kept so an exit (incl. manual square-off) can find the same contract at any broker.
                      "contract": {k: trade.get(k) for k in ("expiry", "strike", "type") if trade.get(k) is not None}}
            context = {"trade": deepcopy(trade), "account": account, "lots": lots, "lot_size": self.state["lot_size"],
                       "revision": self.state["revision"]}
            if not await asyncio.to_thread(self.repository.reserve_order, tid, account, preset, side, record, context):
                return False
            self.state["orders"].setdefault(account, {}).setdefault(tid, {})[side.lower()] = record
        # No transaction spans a network request. Unknown outcomes are never retried.
        try:
            order_id, sent_qty, symbol = await brokers.place_order(creds, trade, lots, self.state["lot_size"], side,
                                                                   f"upstocks{preset[:6]}", qty=exit_qty)
            if not order_id:
                raise RuntimeError("Broker did not return an order ID")
            record.update(order_id=order_id, status="submitted", qty=sent_qty, broker_symbol=symbol)
            message = f"{self._who(account)} · {live.LABELS[preset]}: {side} {sent_qty} {symbol} ({why}) → order {order_id}"
        except asyncio.CancelledError:
            record.update(status="unknown", error="Order interrupted. Verify its status with the broker; it will not be retried automatically.")
            await asyncio.to_thread(self.repository.finish_order, tid, account, side, record,
                                    f"Order needs reconciliation: {tid} {account} {side}")
            raise
        except Exception as exc:
            record.update(status="unknown", error=redact(str(exc)))
            message = f"{self._who(account)} · {live.LABELS[preset]}: {side} {qty} {trade.get('symbol', '')} needs broker review: {record['error']}"
            log.exception("Order outcome unknown", extra={"trade_id": tid, "preset": preset, "account": account})
        message = redact(f"{live.now_ist():%Y-%m-%d %H:%M:%S} {message}")
        await asyncio.to_thread(self.repository.finish_order, tid, account, side, record, message)
        self.state["log"] = (self.state["log"] + [message])[-300:]
        log.info(message)
        return record["status"] == "submitted"

    async def _act(self, preset, trades):
        for trade in trades:
            tid = trade["id"]
            for account, acc in list(self.state["accounts"].items()):
                orders = self.state["orders"].get(account, {}).get(tid, {})
                if "buy" not in orders:
                    if not acc["live"].get(preset):
                        continue
                    now = live.now_ist()
                    h, m = map(int, trade["entry_time"].split(":"))
                    age = (now - now.replace(hour=h, minute=m, second=0, microsecond=0)).total_seconds() / 60
                    if trade["open"] and 0 <= age <= live.FRESH_MIN:
                        await self._order(account, preset, tid, trade, "BUY", f"entry {trade['entry_time']} @ ₹{trade['entry']:.2f}")
                elif orders["buy"].get("order_id") and "sell" not in orders and not trade["open"]:
                    await self._order(account, preset, tid, trade, "SELL", f"{trade['status']} {trade.get('exit_time') or ''}".strip())

    async def square_off(self, preset, user=None, broker=None):
        sent = 0
        async with self._scan_lock:
            for account, tid in self._open_positions(preset):
                acc = self.state["accounts"].get(account, {})
                if (user is not None and acc.get("user") != user) or (broker and acc.get("broker") != broker):
                    continue
                buy = self.state["orders"][account][tid]["buy"]
                trade = {"symbol": buy["symbol"], "instrument_key": tid.split("|", 3)[3],
                         **(buy.get("contract") or {})}
                sent += await self._order(account, preset, tid, trade, "SELL", "manual square-off")
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
