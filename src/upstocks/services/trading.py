"""Own the scanner lifecycle and serialize live-order decisions.

One scan produces each strategy's trades. Every person's broker account ("<user id>:<broker>")
with that strategy's live switch on sends its own orders with its own daily token; accounts
never share an order. Each order is reserved durably before the broker call, never sent
twice, and an unknown outcome is never retried.
"""
import asyncio
from copy import deepcopy
from datetime import date, timedelta
import json
import logging
import time
import uuid

from .. import brokers, execution_brokers, fees, live, upstox
from ..stocks.domain import TERMINAL, PortfolioError
from ..order_evidence import current_attempt, apply_evidence, combine
from ..observability import redact
from ..repository import account_id
from .exchange_calendar import ExchangeCalendar
from .execution_details import ExecutionDetails

log = logging.getLogger(__name__)


class TradingService:
    def __init__(self, repository, vault=None):
        self.repository = repository
        self.calendar = ExchangeCalendar(repository)
        self.execution_details = ExecutionDetails(repository)
        self.vault = vault
        self.state = repository.state()
        self.view = repository.get_view()
        self.view_revision = uuid.uuid4().hex
        self._scan_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._task = None
        self._reconcile_task = None
        self._report_task = None
        self.last_cycle = None
        self.last_reconcile = None
        self.started_at = time.monotonic()
        self._fills_errors = {}
        self._fee_errors = {}
        self._fee_retry = {}
        self._fills_lock = asyncio.Lock()
        self._skipped = set()   # (account, trade id) already reported as "not logged in"
        self._peak_cache = {}   # closed trade -> (peak high, peak low)
        self._historical_retry = {}
        self._fills_cache = {}  # account -> (monotonic time, {order id: fill price})

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="market-scanner")
            self._reconcile_task = asyncio.create_task(self._reconcile_loop(), name="order-reconciliation")

    def worker_health(self):
        return {"running": bool(self._task and not self._task.done() and self._reconcile_task and not self._reconcile_task.done()),
                "stale": time.monotonic() - (self.last_cycle or self.started_at) > 180 or time.monotonic() - (self.last_reconcile or self.started_at) > 120,
                "reconciliation_stale": time.monotonic() - (self.last_reconcile or self.started_at) > 120}

    async def stop(self):
        tasks = [t for t in (self._task, self._reconcile_task, self._report_task) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = self._reconcile_task = self._report_task = None

    async def _reconcile_loop(self):
        while True:
            try:
                await self.record_fills()
            except Exception:
                log.exception("Order reconciliation cycle failed")
            self.last_reconcile = time.monotonic()
            await asyncio.sleep(20)

    def reload_people(self):
        """Pick up people or switches added outside this service (new user, keys)."""
        fresh = self.repository.state()
        for key in ("users", "accounts"):
            self.state[key] = fresh[key]

    def snapshot(self, user_id=None, *, intraday=False):
        """What one person sees: shared strategy settings and scan, plus only their own accounts and orders."""
        now = live.now_ist()
        session = self.calendar.intraday_session(now) if intraday else None
        state, view = deepcopy(self.state), deepcopy(self.view)
        if session is not None and view.get("day") != session["day"]:
            view = self.repository.paper_view(session["day"]) if session["day"] else {
                "day": None, "scanned_at": None, "results": {}, "errors": {}, "running": False}
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
        state["lot_sizes"] = {p: live.lot_size(p, state["lot_size"]) for p in live.PRESETS}
        state["paper_only"] = [p for p in live.PRESETS if live.paper_only(p)]
        return {"state": state, "view": view, "market_open": live.market_open(),
                "market_calendar": self.calendar.day(now.date()),
                "intraday_session": session,
                "now": now.strftime("%a %d %b %H:%M:%S"), "view_revision": self.view_revision,
                "fee_status": {a: {"error": self._fee_errors.get(a)} for a in state["accounts"]},
                "reconciliation": {a: {"error": self._reconciliation_error(a), "age_seconds": round(time.monotonic() - self._fills_cache[a][0]) if a in self._fills_cache else None}
                                   for a in state["accounts"]}}

    def _reconciliation_error(self, account):
        error = self._fills_errors.get(account)
        if error == "Broker login expired; reconciliation unavailable" and not self.calendar.day(live.now_ist().date())["login_alerts_required"]:
            error = None
        return error or ("Unresolved orders block new entries; verified broker evidence is required" if self._unresolved(account) else None)

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
                    if tid.startswith(preset + "|") and (orders.get("buy", {}).get("filled_qty", 0) - orders.get("sell", {}).get("filled_qty", 0)) > 0]
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

    async def live_fills(self, user_id):
        """Persist terminal status and cumulative quantities; never infer fills from an ID."""
        out = {}
        async with self._fills_lock:
            for account, acc in list(self.state["accounts"].items()):
                saved = self.state["orders"].get(account, {})
                if acc["user"] != user_id or not saved:
                    continue
                hit = self._fills_cache.get(account)
                if hit and time.monotonic() - hit[0] < 20:
                    out[account] = hit[1]
                    continue
                try:
                    creds = await asyncio.to_thread(self._credentials, account)
                    if not creds.token_valid():
                        raise brokers.BrokerError("Broker login expired; reconciliation unavailable")
                    rows = await asyncio.wait_for(execution_brokers.order_book(creds), 20)
                    fills = {}
                    errors = []
                    historical_budget = 2
                    for tid, sides in list(saved.items()):
                        for side, order in list(sides.items()):
                            matches = [r for r in rows if (order.get("order_id") and r["order_id"] == str(order["order_id"]))
                                       or (order.get("client_tag") and r.get("tag") == order["client_tag"])]
                            if not matches and order.get("execution_status") not in TERMINAL:
                                if not order.get("order_id"):
                                    errors.append("Unknown submission has no unique broker ID; review statement evidence")
                                    continue
                                key = (account,str(order["order_id"]))
                                if historical_budget <= 0 or self._historical_retry.get(key,0) > time.monotonic():
                                    errors.append("Historical order awaits verification; import a statement if unavailable")
                                    continue
                                historical_budget -= 1
                                self._historical_retry[key] = time.monotonic()+300
                                try:
                                    matches = [await asyncio.wait_for(execution_brokers.order_detail(creds,order["order_id"],equity=False),6)]
                                except Exception:
                                    errors.append("Historical order unavailable from broker; import verified statement evidence")
                                    continue
                            if not matches:
                                continue
                            if len(matches) != 1:
                                raise brokers.BrokerError("Ambiguous broker order reference")
                            row = matches[0]
                            if not order.get("client_id") or order["client_id"] != creds.client_id:
                                raise brokers.BrokerError("Order belongs to a different or unverified broker login")
                            if row["qty"] != order.get("qty") or row["side"] != side.upper():
                                raise brokers.BrokerError("Broker order quantity or side does not match intent")
                            current = current_attempt(order)
                            context = dict(current.get("fee_context") or {})
                            context.update({k: row[k] for k in ("symbol", "exchange", "product", "order_type", "variety", "key", "security_id", "segment") if row.get(k)})
                            context.update(side=side.upper(), trade_date=tid.split("|")[1])
                            current["fee_context"] = context
                            if row["filled_qty"] < current["filled_qty"]:
                                continue
                            updated = combine(order,apply_evidence(current,row))
                            updated.update(order_id=row["order_id"],status="submitted")
                            if row["filled_qty"]:
                                fills[row["order_id"]] = row["average_price"]
                            updated.pop("error", None)
                            if updated != order:
                                await asyncio.to_thread(self.repository.finish_order, tid, account, side.upper(), updated,
                                                        f"{self._who(account)} · broker status reconciled")
                                order.clear()
                                order.update(updated)
                    self._fills_cache[account] = (time.monotonic(), fills)
                    if errors:
                        self._fills_errors[account] = "; ".join(sorted(set(errors)))
                    else:
                        self._fills_errors.pop(account, None)
                    out[account] = fills
                    await self._store_charges(account, creds)
                except Exception as exc:
                    self._fills_errors[account] = redact(str(exc)) or "Broker evidence unavailable"
                    log.warning("Broker reconciliation unavailable", extra={"account": account}, exc_info=True)
        return out

    async def _store_charges(self, account, creds):
        """Store fee evidence for each actual attempt, including partial/cancelled fills."""
        errors, budget = [], 2
        for tid, sides in self.state["orders"].get(account, {}).items():
            for side, order in sides.items():
                if order.get("charges_source") == "operator_statement":
                    continue
                attempts = deepcopy(order.get("attempts", []))
                current = current_attempt(order)
                changed = False
                for attempt in [*attempts, current]:
                    if not attempt.get("filled_qty") or attempt.get("charges_paise") is not None or fees.valid_estimate(attempt):
                        continue
                    if attempt.get("client_id") != creds.client_id or not creds.client_id:
                        errors.append("Fee account does not match the saved order")
                        continue
                    key = (account, attempt.get("order_id"), attempt["filled_qty"], attempt["notional"])
                    if self._fee_retry.get(key, 0) > time.monotonic():
                        errors.append("Broker charge calculation will retry")
                        continue
                    if not budget:
                        errors.append("Additional charges will refresh on the next cycle")
                        continue
                    budget -= 1
                    self._fee_retry[key] = time.monotonic() + 300
                    context = dict(attempt.get("fee_context") or {})
                    context.update(side=side.upper(), trade_date=tid.split("|")[1])
                    # Legacy Kite NFO orders were submitted by kite_place as MIS MARKET.
                    # Do not invent delivery/other-broker metadata for old manual orders.
                    if creds.broker == "kite" and "|NSE_FO|" in tid and (attempt.get("contract") or {}).get("type") in ("CE", "PE"):
                        for k, v in {"symbol": attempt.get("broker_symbol"), "exchange": "NFO",
                                     "product": "MIS", "order_type": "MARKET", "variety": "regular"}.items():
                            context.setdefault(k, v)
                    try:
                        attempt["fee_estimate"] = await asyncio.wait_for(fees.calculate(creds, attempt, context), 3)
                        attempt["fee_context"] = context
                        self._fee_retry.pop(key, None)
                        changed = True
                    except Exception as exc:
                        errors.append(redact(str(exc)) or "Broker charges unavailable")
                if changed:
                    updated = combine(order, current, attempts)
                    await asyncio.to_thread(self.repository.finish_order, tid, account, side.upper(), updated,
                                            "Broker fee estimate saved for confirmed fills")
                    order.update(updated)
                    for field in ("charges", "charges_source", "estimated_charges", "estimated_charges_qty", "estimated_charges_notional"):
                        if field not in updated:
                            order.pop(field, None)
        error = "; ".join(sorted(set(errors)))
        if error:
            if self._fee_errors.get(account) != error:
                log.warning("Broker fee calculation pending", extra={"account": account, "reason": error})
            self._fee_errors[account] = error
        else:
            self._fee_errors.pop(account, None)

    async def record_fills(self):
        users = {a["user"] for account, a in self.state["accounts"].items()
                 if self.state["orders"].get(account)}
        await asyncio.gather(*(self.live_fills(uid) for uid in users))

    def _unresolved(self, account):
        return any(o.get("execution_status") not in TERMINAL
                   for sides in self.state["orders"].get(account, {}).values() for o in sides.values())

    def _any_live(self, preset):
        return any(acc["live"].get(preset) for acc in self.state["accounts"].values())

    async def scan(self, day: date | None = None, *, execute_live: bool = True):
        day = day or upstox.today_ist()
        is_today = day == upstox.today_ist()
        async with self._scan_lock:
            self.view["running"] = True
            try:
                results, errors = {}, {}
                async def calculate(preset):
                    cfg = self.state["config"][preset]
                    if not (cfg["on"] or self._any_live(preset) or (is_today and self._open_positions(preset))):
                        return preset, None, None
                    try:
                        return preset, await asyncio.wait_for(live.run_engine(preset, day), 45), None
                    except Exception as exc:
                        log.exception("Strategy scan failed", extra={"preset": preset})
                        return preset, None, redact(str(exc)) or "Strategy scan timed out"

                tasks = [asyncio.create_task(calculate(p)) for p in live.PRESETS]
                for finished in asyncio.as_completed(tasks):
                    preset, trades, error = await finished
                    if error:
                        errors[preset] = error
                        continue
                    cfg = self.state["config"][preset]
                    if not (cfg["on"] or self._any_live(preset) or (is_today and self._open_positions(preset))):
                        continue
                    if trades is None:
                        continue
                    for trade in trades:
                        trade["open"] = is_today and live.market_open() and trade["status"] == "EOD"
                        trade["id"] = live.trade_id(preset, trade)
                    if is_today and execute_live:
                        await self._act(preset, trades)
                    for trade in trades:
                        trade["orders"] = ({acc: deepcopy(o[trade["id"]]) for acc, o in self.state["orders"].items() if trade["id"] in o}
                                           if is_today else {})
                    results[preset] = trades
                view = {"day": day.isoformat(), "scanned_at": f"{live.now_ist():%H:%M:%S}",
                        "results": results, "errors": errors, "running": False}
                sessions = None
                if is_today and live.market_open():
                    sessions = {p: {"lots": 1, "lot_size": live.lot_size(p, self.state["lot_size"]),
                                    "updated": f"{live.now_ist():%Y-%m-%d %H:%M}",
                                    "trades": [{**{k: t.get(k) for k in live.KEEP}, "id": t["id"], "open": t["open"],
                                                "live": any(o.get("buy", {}).get("order_id") for o in t["orders"].values())}
                                               for t in trades]}
                                for p, trades in results.items()}
                await asyncio.to_thread(self.repository.save_scan, view, sessions,
                                        {"config": deepcopy(self.state["config"]), "accounts": deepcopy(self.state["accounts"]),
                                         "paused": {uid: u["paused"] for uid, u in self.state["users"].items()},
                                         "lot_size": self.state["lot_size"], "revision": self.state["revision"],
                                         "is_replay": not is_today, "execute_live": execute_live, "market_open": live.market_open()})
                self.view = view
                if self._report_task:
                    self._report_task.cancel()
                    await asyncio.gather(self._report_task, return_exceptions=True)
                async def enrich():
                    try:
                        await asyncio.wait_for(asyncio.gather(*(self._peaks(day, ts) for ts in results.values())), 30)
                    except Exception:
                        log.warning("Optional trade statistics unavailable", exc_info=True)
                self._report_task = asyncio.create_task(enrich(), name="trade-statistics")
                self.view_revision = uuid.uuid4().hex
                log.info("Scan completed", extra={"status_code": 200 if not errors else 207})
                return deepcopy(view)
            finally:
                self.view["running"] = False
                for task in locals().get("tasks", []):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*locals().get("tasks", []), return_exceptions=True)
                self.last_cycle = time.monotonic()

    def _credentials(self, account):
        acc = self.state["accounts"][account]
        row = self.repository.broker_account(acc["user"], acc["broker"]) or {}
        open_ = self.vault.open if self.vault else (lambda v: v)
        return brokers.Credentials(acc["broker"], row.get("api_key", ""), open_(row.get("api_secret", "")),
                                   open_(row.get("access_token", "")), row.get("token_issued"), str(row.get("broker_user") or ""),
                                   json.loads(row.get("profile") or "{}").get("name", ""))

    async def _order(self, account, preset, tid, trade, side, why, replacement=None):
        acc = self.state["accounts"].get(account)
        if acc is None or live.paper_only(preset):
            return False
        async with self._config_lock:
            if side == "BUY" and (self._unresolved(account) or self._fills_errors.get(account) or self.state["users"].get(acc["user"], {}).get("paused") or not acc["live"].get(preset)
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
            exit_qty = buy.get("filled_qty", 0) - (replacement or {}).get("filled_qty", 0) if side == "SELL" else None
            if side == "SELL" and (buy.get("execution_status") not in TERMINAL or not exit_qty):
                return False
            lots = acc["lots"].get(preset, 1)
            lot_size = live.lot_size(preset, self.state["lot_size"])
            qty = exit_qty or lots * lot_size
            client_id, holder = creds.client_id, creds.holder
            if not client_id:
                await self._activity(f"{self._who(account)} · broker account identity unavailable; reconnect before trading")
                return False
            if side == "SELL" and (not buy.get("client_id") or client_id != buy["client_id"]):
                await self._activity(f"{self._who(account)} · exit blocked: broker account identity changed or is unknown")
                return False
            record = {"at": f"{live.now_ist():%H:%M:%S}", "created_at":live.now_ist().isoformat(), "qty": qty, "why": why, "symbol": trade.get("symbol", ""),
                      "broker": acc["broker"], "status": "pending", "client_tag": "ut" + uuid.uuid4().hex[:18],
                      "filled_qty": 0, "execution_status": "unknown",
                      "fee_context": {"side": side, "trade_date": live.now_ist().date().isoformat(),
                                      "key": trade.get("instrument_key"), "variety": "regular",
                                      "order_type": "MARKET", "segment": "DERIVATIVE",
                                      "exchange": "NFO" if acc["broker"] == "kite" else "NSE",
                                      "product": {"kite": "MIS", "upstox": "I", "indmoney": "INTRADAY"}[acc["broker"]]},
                      # The broker account this order went to — a broker's login can change between days.
                      "client_id": client_id, "holder": holder,
                      # Kept so an exit (incl. manual square-off) can find the same contract at any broker.
                      "contract": {k: trade.get(k) for k in ("expiry", "strike", "type") if trade.get(k) is not None}}
            context = {"trade": deepcopy(trade), "account": account, "lots": lots, "lot_size": lot_size,
                       "revision": self.state["revision"]}
            if replacement:
                record["total_qty"] = buy["filled_qty"]
                record = combine(record,current_attempt(record),[*replacement.get("attempts",[]),current_attempt(replacement)])
                await asyncio.to_thread(self.repository.replace_exit,tid,account,replacement,record)
            elif not await asyncio.to_thread(self.repository.reserve_order, tid, account, preset, side, record, context):
                return False
            self.state["orders"].setdefault(account, {}).setdefault(tid, {})[side.lower()] = record
        # No transaction spans a network request. Unknown outcomes are never retried.
        try:
            order_id, sent_qty, symbol = await brokers.place_order(creds, trade, lots, lot_size, side,
                                                                   record["client_tag"], qty=exit_qty)
            if not order_id:
                raise RuntimeError("Broker did not return an order ID")
            record.update(order_id=order_id, status="submitted", qty=sent_qty, broker_symbol=symbol)
            record["fee_context"]["symbol"] = symbol
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
                elif orders["buy"].get("filled_qty", 0) > 0 and "sell" not in orders and not trade["open"]:
                    await self._order(account, preset, tid, trade, "SELL", f"{trade['status']} {trade.get('exit_time') or ''}".strip())

    async def recover_exit(self, user_id, body):
        account = account_id(user_id,body.broker)
        if not live.market_open():
            raise PortfolioError("Exit recovery is available during market hours")
        self._fills_cache.pop(account,None)
        await self.live_fills(user_id)
        async with self._scan_lock, self._fills_lock:
            orders = self.state["orders"].get(account,{}).get(body.trade_id,{})
            buy,sell = orders.get("buy",{}),orders.get("sell",{})
            if sell.get("order_id") != body.order_id or sell.get("execution_status") not in ("cancelled","rejected"):
                raise PortfolioError("Select a reconciled cancelled/rejected exit")
            if buy.get("execution_status") not in TERMINAL or self._unresolved(account):
                raise PortfolioError("Reconcile all pending orders before replacing an exit")
            remaining = buy.get("filled_qty",0)-sell.get("filled_qty",0)
            if remaining <= 0:
                raise PortfolioError("No strategy-owned quantity remains")
            creds = await asyncio.to_thread(self._credentials,account)
            evidence = await execution_brokers.stock_snapshot(creds)
            if evidence["client_id"] != buy.get("client_id") or evidence["client_id"] != sell.get("client_id"):
                raise PortfolioError("Broker account identity changed")
            symbol = sell.get("broker_symbol") or buy.get("broker_symbol")
            if not symbol:
                raise PortfolioError("Exact broker contract is unavailable; reconcile it before recovery")
            known = {a.get("order_id") for o in (buy,sell) for a in [*o.get("attempts",[]),current_attempt(o)]}
            if any(r["symbol"] == symbol and r["order_id"] not in known and (r["filled_qty"] or r["status"] not in TERMINAL) for r in evidence["orders"]):
                raise PortfolioError("Manual or other-strategy activity exists for this contract")
            positions = [r for r in evidence["positions"] if (r.get("tradingsymbol") or r.get("trading_symbol") or r.get("name")) == symbol]
            net = sum(int(r.get("net_qty") if body.broker == "indmoney" else r.get("quantity",0)) for r in positions)
            if net != remaining:
                raise PortfolioError("Broker position does not match the remaining strategy quantity")
            trade = {"symbol":buy["symbol"],"instrument_key":body.trade_id.split("|",3)[3],**buy.get("contract",{})}
            sent = await self._order(account,body.trade_id.split("|")[0],body.trade_id,trade,"SELL","explicit recovery of remaining quantity",replacement=deepcopy(sell))
            return {"sold":int(sent),"remaining_quantity":remaining}

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
            self.last_cycle = time.monotonic()
            await asyncio.sleep(max(0, (nxt - now).total_seconds()))
            if live.market_open(nxt) and nxt.strftime("%H:%M") >= "09:16":
                try:
                    await self.scan()
                except Exception as exc:
                    log.exception("Scheduled scan failed")
                    self.view["errors"] = {"scan": redact(str(exc))}
