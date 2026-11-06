import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from helpers import DashboardClient
from upstocks import brokers
from upstocks.app import create_app
from upstocks.config import AppSettings

TOKEN = "a" * 32


class BrokerTokenPasteTests(unittest.TestCase):
    """Pasting today's Kite / Upstox access token on My brokers."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.client = DashboardClient(create_app(AppSettings(
            database_path=root / "db.sqlite", log_dir=root / "logs",
            legacy_dir=root, cache_dir=root / "cache", scanner_enabled=False)))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def test_upstox_token_is_checked_then_saved(self):
        profile = {"user_id": "7XK2A1", "name": "Test", "email": "", "avatar": ""}
        with patch("upstocks.brokers.upstox_profile", new=AsyncMock(return_value=profile)) as check:
            r = self.client.post("/api/brokers/upstox/token", json={"token": TOKEN})
        self.assertEqual(r.status_code, 200, r.text)
        check.assert_awaited_once_with(TOKEN)
        self.assertTrue(r.json()["brokers"]["upstox"]["logged_in"])

    def test_rejected_token_is_not_saved(self):
        with patch("upstocks.brokers.upstox_profile", new=AsyncMock(side_effect=brokers.BrokerError("Upstox token was not accepted"))):
            r = self.client.post("/api/brokers/upstox/token", json={"token": TOKEN})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.client.get("/api/brokers").json()["brokers"]["upstox"]["logged_in"])

    def test_kite_needs_saved_api_key(self):
        with patch("upstocks.brokers.kite_profile", new=AsyncMock()) as check:
            r = self.client.post("/api/brokers/kite/token", json={"token": TOKEN})
        self.assertEqual(r.status_code, 400)
        check.assert_not_awaited()

    def test_kite_token_checked_with_saved_api_key(self):
        self.client.post("/api/brokers/keys", json={"broker": "kite", "api_key": "kiteapikey123"})
        profile = {"user_id": "AB1234", "name": "Test", "email": "", "avatar": ""}
        with patch("upstocks.brokers.kite_profile", new=AsyncMock(return_value=profile)) as check:
            r = self.client.post("/api/brokers/kite/token", json={"token": TOKEN})
        self.assertEqual(r.status_code, 200, r.text)
        check.assert_awaited_once_with("kiteapikey123", TOKEN)
        self.assertTrue(r.json()["brokers"]["kite"]["logged_in"])

    def test_copy_returns_saved_token_only_when_logged_in(self):
        self.assertEqual(self.client.post("/api/brokers/upstox/token/copy", json={}).status_code, 400)
        profile = {"user_id": "7XK2A1", "name": "Test", "email": "", "avatar": ""}
        with patch("upstocks.brokers.upstox_profile", new=AsyncMock(return_value=profile)):
            self.client.post("/api/brokers/upstox/token", json={"token": TOKEN})
        r = self.client.post("/api/brokers/upstox/token/copy", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["token"], TOKEN)
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/api/brokers/upstox/token/copy").status_code, 405)
        self.assertNotIn(TOKEN, self.client.get("/api/brokers").text)

    def test_other_brokers_and_bad_tokens_are_refused(self):
        self.assertEqual(self.client.post("/api/brokers/nope/token", json={"token": TOKEN}).status_code, 404)
        self.assertEqual(self.client.post("/api/brokers/upstox/token", json={"token": "short"}).status_code, 422)
        self.assertEqual(self.client.post("/api/brokers/upstox/token", json={"token": "has space " + TOKEN}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
