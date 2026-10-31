from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from helpers import DashboardClient
from upstocks.accounts import SESSION_COOKIE
from upstocks.app import create_app
from upstocks.config import AppSettings


class StockApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.app = create_app(
            AppSettings(
                database_path=root / "db.sqlite",
                log_dir=root / "logs",
                cache_dir=root / "cache",
                legacy_dir=root,
                scanner_enabled=False,
            )
        )

    def test_user_scope_and_revision_are_enforced(self):
        with DashboardClient(self.app) as client:
            repo = self.app.state.repository
            uid = repo.add_user("other", "!")
            self.app.state.trading.reload_people()
            first = client.get("/api/stocks").json()
            self.assertEqual({p["user_id"] for p in first["portfolios"]}, {1})
            body = {
                "broker": "paper",
                "revision": 0,
                "usable_capital": 25000,
                "enabled": True,
                "paused": False,
            }
            self.assertEqual(client.post("/api/stocks/config", json=body).status_code, 200)
            self.assertEqual(client.post("/api/stocks/config", json=body).status_code, 409)
            client.cookies.clear()
            client.cookies.set(SESSION_COOKIE, self.app.state.signer.session(uid))
            other = client.get("/api/stocks").json()
            self.assertEqual({p["user_id"] for p in other["portfolios"]}, {uid})
            self.assertEqual(other["portfolios"][0]["usable_capital"], 5_000_000)
            self.assertEqual(client.post("/api/stocks/config", json={**body, "user_id": 1}).status_code, 422)

    def test_live_confirmation_and_separate_controls(self):
        with DashboardClient(self.app) as client:
            body = {
                "broker": "kite",
                "revision": 0,
                "usable_capital": 50000,
                "enabled": True,
                "paused": False,
            }
            self.assertEqual(client.post("/api/stocks/config", json=body).status_code, 422)
            self.assertEqual(client.post("/api/momentum/tuned/on", json={"on": False}).status_code, 409)
            self.assertEqual(
                client.post(
                    "/api/momentum/tuned/live", json={"broker": "kite", "live": True, "confirm": "LIVE"}
                ).status_code,
                409,
            )
            for value in (-1, 0, "NaN", 1e20):
                self.assertEqual(
                    client.post(
                        "/api/stocks/config", json={**body, "broker": "paper", "usable_capital": value}
                    ).status_code,
                    422,
                )
            html = client.get("/stocks").text
            self.assertIn('id="usable-capital"', html)
            self.assertIn("Pause new entries; continue exits", html)
            self.assertEqual(client.get("/static/stocks.js").status_code, 200)

    def test_readiness_detects_dead_stock_worker(self):
        from dataclasses import replace
        from upstocks.services.trading import TradingService
        from upstocks.services.momentum_live import MomentumLiveService
        from upstocks.services.reminders import ReminderService

        app = create_app(replace(self.app.state.settings, scanner_enabled=True))
        with (
            patch.object(TradingService, "start"),
            patch.object(MomentumLiveService, "start"),
            patch.object(ReminderService, "start"),
            patch.object(TradingService, "worker_health", return_value={"running": True, "stale": False}),
            patch.object(ReminderService, "worker_health", return_value={"running": True, "stale": False}),
            patch.object(
                MomentumLiveService, "worker_health", return_value={"running": False, "stale": False}
            ) as stock_health,
        ):
            with DashboardClient(app) as client:
                self.assertEqual(client.get("/health/ready").status_code, 503)
                stock_health.return_value = {"running": True, "stale": False}
                self.assertEqual(client.get("/health/ready").status_code, 200)
                stock_health.return_value = {"running": True, "stale": True}
                self.assertEqual(client.get("/health/ready").status_code, 503)

    def test_authenticated_user_cannot_reconcile_another_portfolio(self):
        with DashboardClient(self.app) as client:
            with patch.object(
                self.app.state.momentum_live, "reconcile", AsyncMock(return_value={"ok": True})
            ) as reconcile:
                self.assertEqual(
                    client.post("/api/stocks/reconcile", json={"broker": "paper"}).status_code, 200
                )
                self.assertEqual(reconcile.await_args.args, (1, "paper"))
            self.assertEqual(
                client.post(
                    "/api/stocks/reconcile", json={"broker": "paper", "portfolio_id": "2:kite"}
                ).status_code,
                422,
            )

    def test_statement_preview_apply_and_recovery_commands_use_session_scope(self):
        import csv
        from datetime import datetime
        import io
        from upstocks import execution_brokers, upstox
        from upstocks.stocks.statements import COLUMNS

        with DashboardClient(self.app) as client:
            svc = self.app.state.momentum_live
            client.get("/api/stocks")
            svc.store.configure(
                "1:kite", 0, enabled=True, paused=False, usable_capital=5000000, client_id="owner"
            )
            inst = {"symbol": "AAA", "isin": "INEAAA", "key": "NSE_EQ|INEAAA"}
            order = svc.store.reserve("1:kite", "test", "BUY", inst, 10, 10000, "owner")
            svc.store.apply(
                order["id"], {"order_id": "b1", "filled_qty": 10, "notional": 100000, "status": "complete"}
            )
            out = io.StringIO()
            writer = csv.DictWriter(out, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerow(
                {
                    "client_id": "owner",
                    "order_id": "b1",
                    "intent_id": order["id"],
                    "symbol": "AAA",
                    "isin": "INEAAA",
                    "side": "BUY",
                    "quantity": 10,
                    "filled_quantity": 10,
                    "average_price": 100,
                    "status": "complete",
                    "trade_date": datetime.fromisoformat(order["created_at"])
                    .astimezone(upstox.IST)
                    .date()
                    .isoformat(),
                    "total_charges": 5,
                }
            )
            body = {"broker": "kite", "reference": "test-note", "csv_text": out.getvalue()}
            with (
                patch.object(svc, "_creds", AsyncMock(return_value=object())),
                patch.object(
                    execution_brokers, "stock_snapshot", AsyncMock(return_value={"client_id": "owner"})
                ),
            ):
                preview = client.post("/api/reconciliation/preview", json=body)
            self.assertEqual(preview.status_code, 200, preview.text)
            doc = preview.json()["document_id"]
            self.assertEqual(
                client.post("/api/reconciliation/apply", json={"document_id": doc}).status_code, 422
            )
            applied = client.post("/api/reconciliation/apply", json={"document_id": doc, "confirm": "IMPORT"})
            self.assertEqual(applied.status_code, 200, applied.text)
            p = next(p for p in svc.store.snapshot(1) if p["broker"] == "kite")
            self.assertTrue(p["paused"])
            self.assertEqual(p["orders"][0]["charges_paise"], 500)
            self.assertEqual(
                client.post(
                    "/api/stocks/recovery",
                    json={
                        "broker": "kite",
                        "revision": p["revision"],
                        "order_id": order["id"],
                        "confirm": "EXIT",
                        "user_id": 2,
                    },
                ).status_code,
                422,
            )
            self.assertEqual(client.get("/api/reconciliation/template.csv").status_code, 200)
