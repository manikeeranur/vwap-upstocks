"""Bounded background jobs with durable progress and restart recovery."""
import asyncio
from datetime import date, timedelta
import logging
import uuid

from .. import combo, ema_pullback, harmonic, smc, upstox, vwap
from ..observability import redact, request_id

log = logging.getLogger(__name__)


class BacktestsBusy(Exception):
    pass


class BacktestService:
    def __init__(self, repository, max_jobs=2):
        self.repository, self.max_jobs = repository, max_jobs
        self.tasks = {}
        self._lock = asyncio.Lock()

    async def submit(self, settings, preset):
        async with self._lock:
            if len(self.tasks) >= self.max_jobs:
                raise BacktestsBusy("Backtest capacity reached. Wait for a running job to finish.")
            jid = uuid.uuid4().hex
            await asyncio.to_thread(self.repository.create_job, jid, settings, preset)
            self.tasks[jid] = asyncio.create_task(self._run(jid, settings), name=f"backtest-{jid}")
            self.tasks[jid].add_done_callback(lambda task: self._finished(jid, task))
            return jid

    def _finished(self, jid, task):
        self.tasks.pop(jid, None)
        if not task.cancelled() and task.exception():
            exc = task.exception()
            log.error("Backtest persistence failure", extra={"job_id": jid}, exc_info=(type(exc), exc, exc.__traceback__))

    async def stop(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    async def _run(self, jid, settings):
        token = request_id.set(None)
        try:
            last = min(settings.end, upstox.today_ist())
            allspot = await vwap.trading_days_and_spot(settings.start - timedelta(days=7) if settings.index_trend else settings.start, last,
                                                       settings.underlying)
            keys = sorted(allspot)
            prev_spot = {day: allspot[keys[i - 1]] if i else [] for i, day in enumerate(keys)}
            spot = {day: bars for day, bars in allspot.items() if settings.start.isoformat() <= day <= last.isoformat()}
            weekdays = [settings.start + timedelta(days=i) for i in range((last - settings.start).days + 1)]
            missing = [day.isoformat() for day in weekdays if day.weekday() < 5 and day.isoformat() not in spot]
            await asyncio.to_thread(self.repository.update_job, jid, total=len(spot), missing=missing)
            sem = asyncio.Semaphore(3)

            async def one(day, bars):
                async with sem:
                    await asyncio.to_thread(self.repository.update_job, jid, current=day)
                    try:
                        if settings.strategy == "smc":
                            result = await smc.run_day(date.fromisoformat(day), settings, bars)
                        elif settings.strategy == "ema_pullback":
                            result = await ema_pullback.run_day(date.fromisoformat(day), settings, bars)
                        elif settings.strategy == "combo":
                            result = await combo.run_day(date.fromisoformat(day), settings, bars)
                        elif settings.strategy == "harmonic":
                            result = await harmonic.run_day(date.fromisoformat(day), settings, bars, prev_spot.get(day))
                        else:
                            result = await vwap.run_day(date.fromisoformat(day), settings, bars, prev_spot[day])
                        out = {"date": result.date, "expiry": result.expiry, "note": result.note,
                               "trades": [vwap.trade_dict(t) for t in result.trades]}
                    except Exception as exc:
                        log.exception("Backtest day failed", extra={"job_id": jid})
                        out = {"date": day, "expiry": None, "note": f"error: {redact(str(exc))}", "trades": []}
                    await asyncio.to_thread(self.repository.add_job_day, jid, out)

            # TaskGroup cancels sibling work if saving a result fails.
            async with asyncio.TaskGroup() as group:
                for day, bars in spot.items():
                    group.create_task(one(day, bars))
            await asyncio.to_thread(self.repository.update_job, jid, status="done", current=None)
            log.info("Backtest completed", extra={"job_id": jid})
        except asyncio.CancelledError:
            await asyncio.to_thread(self.repository.update_job, jid, status="error", current=None,
                                    error="Interrupted by application shutdown. Saved results are retained.")
            raise
        except Exception as exc:
            log.exception("Backtest failed", extra={"job_id": jid})
            await asyncio.to_thread(self.repository.update_job, jid, status="error", current=None, error=redact(str(exc)))
        finally:
            request_id.reset(token)
