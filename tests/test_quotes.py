import asyncio
from copy import deepcopy
from datetime import date
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from helpers import DashboardClient
from upstocks import upstox
from upstocks.app import create_app
from upstocks.config import AppSettings
from upstocks.services.quotes import QuoteService

DAY = date(2026, 10, 12)
KEY = 'NSE_FO|123'
TID = 'frontend_live|2026-10-12|09:36|' + KEY


class QuoteServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.trading = SimpleNamespace(view_revision='scan-1', view={'day': str(DAY), 'results': {
            'frontend_live': [{'id': TID, 'entry': 300.0, 'instrument_key': KEY, 'open': True,
                               'exit': 301.0, 'pnl': 1.0, 'sl': 282, 'target': 390}],
            'smc_live': [{'id': 'second-position', 'entry': 290.0, 'instrument_key': KEY, 'open': True}],
        }})
        self.service = QuoteService(self.trading)
        self.addAsyncCleanup(self.service.stop)
        for target, value in [('upstocks.live.market_open', True), ('upstocks.upstox.today_ist', DAY)]:
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_shared_batch_updates_marks_without_changing_entries_or_decisions(self):
        original = deepcopy(self.trading.view)
        with patch('upstocks.upstox.ltp_quotes', new=AsyncMock(return_value={KEY: 315.25})) as quote, patch('upstocks.upstox.place_market_order', new=AsyncMock()) as order:
            responses = await asyncio.gather(*(self.service.get() for _ in range(8)))
            quote.assert_awaited_once_with([KEY])
            self.assertEqual(responses[0]['status'], 'live')
            self.assertEqual(responses[0]['trades'][TID]['pnl'], 15.25)
            self.assertEqual(responses[0]['trades']['second-position']['pnl'], 25.25)
            self.assertFalse(responses[0]['trades'][TID]['stale'])
            await self.service.get()  # another tab reuses this second's quote
            self.assertEqual(quote.await_count, 1)
            order.assert_not_awaited()
        self.assertEqual(self.trading.view, original)

    async def test_new_quote_each_second_and_duplicate_instruments_are_batched(self):
        with patch('upstocks.services.quotes.monotonic', return_value=100), patch('upstocks.upstox.ltp_quotes', new=AsyncMock(return_value={KEY: 302})) as quote:
            first = await self.service.get()
            with patch('upstocks.services.quotes.monotonic', return_value=100.9):
                self.assertEqual((await self.service.get())['trades'], first['trades'])
                self.assertEqual(quote.await_count, 1)
            quote.return_value = {KEY: 304}
            with patch('upstocks.services.quotes.monotonic', return_value=101):
                result = await self.service.get()
                self.assertEqual(quote.await_count, 2)
                self.assertEqual(result['trades'][TID]['pnl'], 4)

    async def test_closed_trades_replays_and_closed_market_do_not_request_quotes(self):
        with patch('upstocks.upstox.ltp_quotes', new=AsyncMock()) as quote:
            with patch('upstocks.live.market_open', return_value=False):
                self.assertEqual((await self.service.get())['status'], 'market_closed')
            self.trading.view['day'] = '2026-10-09'
            self.assertEqual((await self.service.get())['status'], 'replay')
            self.trading.view['day'] = str(DAY)
            for trades in self.trading.view['results'].values():
                for trade in trades:
                    trade['open'] = False
            self.assertEqual((await self.service.get())['status'], 'idle')
            quote.assert_not_awaited()

    async def test_exit_during_quote_fetch_cannot_revive_closed_trade(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def fetch(keys):
            entered.set()
            await finish.wait()
            return {KEY: 999}
        with patch('upstocks.upstox.ltp_quotes', new=fetch):
            task = asyncio.create_task(self.service.get())
            await entered.wait()
            self.trading.view['results'] = {'frontend_live': [{'id': TID, 'entry': 300, 'exit': 390, 'open': False}]}
            self.trading.view_revision = 'scan-2'
            finish.set()
            result = await task
            self.assertEqual(result['status'], 'idle')
            self.assertEqual(result['trades'], {})
            self.assertEqual(result['view_revision'], 'scan-2')
            self.assertEqual(self.trading.view['results']['frontend_live'][0]['exit'], 390)

    async def test_failed_quotes_keep_last_price_mark_stale_and_back_off(self):
        with patch('upstocks.upstox.ltp_quotes', new=AsyncMock(return_value={KEY: 310})) as quote, patch('upstocks.services.quotes.monotonic', return_value=100):
            first = await self.service.get()
            quote.side_effect = upstox.QuoteUnavailable('Rate limited', retry_after=30)
            with patch('upstocks.services.quotes.monotonic', return_value=101), self.assertLogs('upstocks.services.quotes', level='WARNING'):
                result = await self.service.get()
            self.assertEqual(result['status'], 'delayed')
            self.assertTrue(result['trades'][TID]['stale'])
            self.assertEqual(result['trades'][TID]['ltp'], 310)
            self.assertEqual(result['trades'][TID]['received_at'], first['trades'][TID]['received_at'])
            with patch('upstocks.services.quotes.monotonic', return_value=110):
                await self.service.get()
                self.assertEqual(quote.await_count, 2)
            quote.side_effect = None
            quote.return_value = {KEY: 320}
            with patch('upstocks.services.quotes.monotonic', return_value=131):
                self.assertEqual((await self.service.get())['status'], 'live')

    async def test_partial_response_does_not_invent_or_zero_missing_prices(self):
        self.trading.view['results']['smc_live'][0]['instrument_key'] = 'NSE_FO|456'
        with patch('upstocks.upstox.ltp_quotes', new=AsyncMock(return_value={KEY: 310})), self.assertLogs('upstocks.services.quotes', level='WARNING'):
            result = await self.service.get()
        self.assertEqual(result['status'], 'delayed')
        self.assertNotIn('second-position', result['trades'])
        self.assertEqual(result['trades'][TID]['ltp'], 310)

    async def test_browser_cancellation_does_not_cancel_other_readers(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def fetch(keys):
            entered.set()
            await finish.wait()
            return {KEY: 310}
        with patch('upstocks.upstox.ltp_quotes', new=fetch):
            first = asyncio.create_task(self.service.get())
            await entered.wait()
            second = asyncio.create_task(self.service.get())
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            finish.set()
            self.assertEqual((await second)['status'], 'live')

    async def test_shutdown_cancels_inflight_read(self):
        entered = asyncio.Event()
        async def fetch(keys):
            entered.set()
            await asyncio.Event().wait()
        with patch('upstocks.upstox.ltp_quotes', new=fetch):
            request = asyncio.create_task(self.service.get())
            await entered.wait()
            await self.service.stop()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertIsNone(self.service._task)
            self.assertEqual((await self.service.get())['status'], 'unavailable')


class QuoteGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = patch.dict('os.environ', {'UPSTOX_ACCESS_TOKEN': 'fake-test-token'})
        self.config.start()
        self.addCleanup(self.config.stop)
        upstox._quote_lock = asyncio.Lock()
        upstox._last_quote_start = 0

    async def test_maps_instrument_token_and_ignores_bad_prices(self):
        data = {'a': {'instrument_token': KEY, 'last_price': 313.5},
                'b': {'instrument_token': 'other', 'last_price': 500},
                'c': {'instrument_token': 'zero', 'last_price': 0},
                'd': {'instrument_token': 'boolean', 'last_price': True},
                'e': {'instrument_token': 'string', 'last_price': 'NaN'}}
        fake = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, json={'status': 'success', 'data': data})))
        with patch('upstocks.upstox.client', return_value=fake), patch('upstocks.upstox._throttle', new=AsyncMock()) as historical_limiter:
            result = await upstox.ltp_quotes([KEY, KEY, 'zero', 'boolean', 'string'])
        self.assertEqual(result, {KEY: 313.5})
        self.assertEqual(fake.get.await_args.args[0], '/v3/market-quote/ltp')
        self.assertEqual(fake.get.await_args.kwargs['params']['instrument_key'].count(KEY), 1)
        historical_limiter.assert_not_awaited()

    async def test_rate_limit_respects_retry_after_without_retry_storm(self):
        fake = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(429, headers={'Retry-After': '42'})))
        with patch('upstocks.upstox.client', return_value=fake), self.assertRaises(upstox.QuoteUnavailable) as error:
            await upstox.ltp_quotes([KEY])
        self.assertEqual(error.exception.retry_after, 42)
        self.assertEqual(fake.get.await_count, 1)

    async def test_gateway_enforces_one_second_between_quote_requests(self):
        starts = []
        async def respond(*args, **kwargs):
            starts.append(asyncio.get_running_loop().time())
            return httpx.Response(200, json={'status': 'success', 'data': {}})
        fake = SimpleNamespace(get=AsyncMock(side_effect=respond))
        with patch('upstocks.upstox.client', return_value=fake):
            await asyncio.gather(upstox.ltp_quotes([KEY]), upstox.ltp_quotes([KEY]))
        self.assertGreaterEqual(starts[1] - starts[0], .99)

    async def test_empty_batch_and_invalid_response(self):
        fake = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, content='not json')))
        with patch('upstocks.upstox.client', return_value=fake):
            self.assertEqual(await upstox.ltp_quotes([]), {})
            fake.get.assert_not_awaited()
            with self.assertRaises(upstox.QuoteUnavailable):
                await upstox.ltp_quotes([KEY])


class QuoteApiTests(unittest.TestCase):
    def test_quote_api_returns_latest_marks_without_persisting_or_ordering(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = create_app(AppSettings(database_path=root/'db.sqlite3', log_dir=root/'logs', legacy_dir=root,
                                         cache_dir=root/'cache', scanner_enabled=False))
            with DashboardClient(app) as client, patch('upstocks.live.market_open', return_value=True), patch('upstocks.upstox.today_ist', return_value=DAY), patch('upstocks.upstox.ltp_quotes', new=AsyncMock(return_value={KEY: 311})), patch('upstocks.upstox.place_market_order', new=AsyncMock()) as orders:
                before = app.state.repository.state()
                app.state.trading.view = {'day': str(DAY), 'results': {'frontend_live': [{'id': TID, 'entry': 300, 'instrument_key': KEY, 'open': True}]}}
                response = client.get('/api/paper/quotes')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['trades'][TID]['pnl'], 11)
                self.assertEqual(response.json()['poll_interval_ms'], 1000)
                self.assertEqual(app.state.repository.state(), before)
                orders.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
