"""Shared candle reads survive cancelled callers; only read requests are retried."""
import asyncio
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from upstocks import upstox

DAY = date(2026, 11, 3)
ROWS = [["2026-11-03T09:15:00+05:30", 100, 101, 99, 100, 1]]


class CandleIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await upstox.close()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.previous_disk = upstox._DISK
        upstox.configure(Path(self.folder.name))
        self.addCleanup(upstox.configure, self.previous_disk)
        self.clock = patch.object(upstox, "today_ist", return_value=DAY)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    async def asyncTearDown(self):
        await upstox.close()

    async def read(self):
        return await upstox.candles("TEST|1", "1m", DAY, DAY, False)

    async def test_owner_cancellation_keeps_one_shared_read_for_followers(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def fetch(*args):
            entered.set()
            await release.wait()
            return ROWS
        with patch.object(upstox, "_fetch_candles", AsyncMock(side_effect=fetch)) as call:
            owner = asyncio.create_task(self.read())
            await entered.wait()
            follower = asyncio.create_task(self.read())
            await asyncio.sleep(0)
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
            late = asyncio.create_task(self.read())
            await asyncio.sleep(0)
            self.assertFalse(follower.done())
            self.assertEqual(len(upstox._inflight), 1)
            release.set()
            self.assertEqual(await asyncio.wait_for(follower, 1), ROWS)
            self.assertEqual(await late, ROWS)
            self.assertEqual(call.await_count, 1)
            self.assertFalse(upstox._inflight)

    async def test_statistics_timeout_does_not_cancel_the_engine_owner(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def fetch(*args):
            entered.set()
            await release.wait()
            return ROWS
        with patch.object(upstox, "_fetch_candles", fetch):
            engine = asyncio.create_task(self.read())
            await entered.wait()
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(self.read(), .01)
            self.assertFalse(engine.done())
            release.set()
            self.assertEqual(await engine, ROWS)

    async def test_abandoned_failure_is_consumed_and_next_read_can_retry(self):
        entered, release = asyncio.Event(), asyncio.Event()
        unhandled = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))
        async def fail(*args):
            entered.set()
            await release.wait()
            raise upstox.MarketDataUnavailable("temporary failure")
        try:
            with patch.object(upstox, "_fetch_candles", fail):
                caller = asyncio.create_task(self.read())
                await entered.wait()
                shared = next(iter(upstox._inflight.values()))
                finished = asyncio.Event()
                shared.add_done_callback(lambda task: finished.set())
                caller.cancel()
                await asyncio.gather(caller, return_exceptions=True)
                release.set()
                await finished.wait()
                self.assertFalse(upstox._inflight)
                self.assertFalse(shared._log_traceback)
            with patch.object(upstox, "_fetch_candles", AsyncMock(return_value=ROWS)):
                self.assertEqual(await self.read(), ROWS)
            self.assertEqual(unhandled, [])
        finally:
            loop.set_exception_handler(previous)

    async def test_shutdown_still_cancels_real_shared_downloads(self):
        entered = asyncio.Event()
        async def fetch(*args):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(upstox, "_fetch_candles", fetch):
            task = asyncio.create_task(self.read())
            await entered.wait()
            await upstox.close()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(upstox._inflight)


class ReadRetryTests(unittest.IsolatedAsyncioTestCase):
    async def check_get(self, responses):
        calls = []
        async def handler(request):
            calls.append(request.method)
            item = responses[min(len(calls)-1, len(responses)-1)]
            if isinstance(item, Exception):
                raise item
            status, body = item
            return httpx.Response(status, json=body)
        async with httpx.AsyncClient(base_url=upstox.BASE, transport=httpx.MockTransport(handler)) as client:
            with patch.object(upstox, "client", return_value=client), patch.object(upstox, "_token", return_value="TEST"), \
                 patch.object(upstox, "_throttle", AsyncMock()), patch("upstocks.upstox.asyncio.sleep", AsyncMock()) as sleep:
                try:
                    value = await upstox._get("/read-only")
                except upstox.UpstoxError as exc:
                    value = exc
        return value, calls, sleep.await_count

    async def test_read_timeout_retries_then_recovers(self):
        value, calls, sleeps = await self.check_get([httpx.ReadTimeout(""), (200, {"status": "success", "data": []})])
        self.assertEqual(value["status"], "success")
        self.assertEqual(calls, ["GET", "GET"])
        self.assertEqual(sleeps, 1)

    async def test_retries_are_bounded_with_an_actionable_error(self):
        value, calls, sleeps = await self.check_get([httpx.ReadTimeout("")])
        self.assertIsInstance(value, upstox.MarketDataUnavailable)
        self.assertIn("timed out after 3 attempts", str(value))
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, 2)

    async def test_gateway_failure_recovers_and_service_failures_are_bounded(self):
        value, calls, _ = await self.check_get([(503, {}), (200, {"status": "success"})])
        self.assertEqual(value["status"], "success")
        self.assertEqual(len(calls), 2)
        value, calls, _ = await self.check_get([(502, {})])
        self.assertIsInstance(value, upstox.MarketDataUnavailable)
        self.assertEqual(len(calls), 3)

    async def test_auth_failure_is_not_retried(self):
        value, calls, sleeps = await self.check_get([(401, {"status": "error", "errors": [{"message": "Invalid login"}]})])
        self.assertIsInstance(value, upstox.UpstoxError)
        self.assertNotIsInstance(value, upstox.MarketDataUnavailable)
        self.assertEqual(calls, ["GET"])
        self.assertEqual(sleeps, 0)

    async def test_existing_rate_limit_retry_policy_is_preserved(self):
        value, calls, sleeps = await self.check_get([(429, {}), (200, {"status": "success"})])
        self.assertEqual(value["status"], "success")
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, 1)
