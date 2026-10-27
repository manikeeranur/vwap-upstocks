import concurrent.futures
from datetime import date
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient

from helpers import DashboardClient
from upstocks import diagnostics
from upstocks.app import create_app
from upstocks.config import AppSettings
from upstocks.database import Database, MIGRATIONS
from upstocks.observability import exception_details
from upstocks.repository import ConfigurationConflict, Repository
from upstocks.security import DashboardGuard
from upstocks.vwap import Settings

DAY = date(2026, 10, 12)
PRESET = "frontend_live"
TID = "frontend_live|2026-10-12|09:36|NSE_FO|123"


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "db.sqlite3")
        self.db.initialize()
        self.repo = Repository(self.db)
        self.repo.migrate_legacy(self.root)

    def test_v1_upgrade_preserves_existing_records_and_is_idempotent(self):
        old = Database(self.root / "old.sqlite3")
        with patch("upstocks.database.MIGRATIONS", MIGRATIONS[:1]):
            old.initialize()
        with old.connect(write=True) as conn:
            conn.execute("INSERT INTO settings VALUES ('lot_size','65')")
            conn.execute("INSERT INTO strategy_config VALUES (?,1,0,3)", (PRESET,))
            conn.execute("INSERT INTO audit_events VALUES (1,?,'activity','original','{}',NULL)", ("2026-10-12T01:00:00.000+00:00",))
            conn.execute("INSERT INTO order_attempts VALUES (?,'BUY',?,'submitted',?,?,?)", (TID, PRESET, '{"qty":195,"order_id":"original"}', "old", "old"))
            conn.execute("INSERT INTO journal_sessions VALUES (?,?,3,65,'old')", (str(DAY), PRESET))
            conn.execute("INSERT INTO journal_trades VALUES (?,?,?,0,'{}')", (str(DAY), PRESET, TID))
            conn.execute("INSERT INTO notes VALUES (?,'keep note','old')", (TID,))
            conn.execute("INSERT INTO backtest_jobs(id,settings,preset,status,created_at,updated_at) VALUES ('job','{}',?,'done','old','old')", (PRESET,))
            conn.execute("INSERT INTO backtest_days VALUES ('job',?,'{}',0)", (str(DAY),))
            tables = ('settings', 'strategy_config', 'order_attempts', 'journal_sessions', 'journal_trades', 'notes', 'backtest_jobs', 'backtest_days')
            before = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in tables}
        old.initialize()
        old.initialize()
        with old.connect() as conn:
            for table in tables:
                rows = [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
                for original in before[table]:
                    self.assertIn(original, rows)
            self.assertEqual(conn.execute("SELECT message,level,component FROM audit_events").fetchone()[:], ('original', 'INFO', 'trading'))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM decision_evidence").fetchone()[0], 0)
        state = Repository(old).state()
        self.assertEqual((state['revision'], state['users'][1]['paused']), (0, False))

    def test_compare_and_set_allows_only_one_concurrent_revision(self):
        def update(lots):
            try:
                self.repo.configure(PRESET, user=1, broker='upstox', lots=lots, revision=0)
                return lots
            except ConfigurationConflict:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(update, range(2, 10)))
        accepted = [r for r in results if r is not None]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(self.repo.state()['accounts']['1:upstox']['lots'][PRESET], accepted[0])
        self.assertEqual(self.repo.state()['revision'], 1)

    def test_evidence_is_immutable_and_atomic_with_order_intent(self):
        trade = {"entry": 301, "stop": 280, "instrument_key": "NSE_FO|123"}
        self.repo.reserve_order(TID, "1:upstox", PRESET, "BUY", {"qty": 65}, {"trade": trade})
        trade['entry'] = 999
        self.repo.finish_order(TID, "1:upstox", "BUY", {"qty": 65, "status": "submitted", "order_id": "fake"}, "submitted")
        with self.db.connect() as conn:
            rows = conn.execute("SELECT stage,payload FROM decision_evidence ORDER BY id").fetchall()
            self.assertEqual([r['stage'] for r in rows], ['ORDER_INTENT', 'ORDER_RESULT'])
            payload = json.loads(rows[0]['payload'])
            self.assertEqual(payload['context']['trade']['entry'], 301)
            self.assertEqual(len(payload['implementation']['sha256']), 64)
        for sql in ("DELETE FROM decision_evidence", "UPDATE decision_evidence SET stage='changed'"):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'), self.db.connect(write=True) as conn:
                conn.execute(sql)
        with patch.object(self.repo, '_evidence', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.repo.reserve_order(TID + '2', '1:upstox', PRESET, 'BUY', {'qty': 65})
        self.assertNotIn(TID + '2', self.repo.state()['orders'].get('1:upstox', {}))

    def test_export_reads_consistent_complete_snapshot_and_redacts(self):
        start, end = diagnostics.day_bounds(DAY)
        self.assertEqual((start, end), ('2026-10-11T18:30:00.000+00:00', '2026-10-12T18:30:00.000+00:00'))
        with self.db.connect(write=True) as conn:
            # More than a page, including both IST midnight boundaries.
            rows = [(start, f'event {i}', json.dumps({'nested': {'api_secret': 'hidden-test-secret'}, 'text': 'Bearer hidden-bearer'})) for i in range(505)]
            rows += [(end, 'next day', '{}'), ('2026-10-11T18:29:59.999+00:00', 'previous day', '{}')]
            conn.executemany("INSERT INTO audit_events(created_at,kind,message,details,level,component) VALUES (?,'test',?,?,'INFO','application')", rows)
        self.repo.save_scan({'day': str(DAY), 'results': {}, 'errors': {}}, {PRESET: {'lots': 1, 'lot_size': 65, 'updated': str(DAY), 'trades': [{'id': TID, 'pnl': 10}]}}, {'is_replay': True})
        self.repo.set_note(TID, 'a useful note')
        self.repo.reserve_order(TID, '1:upstox', PRESET, 'BUY', {'qty': 65})
        self.repo.create_job('job', Settings(start=DAY, end=DAY), PRESET)
        self.repo.add_job_day('job', {'date': str(DAY), 'trades': []})
        self.repo.update_job('job', status='done', total=1)
        token = self.repo.dashboard_token()
        state = self.repo.state()
        original_counts = diagnostics._counts
        def snapshot_then_write(conn, day):
            counts = original_counts(conn, day)
            with self.db.connect(write=True) as writer:
                writer.execute("INSERT INTO audit_events(created_at,kind,message,details) VALUES (?,'test','too late','{}')", (start,))
            return counts
        with patch('upstocks.diagnostics._counts', side_effect=snapshot_then_write), diagnostics.build_session_export(self.db, DAY) as exported, zipfile.ZipFile(exported) as bundle:
            manifest = json.loads(bundle.read('manifest.json'))
            events = [json.loads(r) for r in bundle.read('events.jsonl').splitlines()]
            self.assertEqual(len(events), manifest['counts']['events'])
            self.assertGreaterEqual(len(events), 505)
            self.assertEqual(sum(r['kind'] == 'test' for r in events), 505)
            for name, count_key in [('evidence', 'evidence'), ('orders', 'order_attempts'), ('journal', 'journal_trades'), ('backtests', 'backtest_days')]:
                self.assertEqual(len(bundle.read(name + '.jsonl').splitlines()), manifest['counts'][count_key])
            self.assertEqual(json.loads(bundle.read('journal.jsonl'))['note'], 'a useful note')
            self.assertTrue(json.loads(bundle.read('evidence.jsonl').splitlines()[0])['payload']['context']['is_replay'])
            text = '\n'.join(bundle.read(n).decode() for n in bundle.namelist())
            for secret in ('hidden-test-secret', 'hidden-bearer', token, 'too late', 'next day', 'previous day'):
                self.assertNotIn(secret, text)
        self.assertEqual(self.repo.state(), state)
        with self.assertRaises(sqlite3.OperationalError), self.db.connect() as conn:
            conn.execute("UPDATE settings SET value='0'")

    def test_search_filters_literal_wildcards_and_stable_pagination(self):
        start, _ = diagnostics.day_bounds(DAY)
        with self.db.connect(write=True) as conn:
            conn.executemany("INSERT INTO audit_events(created_at,kind,message,details,level,component) VALUES (?,'test',?,'{}',?,?)", [
                (start, '100%_literal', 'WARNING', 'scanner'),
                (start, '100xxliteral', 'INFO', 'http'),
                (start, 'second', 'WARNING', 'scanner'),
                (start, 'third', 'ERROR', 'research'),
            ])
        found = diagnostics.events(self.db, day=DAY, query='%_')
        self.assertEqual([r['message'] for r in found['events']], ['100%_literal'])
        first = diagnostics.events(self.db, day=DAY, level='WARNING', component='scanner', limit=1)
        second = diagnostics.events(self.db, day=DAY, level='WARNING', component='scanner', limit=1, before=first['next_cursor'])
        self.assertEqual([first['events'][0]['message'], second['events'][0]['message']], ['second', '100%_literal'])
        self.assertIsNone(second['next_cursor'])
        self.assertFalse(diagnostics.events(self.db, day=DAY, query="' OR 1=1 --")['events'])
        self.assertEqual(len(diagnostics.events(self.db, day=DAY, component='http', include_http=True)['events']), 1)
        with diagnostics.build_session_export(self.db, date(1990, 1, 1)) as archive, zipfile.ZipFile(archive) as bundle:
            self.assertEqual(bundle.read('events.jsonl'), b'')


class OperationsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = AppSettings(database_path=self.root / 'db.sqlite3', log_dir=self.root / 'logs', cache_dir=self.root / 'cache', legacy_dir=self.root, scanner_enabled=False)
        self.app = create_app(self.settings)
        guard = patch('upstocks.upstox.client', side_effect=AssertionError('No real broker requests in tests'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_stale_config_returns_current_state_and_pause_survives_restart(self):
        with DashboardClient(self.app) as client:
            first = client.post('/api/paper/config', json={'revision': 0, 'entries_paused': True})
            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.json()['state']['entries_paused'])
            stale = client.post('/api/paper/config', json={'revision': 0, 'preset': PRESET, 'broker': 'upstox', 'lots': 9})
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale.json()['state']['revision'], 1)
            self.assertEqual(stale.json()['state']['accounts']['1:upstox']['lots'][PRESET], 1)
            self.assertEqual(client.post('/api/paper/config', json={'revision': 1, 'entries_paused': False}).status_code, 422)
        with DashboardClient(create_app(self.settings)) as client:
            self.assertTrue(client.get('/api/paper').json()['state']['entries_paused'])
            self.assertEqual(client.post('/api/paper/config', json={'revision': 1, 'entries_paused': False, 'confirm': 'LIVE'}).status_code, 200)
            self.assertFalse(any(any(a['live'].values()) for a in client.get('/api/paper').json()['state']['accounts'].values()))

    def test_host_origin_token_and_body_guards(self):
        with TestClient(self.app, base_url='http://127.0.0.1') as client:
            payload = {'revision': 0, 'entries_paused': True}
            self.assertEqual(client.post('/api/paper/config', json=payload).status_code, 403)
            client.headers['X-App-Token'] = self.app.state.dashboard_token
            for headers in ({'origin': 'https://evil.example'}, {'sec-fetch-site': 'cross-site'}, {'X-App-Token': 'wrong'}):
                self.assertEqual(client.post('/api/paper/config', json=payload, headers=headers).status_code, 403)
            for host in ('evil.example', '127.0.0.1.evil.example', '127.0.0.1@evil.example', 'localhost:bad'):
                self.assertEqual(client.get('/paper', headers={'host': host}).status_code, 400)
            self.assertEqual(client.post('/api/paper/config', content='{}', headers={'content-type': 'text/plain'}).status_code, 415)
            self.assertEqual(client.post('/api/paper/config', content=' ' * 1_048_577, headers={'content-type': 'application/json'}).status_code, 413)
            self.assertEqual(client.get('/paper').headers['x-frame-options'], 'DENY')
            self.assertFalse(self.app.state.repository.state()['users'][1]['paused'])
            from upstocks.accounts import SESSION_COOKIE
            client.cookies.set(SESSION_COOKIE, self.app.state.signer.session(1))
            self.assertEqual(client.post('/api/paper/config', json=payload, headers={'origin': 'http://127.0.0.1'}).status_code, 200)

    def test_activity_export_and_request_correlation(self):
        with DashboardClient(self.app) as client:
            response = client.get('/api/paper?access_token=do-not-log-this-query')
            with self.app.state.database.connect() as conn:
                event = conn.execute("SELECT * FROM audit_events WHERE kind='http' AND request_id=?", (response.headers['x-request-id'],)).fetchone()
                self.assertEqual(json.loads(event['details'])['route'], '/api/paper')
                self.assertNotIn('do-not-log-this-query', event['details'])
            self.assertEqual(client.get('/activity').status_code, 200)
            self.assertEqual(client.get('/api/activity?day=bad').status_code, 422)
            self.assertEqual(client.get('/api/activity?day=2026-10-12&limit=501').status_code, 422)
            self.assertEqual(client.get('/api/activity?day=2026-10-12&before=-1').status_code, 422)
            response = client.get('/api/activity/session.zip?day=2026-10-12')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['content-type'], 'application/zip')
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                self.assertEqual(json.loads(archive.read('manifest.json'))['schema_version'], 4)
            self.assertEqual(client.get('/health/ready').json()['audit_failures'], 0)

    def test_exception_frames_exclude_source_and_local_secrets(self):
        try:
            local_password = 'do-not-print-local-or-source'
            raise ValueError('api_secret=hidden-exception-secret')
        except ValueError as exc:
            details = exception_details(exc)
        text = json.dumps(details)
        self.assertNotIn(local_password, text)
        self.assertNotIn('hidden-exception-secret', text)
        self.assertEqual(set(details[0]['frames'][-1]), {'file', 'line', 'function'})


class ChunkedBodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_body_cannot_bypass_size_limit(self):
        reached = False
        async def endpoint(scope, receive, send):
            nonlocal reached
            reached = True
        guard = DashboardGuard(endpoint, allowed_hosts=('127.0.0.1',))
        scope = {'type': 'http', 'method': 'POST', 'scheme': 'http', 'app': SimpleNamespace(state=SimpleNamespace(dashboard_token='test')), 'headers': [(b'host', b'127.0.0.1'), (b'x-app-token', b'test'), (b'content-type', b'application/json')]}
        chunks = iter([{'type': 'http.request', 'body': b'x' * 600_000, 'more_body': True}] * 2)
        async def receive():
            return next(chunks)
        messages = []
        async def send(message):
            messages.append(message)
        await guard(scope, receive, send)
        self.assertEqual(messages[0]['status'], 413)
        self.assertFalse(reached)


if __name__ == '__main__':
    unittest.main()
