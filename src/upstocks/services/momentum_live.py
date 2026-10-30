"""Momentum Tuned — the Momentum page's "tuned" rotation, run every month from 2026-11-01.

- One holding, ₹50,000 to start, compounding: each sale's money buys the next stock.
- Decision on the first trading day of each month, from the ranking after the previous month's
  last close (12-1 momentum among the 1,000 most-traded NSE stocks): keep the holding while it is
  rank ≤ 5 and above its 150-day average; otherwise sell it at 11:00 and, at 12:00, buy the
  best-ranked stock above its average (sell first, so the sale pays for the buy).
- Paper always runs (fills at the last traded price then, 0.1 % cost a side, like the backtest).
- Each broker has its own live switch. Live orders are delivery (CNC) MARKET orders for the paper
  quantity, each saved before it is sent and never retried. A live sell is only sent for shares
  this strategy bought at that broker and still held there; switching live on mid-holding starts
  with the next buy.
- No trade on a market holiday, or when the month's first trading day was missed.
"""
import asyncio
from copy import deepcopy
from datetime import date, timedelta
import logging

from .. import brokers, live, momentum, upstox
from ..observability import redact

log = logging.getLogger(__name__)

KEY = "momentum_tuned"
START, CAPITAL, COST = date(2026, 11, 1), 50_000.0, 0.001
PREPARE_AT, SELL_AT, BUY_AT, GIVE_UP_AT = "09:30", "11:00", "12:00", "15:00"
TAG = "upstocksmomtuned"


def settings(today: date) -> momentum.Settings:
    return momentum.Settings(start=today - timedelta(days=60), end=today, capital=CAPITAL, cost_pct=COST * 100,
                             score="tuned", **momentum.TUNED)


def default_state() -> dict:
    return {"start": START.isoformat(), "capital": CAPITAL, "cash": CAPITAL, "holding": None,
            "live": {b: False for b in brokers.BROKERS}, "positions": {}, "month": None, "plan": None,
            "history": [], "orders": [], "log": []}


def decide(ranking: list[dict], held: str | None, exit_rank: int) -> tuple[dict | None, dict | None, str]:
    """(sell, buy, reason) for one holding — the same rules as momentum.decide."""
    by_symbol = {r["symbol"]: r for r in ranking}
    sell, reason = None, ""
    if held:
        r = by_symbol.get(held)
        if r is None:
            sell, reason = {"symbol": held, "rank": None}, "Left the universe"
        elif r["rank"] > exit_rank:
            sell, reason = {"symbol": held, "rank": r["rank"]}, f"Rank {r['rank']} worse than {exit_rank}"
        elif not r["above_sma"]:
            sell, reason = {"symbol": held, "rank": r["rank"]}, "Closed below its 150-day average"
        else:
            return None, None, f"Keep {held}: rank {r['rank']}, above its average"
    buy = next(({"symbol": r["symbol"], "key": r["key"], "rank": r["rank"]} for r in ranking
                 if r["symbol"] != held and r["above_sma"]), None)
    if buy is None:
        reason = (reason + " · " if reason else "") + "no stock above its average — stay in cash"
    return sell, buy, reason or f"Buy {buy['symbol']} (rank {buy['rank']})"


class MomentumLiveService:
    def __init__(self, app):
        self.app = app
        self._task = None
        self._lock = asyncio.Lock()   # state changes and orders, one at a time
        self.state = default_state()

    @property
    def repo(self):
        return self.app.state.repository

    def load(self):
        saved, base = self.repo.get_json(KEY) or {}, default_state()
        self.state = {**base, **{k: v for k, v in saved.items() if k in base}}

    async def _save(self):
        await asyncio.to_thread(self.repo.set_json, KEY, self.state)

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="momentum-tuned")

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def snapshot(self) -> dict:
        return deepcopy(self.state)

    async def _note(self, message, telegram=True):
        stamp = f"{live.now_ist():%Y-%m-%d %H:%M}"
        self.state["log"] = (self.state["log"] + [f"{stamp} {message}"])[-200:]
        await self.app.state.trading._activity(f"Momentum Tuned: {message}")
        if telegram:
            for u in await asyncio.to_thread(self.repo.users):
                if u.get("telegram_chat_id"):
                    await self.app.state.reminders.send(u["telegram_chat_id"], f"Momentum Tuned: {message}")

    async def configure(self, broker: str, on: bool):
        async with self._lock:
            self.state["live"][broker] = on
            await self._save()
            await self._note(f"{brokers.LABELS[broker]} LIVE {'ON' if on else 'OFF'}", telegram=False)
        return self.snapshot()

    async def preview(self) -> dict:
        """What the rules would do with the latest close (read-only)."""
        r = await self.app.state.momentum.live_ranking(settings(upstox.today_ist()))
        held = (self.state["holding"] or {}).get("symbol")
        sell, buy, reason = decide(r["ranking"], held, momentum.TUNED["exit_rank"])
        return {"ranking_date": r["date"], "sell": sell, "buy": buy, "reason": reason, "top": r["ranking"][:10],
                "held_rank": next((x for x in r["ranking"] if x["symbol"] == held), None)}

    # ── Schedule ────────────────────────────────────────────────────────────
    async def _loop(self):
        while True:
            await asyncio.sleep(20)
            try:
                await self.tick()
            except Exception as exc:
                log.exception("Momentum Tuned tick failed")
                await self._note(f"error — {redact(str(exc))} (tries again in 5 minutes)", telegram=False)
                await asyncio.sleep(300)

    async def tick(self, now=None):
        now = now or live.now_ist()
        if now.date() < date.fromisoformat(self.state["start"]) or not live.market_open(now):
            return
        month, t = now.strftime("%Y-%m"), now.strftime("%H:%M")
        plan = self.state["plan"] if self.state["month"] == month else None
        if plan is None:
            if t < PREPARE_AT:
                return
            async with self._lock:
                await self._prepare(now, month)
            return
        if plan.get("skip") or plan.get("finished"):
            return
        async with self._lock:
            if t >= GIVE_UP_AT:
                plan["finished"] = True
                await self._note(f"{now:%d-%m-%Y}: steps left undone at {GIVE_UP_AT} ({', '.join(k for k in ('sell', 'buy') if plan.get(k) and not plan.get(k + '_done'))}) — nothing more this month")
            elif plan.get("sell") and not plan.get("sell_done") and t >= SELL_AT:
                await self._sell(plan)
            elif plan.get("buy") and not plan.get("buy_done") and t >= BUY_AT and (not plan.get("sell") or plan.get("sell_done")):
                await self._buy(plan)
            if all(not plan.get(k) or plan.get(k + "_done") for k in ("sell", "buy")):
                plan["finished"] = True
            await self._save()

    async def _prepare(self, now, month):
        today = now.date()
        rows = await upstox.candles(upstox.NIFTY, "1m", today, today, expired=False)
        if not rows:
            return  # holiday or no candle yet; checked again on the next tick
        r = await self.app.state.momentum.live_ranking(settings(today))
        self.state["month"] = month
        if r["date"][:7] == month:
            self.state["plan"] = {"day": today.isoformat(), "skip": f"Not the month's first trading day (already traded {r['date']})"}
            await self._note(f"{today:%d-%m-%Y}: missed this month's first trading day — no change until next month")
        elif r["failed"] > 50:
            self.state["plan"] = {"day": today.isoformat(), "skip": f"{r['failed']} stocks' prices failed to download"}
            await self._note(f"{today:%d-%m-%Y}: price download incomplete ({r['failed']} failed) — no change this month")
        else:
            held = (self.state["holding"] or {}).get("symbol")
            sell, buy, reason = decide(r["ranking"], held, momentum.TUNED["exit_rank"])
            self.state["plan"] = {"day": today.isoformat(), "ranking_date": r["date"], "sell": sell, "buy": buy,
                                  "reason": reason, "top": r["ranking"][:5]}
            top = ", ".join(f"{x['rank']}. {x['symbol']}" for x in r["ranking"][:5])
            steps = (f"sell {sell['symbol']} at {SELL_AT}" if sell else "") + (" · " if sell and buy else "") + \
                    (f"buy {buy['symbol']} at {BUY_AT}" if buy else "")
            await self._note(f"{today:%d-%m-%Y} decision (close of {date.fromisoformat(r['date']):%d-%m-%Y}): {reason}"
                             f"{' — ' + steps if steps else ''}. Top 5: {top}")
        await self._save()

    async def _ltp(self, key):
        quotes = await upstox.ltp_quotes([key])
        px = quotes.get(key)
        if not px:
            raise RuntimeError(f"No last price for {key}")
        return float(px)

    # ── Steps ───────────────────────────────────────────────────────────────
    async def _sell(self, plan):
        h = self.state["holding"]
        px = await self._ltp(h["key"])
        gross = h["qty"] * px
        self.state["cash"] = round(self.state["cash"] + gross * (1 - COST), 2)
        pnl = gross * (1 - COST) - h["cost"]
        self.state["history"].append({**h, "exit_date": plan["day"], "exit_time": SELL_AT, "exit": round(px, 2),
                                      "pnl": round(pnl, 2), "pnl_pct": round(pnl / h["cost"] * 100, 2),
                                      "reason": plan["reason"], "cash_after": self.state["cash"]})
        self.state["holding"] = None
        plan["sell_done"] = True
        await self._save()
        await self._note(f"paper SELL {h['qty']} {h['symbol']} @ ₹{px:,.2f} ({pnl:+,.2f}) — cash ₹{self.state['cash']:,.2f}")
        for broker, on in self.state["live"].items():
            pos = self.state["positions"].get(broker)
            if not pos or pos["symbol"] != h["symbol"]:
                continue   # nothing this strategy bought there (live switched on later, or already sold)
            creds = self._creds(broker)
            if creds is None:
                await self._note(f"{brokers.LABELS[broker]}: SELL {h['symbol']} skipped — not logged in today; sell it yourself")
                continue
            try:
                held = await brokers.equity_held(creds, pos["isin"])
            except Exception as exc:
                await self._note(f"{brokers.LABELS[broker]}: SELL {h['symbol']} skipped — holdings check failed: {redact(str(exc))}")
                continue
            qty = min(pos["qty"], held)
            if qty <= 0:
                await self._note(f"{brokers.LABELS[broker]}: SELL {h['symbol']} skipped — no shares held there")
                self.state["positions"].pop(broker, None)
                continue
            if await self._order(broker, creds, "SELL", pos["symbol"], pos["isin"], qty):
                self.state["positions"].pop(broker, None)
            await self._save()

    async def _buy(self, plan):
        b = plan["buy"]
        px = await self._ltp(b["key"])
        qty = int(self.state["cash"] / (px * (1 + COST)))
        plan["buy_done"] = True
        if qty <= 0:
            await self._note(f"BUY {b['symbol']} skipped — ₹{self.state['cash']:,.2f} is less than one share at ₹{px:,.2f}")
            return
        cost = qty * px * (1 + COST)
        self.state["cash"] = round(self.state["cash"] - cost, 2)
        isin = b["key"].split("|", 1)[1]
        self.state["holding"] = {"symbol": b["symbol"], "key": b["key"], "isin": isin, "qty": qty, "entry": round(px, 2),
                                 "entry_date": plan["day"], "entry_time": BUY_AT, "rank": b["rank"], "cost": round(cost, 2)}
        await self._save()
        await self._note(f"paper BUY {qty} {b['symbol']} @ ₹{px:,.2f} (rank {b['rank']}) — ₹{cost:,.2f}")
        for broker, on in self.state["live"].items():
            if not on:
                continue
            creds = self._creds(broker)
            if creds is None:
                await self._note(f"{brokers.LABELS[broker]}: BUY {b['symbol']} skipped — not logged in today")
                continue
            if await self._order(broker, creds, "BUY", b["symbol"], isin, qty):
                self.state["positions"][broker] = {"symbol": b["symbol"], "isin": isin, "qty": qty, "since": plan["day"]}
            await self._save()

    def _creds(self, broker):
        trading = self.app.state.trading
        for acc_id, acc in trading.state["accounts"].items():
            if acc["broker"] == broker:
                creds = trading._credentials(acc_id)
                return creds if creds.token_valid(live.now_ist()) else None
        return None

    async def _order(self, broker, creds, side, symbol, isin, qty) -> bool:
        """Save the order first, send it once; an unknown outcome is never retried."""
        rec = {"at": f"{live.now_ist():%Y-%m-%d %H:%M:%S}", "broker": broker, "side": side, "symbol": symbol,
               "isin": isin, "qty": qty, "status": "pending"}
        self.state["orders"].append(rec)
        await self._save()
        try:
            rec["order_id"] = await brokers.place_equity_order(creds, symbol, isin, qty, side, TAG)
            if not rec["order_id"]:
                raise RuntimeError("Broker did not return an order ID")
            rec["status"] = "submitted"
            await self._note(f"{brokers.LABELS[broker]} LIVE {side} {qty} {symbol} → order {rec['order_id']}")
        except asyncio.CancelledError:
            rec.update(status="unknown", error="Interrupted — check the order with the broker; not retried")
            await self._save()
            raise
        except Exception as exc:
            rec.update(status="unknown", error=redact(str(exc)))
            await self._note(f"{brokers.LABELS[broker]} LIVE {side} {qty} {symbol} needs review: {rec['error']}")
        await self._save()
        return rec["status"] == "submitted"

