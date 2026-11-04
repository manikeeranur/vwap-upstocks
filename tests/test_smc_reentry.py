"""SMC entries must be possible at the time of the scan that publishes them."""
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from upstocks import upstox
from upstocks.database import Database
from upstocks.repository import Repository
from upstocks.services.trading import TradingService


class SmcReentryTests(unittest.IsolatedAsyncioTestCase):
    day = date(2026, 11, 4)
    preset = "smc_live"

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        db = Database(root / "paper.sqlite3")
        db.initialize()
        self.repo = Repository(db)
        self.repo.migrate_legacy(root)
        self.repo.ensure_presets()
        self.scanner = TradingService(self.repo)
        for preset, config in self.scanner.state["config"].items():
            config["on"] = preset == self.preset

        self.spot = []
        self.option = []
        for minute in range(9 * 60 + 15, 10 * 60 + 16):
            stamp = f"{minute // 60:02}:{minute % 60:02}"
            close = 343.2 if stamp < "09:32" else 342.1 if stamp < "09:50" else 321.5
            self.spot.append((stamp, 23930, 23930, 23930, 23930, 0))
            self.option.append([f"{self.day}T{stamp}:00+05:30", close, close, close, close, 650])
        contract = {"instrument_key": "NSE_FO|fixture", "trading_symbol": "NIFTY 24150 PE", "strike_price": 24150,
                    "instrument_type": "PE"}
        self.clock = self.enterContext(patch("upstocks.live.now_ist"))
        self.enterContext(patch("upstocks.upstox.today_ist", return_value=self.day))
        self.enterContext(patch("upstocks.live.market_open", return_value=True))
        self.enterContext(patch("upstocks.vwap.trading_days_and_spot", AsyncMock(return_value={str(self.day): self.spot})))
        self.enterContext(patch("upstocks.smc._nearest_expiry", AsyncMock(return_value="2026-11-09")))
        self.enterContext(patch("upstocks.upstox.expired_expiries", AsyncMock(return_value=[])))
        self.enterContext(patch("upstocks.upstox.contracts", AsyncMock(return_value=[contract])))
        self.enterContext(patch("upstocks.smc.day_rows", AsyncMock(return_value=self.option)))
        self.enterContext(patch("upstocks.smc.analyze", self.signal))
        self.enterContext(patch.object(self.scanner, "_peaks", AsyncMock()))

    @staticmethod
    def signal(bars, spot):
        side = dict(score=0, bonus=0, trendOk=False, concepts=[])
        return {"bull": side, "bear": {**side, "score": 2 if bars[-1][0] >= "09:30" else 0}}

    async def asyncTearDown(self):
        await self.scanner.stop()

    async def scan_at(self, hour, minute):
        self.clock.return_value = datetime(2026, 11, 4, hour, minute, 22, tzinfo=upstox.IST)
        view = await self.scanner.scan(execute_live=False)
        self.assertFalse(view["errors"])
        trades = view["results"][self.preset]
        card = self.scanner.snapshot(intraday=True)["view"]["results"][self.preset]
        saved = self.repo.paper_view(str(self.day))["results"][self.preset]
        self.assertEqual([t["id"] for t in trades], [t["id"] for t in card])
        self.assertEqual([t["id"] for t in trades], [t["id"] for t in saved])
        return trades

    async def test_open_loss_then_stop_and_full_cooldown(self):
        first, = await self.scan_at(9, 32)
        self.assertEqual((first["entry_time"], first["entry"], first["exit_time"]), ("09:32", 343.2, None))
        first, = await self.scan_at(9, 33)
        self.assertTrue(first["open"])
        self.assertLess(first["pnl"], 0)
        first, = await self.scan_at(9, 53)
        self.assertEqual((first["status"], first["exit_time"]), ("SL", "09:51"))
        self.assertEqual(len(await self.scan_at(10, 6)), 1)
        first, second = await self.scan_at(10, 7)
        self.assertEqual((second["entry_time"], second["open"]), ("10:07", True))
        self.assertEqual([t["id"] for t in await self.scan_at(10, 7)], [first["id"], second["id"]])

    async def test_delayed_index_or_option_candles_cannot_create_reentry(self):
        complete_spot = list(self.spot)
        self.spot[:] = [b for b in self.spot if b[0] <= "09:50"]
        self.assertEqual(len(await self.scan_at(10, 15)), 1)
        self.spot[:] = complete_spot
        self.option[:] = [b for b in self.option if b[0][11:16] <= "09:51"]
        self.assertEqual(len(await self.scan_at(10, 15)), 1)

    async def test_saved_pre_fix_snapshot_hides_unconfirmed_entry(self):
        first, = await self.scan_at(9, 53)
        self.scanner.view["results"][self.preset].append({**first, "entry_time": "10:07", "open": True})
        self.clock.return_value = datetime(2026, 11, 4, 10, 15, tzinfo=upstox.IST)
        view = self.scanner.snapshot(intraday=True)["view"]
        self.assertEqual([t["entry_time"] for t in view["results"][self.preset]], ["09:32"])
        self.assertEqual(len(self.scanner.view["results"][self.preset]), 2)
