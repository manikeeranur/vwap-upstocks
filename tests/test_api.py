from datetime import date
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from helpers import DashboardClient as TestClient

from upstocks.app import create_app
from upstocks.config import AppSettings
from upstocks.observability import close_logging, configure_logging, JsonFormatter
from upstocks.vwap import Settings


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = AppSettings(database_path=self.root / "db.sqlite3", log_dir=self.root / "logs",
                                    cache_dir=self.root / "cache", legacy_dir=self.root, scanner_enabled=False)
        self.app = create_app(self.settings)
        self.network_guard = patch("upstocks.upstox.client", side_effect=AssertionError("Tests must not access the real broker"))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)

    def test_config_validation_and_restart_persistence(self):
        with TestClient(self.app) as client:
            for payload in ({"preset": "frontend_live", "live": "false"},
                            {"preset": "frontend_live", "live": True},
                            {"preset": "frontend_live", "lots": -1},
                            {"preset": "frontend_live", "lots": 1.5},
                            {"preset": "missing", "on": False}, {"live": True, "confirm": "LIVE"}, {},
                            {"preset": "frontend_live", "lots": 3}, {"preset": "frontend_live", "broker": "dhan", "lots": 3}):
                self.assertEqual(client.post("/api/paper/config", json={"revision": 0, **payload}).status_code, 422)
            response = client.post("/api/paper/config", json={"revision": 0, "preset": "frontend_live", "broker": "upstox", "lots": 3})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["state"]["accounts"]["1:upstox"]["lots"]["frontend_live"], 3)
            rid = response.headers["x-request-id"]
            with self.app.state.database.connect() as conn:
                self.assertEqual(conn.execute("SELECT request_id FROM audit_events WHERE kind='config' ORDER BY id DESC LIMIT 1").fetchone()[0], rid)
        with TestClient(create_app(self.settings)) as client:
            self.assertEqual(client.get("/api/paper").json()["state"]["accounts"]["1:upstox"]["lots"]["frontend_live"], 3)

    def test_manual_scan_endpoint_requests_simulation_only(self):
        with TestClient(self.app) as client, patch.object(self.app.state.trading, "scan", new_callable=AsyncMock) as scan:
            scan.return_value = {"day": "2026-10-30", "results": {}}
            self.assertEqual(client.post("/api/paper/scan", json={}).status_code, 200)
            scan.assert_awaited_once_with(None, execute_live=False)

    def test_routes_headers_readiness_and_no_network(self):
        with TestClient(self.app) as client, patch("upstocks.upstox.expiries", new=AsyncMock(return_value=[])), patch("upstocks.upstox.account", new=AsyncMock(return_value={"profile": {"data": None, "error": "static IP required"}})):
            self.assertEqual(client.get("/").status_code, 200)
            for path in ("/paper", "/journal", "/calendar", "/vwap", "/backtest", "/account", "/data", "/activity"):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, path)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(len(response.headers["x-request-id"]), 32)
            health = client.get("/health/ready").json()
            self.assertEqual(health["database"], "ok")
            self.assertEqual(health["scanner"], "disabled")
            self.assertEqual(client.get("/health/live").json()["status"], "ok")
        self.assertFalse(self.app.state.ready)

    def test_durable_job_poll_and_export_after_restart(self):
        with TestClient(self.app):
            repo = self.app.state.repository
            repo.create_job("saved-job", Settings(start=date(2026, 10, 1), end=date(2026, 10, 1)), "frontend_live")
            repo.add_job_day("saved-job", {"date": "2026-10-01", "expiry": None, "note": "No trades", "trades": []})
            repo.update_job("saved-job", status="done", total=1)
        with TestClient(create_app(self.settings)) as client:
            result = client.get("/api/vwap/job/saved-job").json()
            self.assertEqual((result["status"], result["done"]), ("done", 1))
            self.assertEqual(result["settings"]["start"], "2026-10-01")
            self.assertEqual(client.get("/api/vwap/jobs").json()["jobs"][0]["id"], "saved-job")
            self.assertEqual(client.get("/api/vwap/job/saved-job/summary.csv").status_code, 200)
            self.assertEqual(client.get("/api/vwap/job/saved-job/report.pdf").status_code, 200)
            self.assertEqual(client.get("/api/vwap/job/saved-job?since=-1").status_code, 422)
            self.assertEqual(client.get("/api/vwap/job/saved-job/summary.csv?qty=0").status_code, 422)
            self.assertEqual(client.get("/api/vwap/job/missing").status_code, 404)

    def test_bad_backtest_inputs_and_note_limits(self):
        with TestClient(self.app) as client:
            base = {"start": "2026-10-01", "end": "2026-10-02"}
            for values in ({"min_premium": "NaN"}, {"live": "false"}, {"candle_minutes": 0},
                           {"entry_start": "25:61"}, {"start": "2026-10-03"},
                           {"min_premium": 900, "max_premium": 100}, {"max_trades": "bad"}):
                self.assertEqual(client.post("/api/vwap/run", json={**base, **values}).status_code, 422)
            self.assertEqual(client.post("/api/journal/note", json={"id": "x", "text": "x" * 2001}).status_code, 422)
            self.assertEqual(client.get("/api/journal?start=2026-10-12&end=2026-10-01").status_code, 422)

    def test_access_logs_do_not_record_query_or_body(self):
        with TestClient(self.app) as client:
            client.get("/api/paper?access_token=top-secret-query")
            client.post("/api/paper/config", json={"password": "top-secret-body"})
        content = (self.settings.log_dir / "access.jsonl").read_text()
        self.assertNotIn("top-secret", content)
        rows = [json.loads(line) for line in content.splitlines()]
        self.assertEqual(rows[0]["route"], "/api/paper")
        self.assertIn("duration_ms", rows[0])
        self.assertIn("request_id", rows[0])

    def test_internal_errors_have_request_id_without_exception_disclosure(self):
        @self.app.get("/test-error")
        async def fail():
            raise RuntimeError("private internal details")
        with TestClient(self.app, raise_server_exceptions=False) as client:
            response = client.get("/test-error")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private internal", response.text)
        self.assertEqual(response.json()["request_id"], response.headers["x-request-id"])
        self.assertIn("private internal", (self.settings.log_dir / "error.jsonl").read_text())


class LoggingTests(unittest.TestCase):
    def test_credentials_redacted_from_message_and_traceback(self):
        with patch.dict("os.environ", {"UPSTOX_ACCESS_TOKEN": "test-token-value"}):
            record = logging.LogRecord("upstocks", logging.ERROR, "", 1,
                                       "Bearer abc.def.ghi access_token=other-secret test-token-value", (), None)
            formatted = JsonFormatter().format(record)
            self.assertNotIn("abc.def.ghi", formatted)
            self.assertNotIn("other-secret", formatted)
            self.assertNotIn("test-token-value", formatted)
            self.assertIn("REDACTED", formatted)
            record.msg = 'password="secret with spaces" {"access_token": "other token"}'
            formatted = JsonFormatter().format(record)
            self.assertNotIn("secret with spaces", formatted)
            self.assertNotIn("other token", formatted)

    def test_rotation_is_bounded_and_handlers_are_closed(self):
        with tempfile.TemporaryDirectory() as root:
            settings = AppSettings(log_dir=Path(root), log_max_bytes=1024, log_backups=2)
            handlers = configure_logging(settings)
            logger = logging.getLogger("upstocks.test")
            try:
                for _ in range(100):
                    logger.info("test %s", "x" * 100)
            finally:
                close_logging(handlers)
            files = list(Path(root).glob("application.jsonl*"))
            self.assertEqual(len(files), 3)
            for file in files:
                for line in file.read_text().splitlines():
                    json.loads(line)


if __name__ == "__main__":
    unittest.main()
