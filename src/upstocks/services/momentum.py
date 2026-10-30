"""Momentum backtest job: NSE equity daily-price cache and one background run at a time.

Each stock's daily candles live in `<cache>/equity/<ISIN>.npz`. The first run
downloads every NSE EQ stock (one Upstox request each, ~2,700 — Upstox's
2,000/30-min budget makes that 30–45 minutes). Later runs only extend stale
files: stocks that matter (top `ACTIVE` by recent turnover) or that are more
than `STALE_DAYS` behind. Upstox serves adjusted prices, so when an update's
overlap day disagrees with the cached close (a split or bonus since), that
stock is downloaded again in full.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime, timedelta
import gzip
import json
import logging
from pathlib import Path
import re
import time
import uuid

import httpx
import numpy as np

from .. import momentum, upstox
from ..observability import redact

log = logging.getLogger(__name__)

INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
BENCHMARK = "NSE_INDEX|Nifty 50"
ACTIVE, STALE_DAYS = 1000, 45
MAX_RUNS = 50
RUN_ID = re.compile(r"^[0-9a-f]{12}$")
# While the market is open the live scanner shares the Upstox budget: bulk
# downloads then use at most this many requests per window.
MARKET_CAPS = ((60.0, 200), (1800.0, 900))


class MomentumBusy(Exception):
    pass


def _market_hours() -> bool:
    now = datetime.now(upstox.IST)
    return now.weekday() < 5 and (9, 0) <= (now.hour, now.minute) <= (15, 45)


class _BulkLimiter:
    def __init__(self):
        self.starts: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            while True:
                now = time.monotonic()
                while self.starts and now - self.starts[0] > MARKET_CAPS[-1][0]:
                    self.starts.popleft()
                wait = 0.0
                if _market_hours():
                    for span, cap in MARKET_CAPS:
                        inside = [t for t in self.starts if now - t < span]
                        if len(inside) >= cap:
                            wait = max(wait, inside[0] + span - now + 0.05)
                if wait <= 0:
                    break
                if wait > 2:
                    upstox.throttled_until = max(upstox.throttled_until, time.time() + wait)
                await asyncio.sleep(min(wait, 5.0))
            self.starts.append(time.monotonic())


class PriceStore:
    def __init__(self, cache_dir: Path):
        self.dir = cache_dir / "equity"
        self.limiter = _BulkLimiter()

    def _path(self, key: str) -> Path:
        return self.dir / (key.split("|", 1)[1].replace(" ", "_") + ".npz")

    async def instruments(self) -> list[dict]:
        """NSE EQ-series stocks from Upstox's public instrument file (refreshed daily)."""
        path = self.dir / "instruments.json"
        if not path.exists() or time.time() - path.stat().st_mtime > 86400:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.get(INSTRUMENTS_URL)
                r.raise_for_status()
            rows = json.loads(gzip.decompress(r.content))
            eq = sorted(({"key": x["instrument_key"], "symbol": x["trading_symbol"], "name": x.get("name", "")}
                         for x in rows if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"),
                        key=lambda x: x["symbol"])
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(eq))
            tmp.replace(path)
        # ETFs also trade in the EQ series; company shares have INE… ISINs, fund units INF….
        return [x for x in json.loads(path.read_text()) if x["key"].startswith("NSE_EQ|INE")]

    def load(self, key: str) -> dict | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            with np.load(path) as z:
                return {k: z[k] for k in z.files}
        except (OSError, ValueError):
            log.warning("Ignoring unreadable price cache %s", path.name)
            return None

    def _save(self, key: str, rows: list[list], frm: date, thru: date):
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        tmp = path.with_name(path.stem + ".tmp.npz")
        np.savez_compressed(tmp, d=np.array([date.fromisoformat(r[0][:10]).toordinal() for r in rows], dtype=np.int32),
                            o=np.array([r[1] for r in rows], dtype=np.float32),
                            c=np.array([r[4] for r in rows], dtype=np.float32),
                            v=np.array([r[5] for r in rows], dtype=np.float32),
                            frm=np.int32(frm.toordinal()), thru=np.int32(thru.toordinal()))
        tmp.replace(path)

    @staticmethod
    def _thru(rows: list[list], end: date) -> date:
        # Upstox publishes a session's day candle some time after the close, so a
        # recent range is only "complete" up to its last row.
        if end <= upstox.today_ist() - timedelta(days=3) or not rows:
            return end
        return date.fromisoformat(rows[-1][0][:10])

    async def _fetch(self, key, start, end):
        await self.limiter.wait()
        return await upstox.daily_history(key, start, end)

    async def refresh(self, key: str, need_from: date, end: date, cached: dict | None) -> str:
        """Bring one stock's file up to `end`; returns 'full', 'update' or 'resync'."""
        if cached is not None and int(cached["frm"]) <= need_from.toordinal() and len(cached["d"]):
            last = date.fromordinal(int(cached["d"][-1]))
            new = await self._fetch(key, last, end)
            if new and new[0][0][:10] == last.isoformat() and abs(new[0][4] / cached["c"][-1] - 1) < 0.005:
                old = [[date.fromordinal(int(d)).isoformat(), o, 0, 0, c, v]
                       for d, o, c, v in zip(cached["d"][:-1], cached["o"][:-1], cached["c"][:-1], cached["v"][:-1])]
                self._save(key, old + new, date.fromordinal(int(cached["frm"])), self._thru(new, end))
                return "update"
            if not new:  # nothing traded since — keep the history, mark it checked
                self._save_checked(key, cached, end)
                return "update"
            kind = "resync"
        else:
            kind = "full"
        frm = min(need_from, date.fromordinal(int(cached["frm"]))) if cached is not None else need_from
        rows = await self._fetch(key, frm, end)
        self._save(key, rows, frm, self._thru(rows, end))
        return kind

    def _save_checked(self, key, cached, end):
        rows = [[date.fromordinal(int(d)).isoformat(), o, 0, 0, c, v]
                for d, o, c, v in zip(cached["d"], cached["o"], cached["c"], cached["v"])]
        self._save(key, rows, date.fromordinal(int(cached["frm"])), end if end <= upstox.today_ist() - timedelta(days=3)
                   else date.fromordinal(int(cached["thru"])))


def _recent_turnover(z: dict) -> float:
    c, v = z["c"][-momentum.TURNOVER_DAYS:], z["v"][-momentum.TURNOVER_DAYS:]
    return float(np.median(c * v)) if len(c) else 0.0


class MomentumService:
    def __init__(self, cache_dir: Path, runs_dir: Path):
        """Each finished run is saved as `<runs_dir>/<id>.json`, listed newest first in `index.json`."""
        self.store = PriceStore(cache_dir)
        self.runs_dir = runs_dir
        self.task: asyncio.Task | None = None
        self.job = {"status": "idle"}
        self.data_lock = asyncio.Lock()   # one price download at a time (backtest or Momentum Tuned live)
        legacy = runs_dir.parent / "momentum_last.json"  # single result kept before saved runs
        if legacy.exists() and not (runs_dir / "index.json").exists():
            try:
                self._save_run(json.loads(legacy.read_text()))
                legacy.unlink()
            except (OSError, ValueError):
                log.warning("Ignoring unreadable momentum result")

    def runs(self) -> list[dict]:
        try:
            return json.loads((self.runs_dir / "index.json").read_text())
        except (OSError, ValueError):
            return []

    def run(self, run_id: str | None = None) -> dict | None:
        """A saved run by id, or the newest one."""
        if run_id is None:
            runs = self.runs()
            if not runs:
                return None
            run_id = runs[0]["id"]
        if not RUN_ID.match(run_id):
            return None
        try:
            return json.loads((self.runs_dir / f"{run_id}.json").read_text())
        except (OSError, ValueError):
            return None

    def _write(self, path: Path, value):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value))
        tmp.replace(path)

    def _save_run(self, result: dict) -> str:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        run_id = result.setdefault("id", uuid.uuid4().hex[:12])
        self._write(self.runs_dir / f"{run_id}.json", result)
        st, s = result["settings"], result["stats"]
        entry = {"id": run_id, "finished": result.get("finished"), "start": st["start"], "end": st["end"],
                 "through": (result.get("data") or {}).get("through"), "slots": st["slots"], "capital": st["capital"], "score": st.get("score", "risk_adjusted"),
                 "cagr": s.get("cagr"), "max_dd": s.get("max_dd"), "final": s.get("final")}
        runs = [entry] + [r for r in self.runs() if r["id"] != run_id]
        for old in runs[MAX_RUNS:]:
            (self.runs_dir / f"{old['id']}.json").unlink(missing_ok=True)
        self._write(self.runs_dir / "index.json", runs[:MAX_RUNS])
        return run_id

    def snapshot(self) -> dict:
        job = dict(self.job)
        if job.get("status") == "running" and upstox.throttled_until > time.time():
            job["waiting_until"] = datetime.fromtimestamp(upstox.throttled_until, upstox.IST).strftime("%H:%M")
        return job

    async def submit(self, settings: momentum.Settings, refresh_all: bool = False):
        if self.task and not self.task.done():
            raise MomentumBusy("A momentum backtest is already running.")
        self.job = {"status": "running", "phase": "Loading the stock list", "done": 0, "total": 0,
                    "started": datetime.now(upstox.IST).isoformat(timespec="seconds")}
        self.task = asyncio.create_task(self._run(settings, refresh_all), name="momentum-backtest")

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _prices(self, need_from: date, end: date, refresh_all: bool, job: dict):
        """Every stock's cached daily prices, brought up to the latest published day (see the module note).
        Returns (instruments, {key: arrays}, downloaded keys); progress goes into `job`."""
        fetch_end = upstox.today_ist() - timedelta(days=1)
        instruments = await self.store.instruments()
        keys = [BENCHMARK] + [x["key"] for x in instruments]
        job.update(phase="Checking cached prices", total=len(keys))
        cached = await asyncio.to_thread(lambda: {k: self.store.load(k) for k in keys})
        active = sorted((k for k, z in cached.items() if z is not None and len(z["d"])),
                        key=lambda k: -_recent_turnover(cached[k]))[:ACTIVE]
        # NIFTY's latest candle is the latest trading day Upstox has published; a stock whose
        # data reaches it is current (weekends, holidays and early mornings need no download).
        await self.store.refresh(BENCHMARK, need_from, fetch_end, cached[BENCHMARK])
        cached[BENCHMARK] = await asyncio.to_thread(self.store.load, BENCHMARK)
        latest = min(end.toordinal(), int(cached[BENCHMARK]["d"][-1]))
        todo = []
        for k in keys[1:]:
            z = cached[k]
            if z is None or int(z["frm"]) > need_from.toordinal():
                todo.append(k)
            elif max(int(z["thru"]), int(z["d"][-1]) if len(z["d"]) else 0) < latest and (
                    refresh_all or k in active or int(z["thru"]) < latest - STALE_DAYS):
                todo.append(k)
        job.update(phase="Downloading daily prices from Upstox" if todo else "Prices up to date",
                   done=0, total=len(todo), failed=[])
        sem = asyncio.Semaphore(6)

        async def one(k):
            async with sem:
                try:
                    await self.store.refresh(k, need_from, fetch_end, cached[k])
                    cached[k] = await asyncio.to_thread(self.store.load, k)
                except upstox.UpstoxError as exc:
                    job["failed"].append(k)
                    log.warning("Price download failed for %s: %s", k, redact(str(exc)))
                job["done"] += 1

        await asyncio.gather(*(one(k) for k in todo))
        return instruments, cached, todo

    async def _run(self, st: momentum.Settings, refresh_all: bool):
        try:
            today = upstox.today_ist()
            end = min(st.end, today - timedelta(days=1))
            need_from = momentum.history_start(st.start)
            async with self.data_lock:
                instruments, cached, todo = await self._prices(need_from, end, refresh_all, self.job)
            self.job.update(phase="Running the backtest", done=0, total=0)
            symbols = {x["key"]: x["symbol"] for x in instruments}
            result = await asyncio.to_thread(self._compute, cached, symbols, st, need_from, end)
            if st.score == "tuned":   # Momentum Tuned's real switch-day times: sell 11:00, then buy 12:00
                self.job.update(phase="Fetching 11:00 / 12:00 prices for the switch days")
                fills = await self._switch_prices(result, instruments)
                result = await asyncio.to_thread(self._compute, cached, symbols, st, need_from, end, fills)
            self.job.update(phase="Fetching each trade's high and low")
            await self._peaks(result, instruments)
            result["data"] = {"stocks": len(instruments), "downloaded": len(todo), "failed": len(self.job["failed"]),
                              "through": result["equity"][-1][0] if result["equity"] else None}
            result["finished"] = datetime.now(upstox.IST).isoformat(timespec="seconds")
            run_id = await asyncio.to_thread(self._save_run, result)
            self.job.update(status="done", phase="Finished", run_id=run_id)
        except asyncio.CancelledError:
            self.job.update(status="error", error="Interrupted by application shutdown. Downloaded prices are kept.")
            raise
        except Exception as exc:
            log.exception("Momentum backtest failed")
            self.job.update(status="error", error=redact(str(exc)))

    async def live_ranking(self, st: momentum.Settings) -> dict:
        """The full ranking after the latest published close (for Momentum Tuned's month-end decision):
        {date, failed, ranking: [{symbol, key, rank, above_sma, close}] best first}."""
        end = upstox.today_ist() - timedelta(days=1)
        need_from = momentum.history_start(st.start)
        job = {}
        async with self.data_lock:
            instruments, cached, _ = await self._prices(need_from, end, False, job)
        return await asyncio.to_thread(self._rank_latest, cached, instruments, st, need_from, end, len(job["failed"]))

    @staticmethod
    def _arrays(cached, symbols, need_from, end):
        """Aligned [day × stock] open / close / volume on NIFTY's trading calendar."""
        bench = cached.get(BENCHMARK)
        if bench is None or not len(bench["d"]):
            raise RuntimeError("No NIFTY 50 history from Upstox (used as the trading calendar)")
        span = (bench["d"] >= need_from.toordinal()) & (bench["d"] <= end.toordinal())
        cal = bench["d"][span]
        dates = [date.fromordinal(int(d)) for d in cal]
        cols = [k for k in symbols if cached.get(k) is not None and len(cached[k]["d"])
                and cached[k]["d"][-1] >= cal[0] and cached[k]["d"][0] <= cal[-1]]
        shape = (len(cal), len(cols))
        o, c, v = (np.full(shape, np.nan, dtype=np.float32) for _ in range(3))
        for j, k in enumerate(cols):
            z = cached[k]
            idx = np.searchsorted(cal, z["d"])
            ok = (idx < len(cal)) & (cal[np.minimum(idx, len(cal) - 1)] == z["d"])
            o[idx[ok], j], c[idx[ok], j], v[idx[ok], j] = z["o"][ok], z["c"][ok], z["v"][ok]
        return dates, cols, o, c, v, bench["c"][span]

    @classmethod
    def _rank_latest(cls, cached, instruments, st, need_from, end, failed):
        symbols = {x["key"]: x["symbol"] for x in instruments}
        dates, cols, _o, c, v, _b = cls._arrays(cached, symbols, need_from, end)
        last = len(dates) - 1
        f = momentum.features(c, v, [last], st.sma_days)
        ranking = momentum.rank_day(f, 0, st.universe, st.score)
        return {"date": dates[last].isoformat(), "failed": failed,
                "ranking": [{"symbol": symbols[cols[r["col"]]], "key": cols[r["col"]], "rank": r["rank"],
                             "above_sma": r["above_sma"], "close": round(r["close"], 2)} for r in ranking]}

    async def _peaks(self, result: dict, instruments: list[dict]):
        """Each trade's (and open holding's) peak high / low from its daily candles, buy day to sell day
        (to the last close for a holding), and the P&L at those prices: max profit / max loss."""
        key_of = {x["symbol"]: x["key"] for x in instruments}
        last = result["equity"][-1][0] if result["equity"] else None
        for t in result["trades"] + result["holdings"]:
            key, end = key_of.get(t["symbol"]), t.get("exit_date") or last
            if not key or not end:
                continue
            try:
                await self.store.limiter.wait()
                rows = await upstox.candles(key, "1d", date.fromisoformat(t["entry_date"]), date.fromisoformat(end), expired=False)
            except upstox.UpstoxError as exc:
                log.warning("Daily high/low unavailable for %s: %s", t["symbol"], redact(str(exc)))
                continue
            if not rows:
                continue
            hi, lo = max(rows, key=lambda r: r[2]), min(rows, key=lambda r: r[3])
            t.update(peak_high=round(float(hi[2]), 2), peak_high_date=hi[0][:10], peak_low=round(float(lo[3]), 2), peak_low_date=lo[0][:10],
                     max_profit=round((float(hi[2]) - t["entry"]) * t["qty"], 2), max_loss=round((float(lo[3]) - t["entry"]) * t["qty"], 2))

    async def _switch_prices(self, result: dict, instruments: list[dict]) -> dict:
        """{("sell"|"buy", key, ISO day): price} — each trade's minute price at 11:00 (sell) / 12:00 (buy).
        Upstox has stock minute candles only from 2022; earlier switch days keep the open."""
        key_of = {x["symbol"]: x["key"] for x in instruments}
        wanted = {("buy", t["symbol"], t["entry_date"]) for t in result["trades"] + result["holdings"]}
        wanted |= {("sell", t["symbol"], t["exit_date"]) for t in result["trades"]}
        out = {}
        for kind, sym, day in sorted(wanted):
            key = key_of.get(sym)
            if not key or day < "2022-01-01":
                continue
            try:
                await self.store.limiter.wait()
                rows = await upstox.candles(key, "1m", date.fromisoformat(day), date.fromisoformat(day), expired=False)
            except upstox.UpstoxError as exc:
                log.warning("Minute prices unavailable for %s %s: %s", sym, day, redact(str(exc)))
                continue
            at = (momentum.SELL_TIME if kind == "sell" else momentum.BUY_TIME)[:5]
            hit = next((r for r in rows if r[0][11:16] >= at), None)
            if hit:
                out[(kind, key, day)] = float(hit[1])
        return out

    @classmethod
    def _compute(cls, cached, symbols, st, need_from, end, fills=None):
        dates, cols, o, c, v, bench = cls._arrays(cached, symbols, need_from, end)
        arrays = None
        if fills:
            row = {d.isoformat(): i for i, d in enumerate(dates)}
            col = {k: j for j, k in enumerate(cols)}
            arrays = {"sell": np.full(o.shape, np.nan), "buy": np.full(o.shape, np.nan)}
            for (kind, key, day), px in fills.items():
                if day in row and key in col:
                    arrays[kind][row[day], col[key]] = px
        return momentum.run(dates, [symbols[k] for k in cols], o, c, v, st, bench, arrays)
