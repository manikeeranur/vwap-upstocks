"""Compounding strategies: the same trades as their base strategy, with lots that grow with profit.

Each starts on START with its own capital (₹1,50,000). After every trading day's close its paper
P&L (that day's trades × the lots it traded, after charges) is added to its capital. On the first
trading day of each month (from 09:05) its lots are re-sized on every broker account — paper and
live alike — to capital ÷ capital-per-lot, between MIN_LOTS and MAX_LOTS (larger orders fill worse,
so it stops at 10). Lots typed in by hand for these strategies are replaced at the next re-size.
Money is withdrawn in the broker's own app, which this app doesn't see: only capital above MAX_LOTS ×
capital-per-lot ("safe_withdraw") can be taken out without the lots outgrowing the account.
"""
import asyncio
from datetime import date
import logging

from .. import live
from ..pdf_report import charges   # the same fee formula as the backtest pages

log = logging.getLogger(__name__)

KEY = "compounding"
START = date(2026, 11, 2)
MIN_LOTS, MAX_LOTS = 1, 10
RESIZE_AT, BOOK_AT = "09:05", "15:35"
# capital per lot ≈ the highest premium of one lot + its 2-year max drawdown (rounded).
COMPOUNDING = {"harmonic_2y_compound": {"start_capital": 150_000, "per_lot": 75_000},
               "pdh_compound": {"start_capital": 150_000, "per_lot": 36_000},
               "vwap_touch_compound": {"start_capital": 150_000, "per_lot": 56_000}}


def lots_for(capital: float, per_lot: float) -> int:
    return max(MIN_LOTS, min(MAX_LOTS, int(capital // per_lot)))


def default_state() -> dict:
    return {p: {"capital": float(c["start_capital"]), "lots": lots_for(c["start_capital"], c["per_lot"]),
                "month": None, "booked": [], "history": []} for p, c in COMPOUNDING.items()}


class CompoundingService:
    def __init__(self, app):
        self.app = app
        self._task = None
        self.state = default_state()

    def load(self):
        saved = self.app.state.repository.get_json(KEY) or {}
        self.state = {p: {**v, **saved.get(p, {})} for p, v in default_state().items()}

    async def _save(self):
        await asyncio.to_thread(self.app.state.repository.set_json, KEY, self.state)

    def snapshot(self) -> dict:
        def one(p, s):
            return {**COMPOUNDING[p], **{k: v for k, v in s.items() if k not in ("booked", "history")},
                    "history": s["history"][-30:],
                    "safe_withdraw": round(max(0.0, s["capital"] - COMPOUNDING[p]["per_lot"] * MAX_LOTS), 2)}
        return {"start": START.isoformat(), "max_lots": MAX_LOTS, "strategies": {p: one(p, s) for p, s in self.state.items()}}

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="compounding")

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self.tick()
            except Exception:
                log.exception("Compounding tick failed")

    async def tick(self, now=None):
        now = now or live.now_ist()
        if now.date() < START:
            return
        t, month = now.strftime("%H:%M"), now.strftime("%Y-%m")
        trading = self.app.state.trading
        for preset, cfg in COMPOUNDING.items():
            s = self.state[preset]
            if now.weekday() < 5 and t >= RESIZE_AT and s["month"] != month:
                s["lots"], s["month"] = lots_for(s["capital"], cfg["per_lot"]), month
                await trading._activity(f"{live.LABELS[preset]}: capital ₹{s['capital']:,.2f} → {s['lots']} lot(s) for {now:%b %Y}")
                await self._save()
            # Every account (paper and live) trades the current lots — also one added or edited mid-month.
            for acc in list(trading.state["accounts"].values()):
                if acc["lots"].get(preset) != s["lots"]:
                    await trading.configure(preset, user=acc["user"], broker=acc["broker"], lots=s["lots"])
        await self._book(now)

    async def _book(self, now):
        """Add each finished trading day's paper P&L (from the saved journal, so a replay or a restart can't
        lose it) to the capital, once. Today counts from BOOK_AT."""
        today, t = now.date().isoformat(), now.strftime("%H:%M")
        days = (await asyncio.to_thread(self.app.state.repository.journal, START.isoformat(), today)).get("days", {})
        lot_size, changed = self.app.state.trading.state["lot_size"], False
        for day in sorted(days):
            if day == today and t < BOOK_AT:
                continue
            for preset in COMPOUNDING:
                s = self.state[preset]
                if day in s["booked"]:
                    continue
                qty = s["lots"] * lot_size
                closed = [x for x in (days[day].get(preset) or {}).get("trades", []) if not x.get("open")]
                net = sum(x["pnl"] * qty - charges(x["entry"], x["exit"], qty) for x in closed)
                s["capital"] = round(s["capital"] + net, 2)
                s["booked"] = (s["booked"] + [day])[-400:]
                changed = True
                if closed:
                    s["history"] = (s["history"] + [{"day": day, "trades": len(closed), "lots": s["lots"],
                                                     "net": round(net, 2), "capital": s["capital"]}])[-400:]
                    await self.app.state.trading._activity(f"{live.LABELS[preset]}: {day} paper {net:+,.2f} at {s['lots']} lot(s) → capital ₹{s['capital']:,.2f}")
        if changed:
            await self._save()
