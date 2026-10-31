
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from helpers import DashboardClient
from upstocks.app import create_app
from upstocks.config import AppSettings


class LoadingApiTests(unittest.TestCase):
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

    def test_page_shells_do_not_wait_for_brokers(self):
        with patch("upstocks.upstox.account", new=AsyncMock(side_effect=RuntimeError("must not call"))) as account, patch(
            "upstocks.upstox.expiries", new=AsyncMock(side_effect=RuntimeError("must not call"))
        ) as expiries:
            for path in ["/account", "/account?broker=kite", "/data"]:
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("skeleton", response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
            account.assert_not_awaited()
            expiries.assert_not_awaited()

    def test_account_fragment_preserves_sections_and_unique_ids(self):
        sections = {key: {"data": [] if key not in ("profile", "funds") else {}, "error": None}
                    for key in ("profile", "funds", "positions", "holdings", "orders", "trades", "charges")}
        with patch("upstocks.upstox.account", new=AsyncMock(return_value=sections)):
            response = self.client.get("/api/account/view")
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-tabs="account"', response.text)
        self.assertNotIn("<script", response.text)
        shell = self.client.get("/account").text
        ids = re.findall(r'\bid="([^"]+)"', shell + response.text)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_versioned_assets_are_cacheable_but_accounts_and_errors_are_not(self):
        asset = self.client.get("/static/loading.js?v=20261031-loading")
        self.assertEqual(asset.status_code, 200)
        self.assertIn("immutable", asset.headers["cache-control"])
        self.assertEqual(asset.headers["content-encoding"], "gzip")
        unversioned = self.client.get("/static/loading.js")
        self.assertIn("must-revalidate", unversioned.headers["cache-control"])
        for path in ["/api/paper", "/api/brokers", "/static/not-found.js?v=20261031-loading"]:
            self.assertEqual(self.client.get(path).headers["cache-control"], "no-store")

    def test_expiry_endpoint_and_no_blocking_remote_page_assets(self):
        with patch("upstocks.upstox.expiries", new=AsyncMock(return_value=["2026-11-03"])):
            self.assertEqual(self.client.get("/api/expiries").json()["expiries"], ["2026-11-03"])
        for path in ["/paper", "/stocks", "/calendar", "/journal", "/brokers", "/session", "/activity", "/momentum", "/vwap"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("skeleton", response.text)
            self.assertNotIn("@import url(", response.text)
            self.assertNotRegex(response.text, r'<script src="https?://')
