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

    async def _run(self, st: momentum.Settings, refresh_all: bool):
        try:
            today = upstox.today_ist()
            end = min(st.end, today - timedelta(days=1))
            fetch_end = today - timedelta(days=1)
            need_from = momentum.history_start(st.start)
            instruments = await self.store.instruments()
            keys = [BENCHMARK] + [x["key"] for x in instruments]
            self.job.update(phase="Checking cached prices", total=len(keys))
            cached = await asyncio.to_thread(lambda: {k: self.store.load(k) for k in keys})
            active = sorted((k for k, z in cached.items() if z is not None and len(z["d"])),
                            key=lambda k: -_recent_turnover(cached[k]))[:ACTIVE]
            active = set(active) | {BENCHMARK}
            todo = []
            for k in keys:
                z = cached[k]
                if z is None or int(z["frm"]) > need_from.toordinal():
                    todo.append(k)
                elif int(z["thru"]) < end.toordinal() and (refresh_all or k in active
                                                           or int(z["thru"]) < (end - timedelta(days=STALE_DAYS)).toordinal()):
                    todo.append(k)
            self.job.update(phase="Downloading daily prices from Upstox" if todo else "Prices up to date",
                            done=0, total=len(todo), failed=[])
            sem = asyncio.Semaphore(6)

            async def one(k):
                async with sem:
                    try:
                        await self.store.refresh(k, need_from, fetch_end, cached[k])
                        cached[k] = await asyncio.to_thread(self.store.load, k)
                    except upstox.UpstoxError as exc:
                        self.job["failed"].append(k)
                        log.warning("Price download failed for %s: %s", k, redact(str(exc)))
                    self.job["done"] += 1

            await asyncio.gather(*(one(k) for k in todo))
            self.job.update(phase="Running the backtest", done=0, total=0)
            symbols = {x["key"]: x["symbol"] for x in instruments}
            result = await asyncio.to_thread(self._compute, cached, symbols, st, need_from, end)
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

    @staticmethod
    def _compute(cached, symbols, st, need_from, end):
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
        return momentum.run(dates, [symbols[k] for k in cols], o, c, v, st, bench["c"][span])
