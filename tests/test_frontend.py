"""Parse every inline script after Jinja renders it; no browser or broker calls."""
import re
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from helpers import DashboardClient as TestClient

from upstocks.app import create_app
from upstocks.config import AppSettings


@unittest.skipUnless(shutil.which("node"), "Node is needed for the inline JavaScript syntax check")
class FrontendTests(unittest.TestCase):
    def test_rendered_scripts_parse(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = AppSettings(database_path=root / "test.sqlite3", log_dir=root / "logs", legacy_dir=root,
                                   cache_dir=root / "cache", scanner_enabled=False)
            with TestClient(create_app(settings)) as client, patch("upstocks.upstox.expiries", new=AsyncMock(return_value=[])), patch("upstocks.upstox.account", new=AsyncMock(return_value={"profile": {"data": None, "error": "static IP required"}})):
                for page in ("paper", "stocks", "settings", "session", "momentum", "brokers", "login", "vwap", "journal", "calendar", "backtest", "data", "account", "activity"):
                    html = client.get(f"/{page}").text
                    for number, script in enumerate(re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.S)):
                        path = root / f"{page}-{number}.js"
                        path.write_text(script, encoding="utf-8")
                        result = subprocess.run([shutil.which("node"), "--check", str(path)], capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)

    def test_static_scripts_parse(self):
        folder = Path(__file__).resolve().parents[1] / "src/upstocks/static"
        for path in folder.glob("*.js"):
            result = subprocess.run([shutil.which("node"), "--check", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
