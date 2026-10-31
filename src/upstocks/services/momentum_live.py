"""Independent, account-scoped monthly execution and broker reconciliation."""

import asyncio
from datetime import date, datetime, timedelta
import logging
import time

from .. import brokers, fees, execution_brokers as execution, live, momentum, upstox
from ..observability import redact
from ..stocks.domain import PortfolioError, TERMINAL, buy_quantity, decide, paise, quantity
from ..stocks.store import StockStore
from .reconciliation import ReconciliationService

log = logging.getLogger(__name__)
START = date(2026, 11, 1)


class MomentumLiveService:
    def __init__(self, app):
        self.app = app
        self.store = StockStore(app.state.database)
        self.reconciliation = ReconciliationService(self.store)
        self._task = None
        self._locks = {}
        self._fenced = set()
        self._ranking_task = None
        self._ranking_day = None
        self.last_cycle = None
        self.started_at = time.monotonic()
        self.last_error = None
        self._market_day = None
        self._fee_retry = {}
        self._fee_errors = {}

    def load(self):
        repo = self.app.state.repository
        self.store.ensure(repo.users(), repo.get_json("momentum_tuned"))
        self.store.recover()

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="momentum-execution")

    async def stop(self):
        tasks = [t for t in (self._task, self._ranking_task) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = self._ranking_task = None

    def worker_health(self):
        running = bool(self._task and not self._task.done())
        stale = time.monotonic() - (self.last_cycle or self.started_at) > 120
        return {
            "running": running,
            "stale": stale,
            "error": self.last_error,
            "blocked_in_memory": len(self._fenced),
        }

    def _lock(self, pid):
        return self._locks.setdefault(pid, asyncio.Lock())

    async def snapshot(self, user_id):
        repo = self.app.state.repository
        await asyncio.to_thread(self.store.ensure, [repo.user(user_id)], repo.get_json("momentum_tuned"))
        portfolios = await asyncio.to_thread(self.store.snapshot, user_id)
        return {
            "portfolios": [{**p, "fee_error": self._fee_errors.get(p["id"])} for p in portfolios],
            "market_calendar": self.app.state.trading.calendar.day(live.now_ist().date()),
            "worker": self.worker_health(),
            "global_entries_paused": self.app.state.trading.state["users"]
            .get(user_id, {})
            .get("paused", False),
            "start": START.isoformat(),
            "money_unit": "paise",
            "preview": self.preview_status(),
            "entry_sources": [
                {"id": "monthly", "label": "Wait for the next monthly decision"},
                {"id": "paper", "label": "Follow my current paper holding"},
            ]
            + [
                {
                    "id": "backtest:" + r["id"],
                    "label": "Saved tuned backtest · "
                    + str(r.get("through") or r.get("end"))
                    + " · "
                    + r["id"],
                }
                for r in (self.app.state.momentum.runs() if hasattr(self.app.state.momentum, "runs") else [])
                if r.get("score") == "tuned"
            ],
        }

    async def _get(self, pid):
        uid = int(pid.split(":")[0])
        return next(p for p in await asyncio.to_thread(self.store.snapshot, uid) if p["id"] == pid)

    async def _creds(self, p):
        account = self.app.state.trading.state["accounts"].get(p["id"])
        if not account or account["user"] != p["user_id"] or account["broker"] != p["broker"]:
            raise PortfolioError("Connect this user's broker account first")
        creds = await asyncio.to_thread(self.app.state.trading._credentials, p["id"])
        if not creds.token_valid(live.now_ist()):
            raise PortfolioError("Broker login expired. Reconnect this account to reconcile.")
        return creds

    async def configure(self, user_id, body):
        await self.snapshot(user_id)
        pid = f"{user_id}:{body.broker}"
        async with self._lock(pid):
            p = await self._get(pid)
            client_id = p["client_id"]
            if body.broker != "paper" and body.enabled:
                creds = await self._creds(p)
                evidence = await execution.stock_snapshot(creds)
                client_id = evidence["client_id"]
                if p["client_id"] and client_id != p["client_id"]:
                    raise PortfolioError("Broker login differs from this portfolio's owner")
            await asyncio.to_thread(
                self.store.configure,
                pid,
                body.revision,
                enabled=body.enabled,
                paused=body.paused,
                usable_capital=paise(body.usable_capital),
                client_id=client_id,
                entry_source=body.entry_source,
            )
        return await self.snapshot(user_id)

    async def _sync_fees(self, p, rows=None):
        missing = [o for o in p["orders"] if o["filled_qty"] and o.get("charges_paise") is None
                   and not fees.valid_estimate(fees.stock_attempt(o))]
        if not missing:
            self._fee_errors.pop(p["id"], None)
            return p
        errors = []
        for order in missing[:2]:
            key = (order["id"], order["filled_qty"], order["notional"])
            if self._fee_retry.get(key, 0) > time.monotonic():
                errors.append("Broker charge calculation will retry")
                continue
            self._fee_retry[key] = time.monotonic() + 300
            try:
                creds = await self._creds(p)
                context = {"symbol": order["symbol"], "key": order["instrument_key"], "side": order["side"],
                           "exchange": "NSE", "product": "D" if p["broker"] == "upstox" else "CNC",
                           "variety": "regular", "order_type": "LIMIT", "segment": "EQUITY",
                           "trade_date": order["created_at"][:10]}
                if p["broker"] == "indmoney":
                    matching: dict = next((r for r in rows or [] if r["order_id"] == order["broker_id"]), {})
                    context["security_id"] = matching.get("security_id") or await brokers.ind_equity_id(creds, order["isin"])
                estimate = await asyncio.wait_for(fees.calculate(creds, fees.stock_attempt(order), context), 3)
                await asyncio.to_thread(self.store.save_fee_estimate, order["id"], estimate)
                self._fee_retry.pop(key, None)
            except Exception as exc:
                errors.append(redact(str(exc)) or "Broker charges unavailable")
        if errors:
            message = "; ".join(sorted(set(errors)))
            if self._fee_errors.get(p["id"]) != message:
                log.warning("Stock fee calculation pending", extra={"portfolio": p["id"], "reason": message})
            self._fee_errors[p["id"]] = message
        else:
            self._fee_errors.pop(p["id"], None)
        return await self._get(p["id"])

    async def _reconcile(self, p, evidence):
        if p["client_id"] != evidence["client_id"]:
            raise PortfolioError("Broker login differs from this portfolio's owner")
        for order in p["orders"]:
            if order["status"] in TERMINAL:
                continue
            rows = evidence["orders"]
            if order["broker_id"] and not any(r["order_id"] == order["broker_id"] for r in rows):
                detail = await execution.order_detail(await self._creds(p), order["broker_id"])
                rows = [*rows, detail]
            row = execution.match_order(order, rows)
            await asyncio.to_thread(self.store.apply, order["id"], row)
        p = await self._get(p["id"])
        known = {o["broker_id"] for o in p["orders"] if o["broker_id"]}
        # Protect current holdings and today's rotation. A stock sold on an
        # earlier day can later be traded manually without blocking this portfolio.
        today = live.now_ist().date()
        instruments = {
            o["isin"]: {"isin": o["isin"], "symbol": o["symbol"], "key": o["instrument_key"]}
            for o in p["orders"]
            if o["status"] not in TERMINAL
            or datetime.fromisoformat(o["created_at"].replace("Z", "+00:00")).astimezone(upstox.IST).date()
            == today
        }
        if p["position"]:
            h = p["position"]
            instruments[h["isin"]] = {"isin": h["isin"], "symbol": h["symbol"], "key": h["instrument_key"]}
        for inst in instruments.values():
            expected = p["position"]["qty"] if p["position"] and p["position"]["isin"] == inst["isin"] else 0
            execution.ownership(evidence, inst, expected, known)
        return p

    async def reconcile(self, user_id, broker):
        await self.snapshot(user_id)
        pid = f"{user_id}:{broker}"
        async with self._lock(pid):
            p = await self._get(pid)
            if broker != "paper":
                evidence = await execution.stock_snapshot(await self._creds(p))
                p = await self._reconcile(p, evidence)
                await self._sync_fees(p, evidence["orders"])
            else:
                for order in p["orders"]:
                    if order["status"] not in TERMINAL:
                        await asyncio.to_thread(
                            self.store.apply,
                            order["id"],
                            {
                                "order_id": "paper-" + order["id"],
                                "status": "cancelled",
                                "filled_qty": order["filled_qty"],
                                "notional": order["notional"],
                            },
                        )
            await asyncio.to_thread(self.store.recovered, pid)
            self._fenced.discard(pid)
        return await self.snapshot(user_id)

    def preview_status(self):
        if not self._ranking_task:
            return {"status": "idle"}
        if not self._ranking_task.done():
            return {"status": "running"}
        try:
            result = self._ranking_task.result()
            if result["failed"]:
                return {"status": "error", "error": "Incomplete price history; trading decisions are blocked"}
            return {"status": "done", "ranking_date": result["date"], "top": result["ranking"][:10]}
        except (Exception, asyncio.CancelledError):
            return {
                "status": "error",
                "error": "Ranking refresh failed. Check data availability and refresh again.",
            }

    async def preview(self, user_id):
        if self._ranking_task and not self._ranking_task.done():
            return self.preview_status()
        self._ranking_day = None
        await self._ranking(upstox.today_ist())
        return self.preview_status()

    @staticmethod
    def settings(today):
        return momentum.Settings(
            start=today - timedelta(days=60),
            end=today,
            capital=50_000,
            cost_pct=0.1,
            score="tuned",
            **momentum.TUNED,
        )

    async def _ranking(self, today):
        # Research/download work never holds up the reconciliation loop.
        if self._ranking_day != today:
            if self._ranking_task and not self._ranking_task.done():
                self._ranking_task.cancel()
            self._ranking_day = today
            self._ranking_task = asyncio.create_task(
                self.app.state.momentum.live_ranking(self.settings(today)), name="momentum-ranking"
            )
            return None
        if self._ranking_task is None or not self._ranking_task.done():
            return None
        result = self._ranking_task.result()
        if result["failed"] or not result["ranking"]:
            raise PortfolioError("Incomplete ranking: no orders until all price downloads succeed")
        ranking_date = date.fromisoformat(result["date"])
        if ranking_date >= today or (today - ranking_date).days > 7:
            raise PortfolioError("Ranking date is stale or not a completed session")
        self.last_error = None
        return result

    async def _price(self, key):
        body = await asyncio.wait_for(upstox._get("/v2/market-quote/quotes", {"instrument_key": key}), 10)
        rows = [r for r in body.get("data", {}).values() if r.get("instrument_token") == key]
        if len(rows) != 1:
            raise PortfolioError("Stock quote is unavailable")
        quote = rows[0]
        stamp = float(quote.get("last_trade_time") or 0) / 1000
        age = live.now_ist().timestamp() - stamp
        if not -5 <= age <= 120:
            raise PortfolioError("Stock quote is stale; no order submitted")
        price = paise(quote["last_price"])
        if price <= 0:
            raise PortfolioError("A positive stock quote is required")
        return price

    async def _tick_size(self, key):
        instruments = await self.app.state.momentum.store.instruments()
        row = next((r for r in instruments if r["key"] == key), None)
        tick = quantity(row.get("tick_size")) if row else 0
        if tick <= 0:
            raise PortfolioError("Exchange tick size is unavailable")
        return tick

    async def _execute(self, p, plan, side, instrument, evidence=None, creds=None, parent_id=None):
        if p["id"] in self._fenced:
            raise PortfolioError("Storage/submission uncertainty requires explicit reconciliation")
        inst = {
            "symbol": instrument["symbol"],
            "key": instrument.get("key") or instrument["instrument_key"],
            "isin": instrument.get("isin") or instrument["key"].split("|", 1)[1],
        }
        price = await self._price(inst["key"])
        # Limit orders cap purchase price and respect the current exchange tick.
        tick = await self._tick_size(inst["key"])
        limit = (
            ((price * 1005 + 1000 * tick - 1) // (1000 * tick)) * tick
            if side == "BUY"
            else max(tick, (price * 995 // (1000 * tick)) * tick)
        )
        budget = min(p["cash"], p["usable_capital"])
        if evidence:
            known = {o["broker_id"] for o in p["orders"] if o["broker_id"]}
            execution.ownership(evidence, inst, p["position"]["qty"] if side == "SELL" else 0, known)
            budget = min(budget, evidence["cash"])
        qty = p["position"]["qty"] if side == "SELL" else buy_quantity(budget, limit)
        if qty <= 0:
            raise PortfolioError("Allocated cash cannot fund one share plus the cost provision")
        # Shared gate serializes reservations with the user's global pause control.
        async with self.app.state.trading._config_lock:
            order = await asyncio.to_thread(
                self.store.reserve, p["id"], plan["month"], side, inst, qty, limit, p["client_id"], parent_id
            )
        if not order:
            return
        self._fenced.add(p["id"])
        if p["broker"] == "paper":
            await asyncio.to_thread(
                self.store.apply,
                order["id"],
                {
                    "order_id": "paper-" + order["id"],
                    "status": "complete",
                    "filled_qty": qty,
                    "notional": qty * price,
                },
            )
            self._fenced.discard(p["id"])
            return
        try:
            oid = await execution.place_stock_limit(creds, order)
            await asyncio.to_thread(self.store.submission, order["id"], oid)
        except asyncio.CancelledError:
            await asyncio.shield(
                asyncio.to_thread(
                    self.store.submission,
                    order["id"],
                    error="Interrupted submission; reconcile before trading",
                )
            )
            raise
        except Exception:
            # Broker call and durable acknowledgement are one uncertainty boundary.
            # A failed DB write must not cause a second submission.
            await asyncio.to_thread(
                self.store.submission,
                order["id"],
                error="Submission uncertain; reconcile the unique order reference",
            )
            raise
        else:
            self._fenced.discard(p["id"])

    async def queue_recovery(self, user_id, body):
        await self.snapshot(user_id)
        pid = f"{user_id}:{body.broker}"
        async with self._lock(pid):
            p = await self._get(pid)
            if body.broker != "paper":
                evidence = await execution.stock_snapshot(await self._creds(p))
                p = await self._reconcile(p, evidence)
            await asyncio.to_thread(
                self.store.queue_recovery,
                pid,
                body.revision,
                body.order_id,
                (live.now_ist() + timedelta(days=7)).isoformat(),
            )
            self._fenced.discard(pid)
        return await self.snapshot(user_id)

    async def cancel_recovery(self, user_id, body):
        pid = f"{user_id}:{body.broker}"
        async with self._lock(pid):
            await asyncio.to_thread(self.store.cancel_recovery, pid, body.revision, body.recovery_id)
        return await self.snapshot(user_id)

    async def statement_preview(self, user_id, body):
        await self.snapshot(user_id)
        pid = f"{user_id}:{body.broker}"
        async with self._lock(pid):
            p = await self._get(pid)
            evidence = await execution.stock_snapshot(await self._creds(p))
            return await asyncio.to_thread(
                self.reconciliation.preview,
                user_id,
                body.broker,
                body.reference,
                body.csv_text,
                evidence["client_id"],
                live.now_ist(),
            )

    async def statement_apply(self, user_id, body):
        # Use the same lock order as the scanner/reconciliation. No broker calls or orders here.
        trading = self.app.state.trading
        async with trading._scan_lock, trading._fills_lock:
            with self.store.db.connect() as c:
                doc = c.execute(
                    "SELECT broker FROM reconciliation_documents WHERE id=? AND user_id=?",
                    (body.document_id, user_id),
                ).fetchone()
            if not doc:
                raise PortfolioError("Statement preview not found")
            pid = f"{user_id}:{doc['broker']}"
            async with self._lock(pid), trading._config_lock:
                result = await asyncio.to_thread(
                    self.reconciliation.apply, user_id, body.document_id, live.now_ist()
                )
                saved = await asyncio.to_thread(trading.repository.state)
                trading.state["orders"][pid] = saved["orders"].get(pid, {})
                trading._fills_cache.pop(pid, None)
                # An explicit broker reconcile still verifies current ownership before entries resume.
                return result

    async def _target(self, p, now, ranking):
        if ranking is None:
            raise PortfolioError("Current strategy entry waits for a complete fresh ranking")
        source = p["entry_source"]
        if source == "paper":
            paper = await self._get(f"{p['user_id']}:paper")
            if paper["blocked"] or any(o["status"] not in TERMINAL for o in paper["orders"]):
                raise PortfolioError("Paper portfolio must finish its rotation before live alignment")
            plan = paper["plan"] or {}
            if plan.get("month") != now.strftime("%Y-%m") or plan.get("skip"):
                raise PortfolioError("Paper portfolio has no current monthly decision")
            holding = paper["position"]
            if (
                not holding
                and plan.get("buy")
                and not any(
                    o["cycle"] == plan["month"] and o["side"] == "BUY" and o["status"] in TERMINAL
                    for o in paper["orders"]
                )
            ):
                raise PortfolioError("Paper entry is still due; live alignment will wait")
            if holding and plan.get("sell") and holding["symbol"] == plan["sell"]["symbol"]:
                raise PortfolioError("Paper exit is still due; live alignment will wait")
        else:
            result = await asyncio.to_thread(self.app.state.momentum.run, source.split(":", 1)[1])
            if not result or result.get("settings", {}).get("score") != "tuned":
                raise PortfolioError("Saved tuned backtest is unavailable")
            settings = result["settings"]
            if any(settings.get(key) != value for key, value in momentum.TUNED.items()):
                raise PortfolioError("Backtest parameters do not match the live tuned strategy")
            data = result.get("data", {})
            if data.get("failed") != 0 or data.get("through") != ranking["date"]:
                raise PortfolioError(
                    "Refresh this saved backtest through the latest completed session before alignment"
                )
            holdings = result.get("holdings", [])
            if len(holdings) > 1:
                raise PortfolioError("A single-holding backtest is required")
            holding = holdings[0] if holdings else None
        if not holding:
            return None
        target = next((r for r in ranking["ranking"] if r["symbol"] == holding["symbol"]), None)
        if target is None:
            raise PortfolioError("Current strategy holding is absent from the verified ranking")
        if source.startswith("backtest:") and ranking["date"][:7] != now.strftime("%Y-%m"):
            sell, buy, _ = decide(ranking["ranking"], target["symbol"], momentum.TUNED["exit_rank"])
            if sell:
                target = buy
        return target

    async def _portfolio_tick(self, pid, now, ranking):
        async with self._lock(pid):
            p = await self._get(pid)
            evidence = creds = None
            if p["broker"] != "paper" and (
                p["enabled"] or p["position"] or any(o["status"] not in TERMINAL for o in p["orders"])
            ):
                creds = await self._creds(p)
                evidence = await asyncio.wait_for(execution.stock_snapshot(creds), 45)
                p = await self._reconcile(p, evidence)
            if p["broker"] != "paper":
                p = await self._sync_fees(p, (evidence or {}).get("orders", []))
            await asyncio.to_thread(self.store.health, pid)
            if (
                not p["enabled"]
                or p["blocked"]
                or pid in self._fenced
                or now.date() < START
                or not live.market_open(now)
                or self._market_day != now.date()
            ):
                return
            if any(o["status"] not in TERMINAL for o in p["orders"]):
                return
            month, hhmm = now.strftime("%Y-%m"), now.strftime("%H:%M")
            for recovery in p["recoveries"]:
                if recovery["status"] != "queued":
                    continue
                if datetime.fromisoformat(recovery["expires_at"]) < now:
                    await asyncio.to_thread(self.store.recovery_status, recovery["id"], "expired")
                    continue
                if "09:30" <= hhmm < "15:00":
                    parent = next(o for o in p["orders"] if o["id"] == recovery["parent_id"])
                    if p["position"]:
                        await self._execute(
                            p,
                            {"month": "recovery:" + recovery["id"]},
                            "SELL",
                            p["position"],
                            evidence,
                            creds,
                            parent["id"],
                        )
                    await asyncio.to_thread(
                        self.store.recovery_status,
                        recovery["id"],
                        "submitted" if p["position"] else "resolved",
                    )
                return
            if p["alignment_pending"] and not p["position"] and not p["paused"] and "09:30" <= hhmm < "15:00":
                if p["broker"] != "paper" and self.app.state.trading.state["users"].get(p["user_id"], {}).get(
                    "paused"
                ):
                    return
                target = await self._target(p, now, ranking)
                plan = {
                    "month": month,
                    "day": str(now.date()),
                    "buy": target,
                    "sell": None,
                    "reason": "Align with " + p["entry_source"],
                    "ranking_date": ranking["date"],
                    "alignment": True,
                }
                await asyncio.to_thread(self.store.plan, pid, plan)
                if target:
                    await self._execute(p, plan, "BUY", target, evidence, creds)
                await asyncio.to_thread(self.store.alignment_done, pid)
                return
            plan = p["plan"]
            if not plan or plan["month"] != month:
                if ranking is None or hhmm < "09:30":
                    return
                if ranking["date"][:7] == month:
                    await asyncio.to_thread(
                        self.store.plan,
                        pid,
                        {
                            "month": month,
                            "day": str(now.date()),
                            "reason": "First session missed; wait until next month",
                            "skip": True,
                        },
                    )
                    return
                sell, buy, reason = decide(
                    ranking["ranking"], (p["position"] or {}).get("symbol"), momentum.TUNED["exit_rank"]
                )
                plan = {
                    "month": month,
                    "day": str(now.date()),
                    "ranking_date": ranking["date"],
                    "sell": sell,
                    "buy": buy,
                    "reason": reason,
                }
                await asyncio.to_thread(self.store.plan, pid, plan)
            if plan.get("skip") or plan["day"] != str(now.date()) or hhmm >= "15:00":
                return
            cycle_orders = [o for o in p["orders"] if (o["origin_cycle"] or o["cycle"]) == month]
            parents = {o["parent_id"] for o in cycle_orders if o["parent_id"]}
            if any(o["status"] in ("cancelled", "rejected") and o["id"] not in parents for o in cycle_orders):
                raise PortfolioError(
                    "A rotation order was cancelled/rejected; review it before the next cycle"
                )
            if plan.get("sell") and p["position"] and hhmm >= "11:00":
                await self._execute(p, plan, "SELL", p["position"], evidence, creds)
            elif plan.get("buy") and not p["position"] and hhmm >= "12:00" and not p["paused"]:
                if p["broker"] != "paper" and self.app.state.trading.state["users"].get(p["user_id"], {}).get(
                    "paused"
                ):
                    return
                if plan.get("sell") and not any(
                    o["side"] == "SELL" and o["status"] == "complete" for o in cycle_orders
                ):
                    raise PortfolioError("The previous holding has not completed its exit")
                await self._execute(p, plan, "BUY", plan["buy"], evidence, creds)

    async def tick(self, now=None):
        now = now or live.now_ist()
        ranking = None
        if now.date() >= START and live.market_open(now) and now.strftime("%H:%M") >= "09:30":
            if self._market_day != now.date():
                try:
                    rows = await asyncio.wait_for(
                        upstox.candles(upstox.NIFTY, "1m", now.date(), now.date(), expired=False), 20
                    )
                    if rows:
                        self._market_day = now.date()
                except Exception as exc:
                    # Market-data outages must not prevent broker fill reconciliation.
                    self.last_error = "Market session could not be verified: " + (
                        redact(str(exc)) or "request timed out"
                    )
                    log.warning("Momentum market data unavailable: %s", self.last_error)
            if self._market_day == now.date():
                try:
                    ranking = await self._ranking(now.date())
                except Exception as exc:
                    self.last_error = redact(str(exc)) or "Ranking unavailable"
                    log.warning("Momentum ranking unavailable: %s", self.last_error)
        portfolios = await asyncio.to_thread(self.store.snapshot)

        async def one(p):
            try:
                await asyncio.wait_for(self._portfolio_tick(p["id"], now, ranking), 60)
            except Exception as exc:
                reason = redact(str(exc)) or "Execution evidence unavailable; reconciliation required"
                log.warning("Stock portfolio cycle failed", extra={"portfolio": p["id"]}, exc_info=True)
                await asyncio.to_thread(self.store.health, p["id"], reason)
                if (
                    any(o["status"] not in TERMINAL for o in p["orders"])
                    or p["id"] in self._fenced
                    or any(
                        x in reason.lower()
                        for x in ("ownership", "manual", "different", "cancelled/rejected")
                    )
                ):
                    await asyncio.to_thread(self.store.block, p["id"], reason)

        await asyncio.gather(*(one(p) for p in portfolios))
        self.last_cycle = time.monotonic()

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except Exception:
                self.last_error = "Momentum worker cycle failed; see operational logs"
                log.exception("Momentum worker cycle failed")
            await asyncio.sleep(20)
