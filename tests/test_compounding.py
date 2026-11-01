import asyncio
from datetime import datetime
from types import SimpleNamespace
import unittest

from upstocks import upstox
from upstocks.services import compounding
from upstocks.services.compounding import CompoundingService, lots_for


class FakeRepo:
    def __init__(self):
        self.saved = {}
        self.days = {}

    def get_json(self, key, default=None):
        return self.saved.get(key, default)

    def set_json(self, key, value):
        import json
        self.saved[key] = json.loads(json.dumps(value))

    def journal(self, start, end, user_id=None):
        return {"days": {d: v for d, v in self.days.items() if start <= d <= end}}


def make(view=None):
    calls, log = [], []

    async def configure(preset, user=None, broker=None, **changes):
        calls.append((preset, broker, changes["lots"]))
        for acc in trading.state["accounts"].values():
            if acc["broker"] == broker:
                acc["lots"][preset] = changes["lots"]

    async def activity(m):
        log.append(m)

    trading = SimpleNamespace(state={"lot_size": 65, "accounts": {"1:kite": {"user": 1, "broker": "kite", "live": {}, "lots": {}}, "1:upstox": {"user": 1, "broker": "upstox", "live": {}, "lots": {}}}},
                              view=view or {}, configure=configure, _activity=activity)
    svc = CompoundingService(SimpleNamespace(state=SimpleNamespace(repository=FakeRepo(), trading=trading)))
    svc.load()
    return svc, calls, log


def at(day, hhmm):
    y, m, d = map(int, day.split("-")); h, mi = map(int, hhmm.split(":"))
    return datetime(y, m, d, h, mi, tzinfo=upstox.IST)


class CompoundingTests(unittest.TestCase):
    def test_shared_engine_presets_are_identical(self):
        from upstocks import live, vwap
        for preset, base in live.SAME_TRADES.items():
            self.assertEqual(vwap.PRESETS[preset], vwap.PRESETS[base])

    def test_lots_are_capital_over_per_lot_within_limits(self):
        self.assertEqual(lots_for(150_000, 75_000), 2)
        self.assertEqual(lots_for(150_000, 36_000), 4)
        self.assertEqual(lots_for(10_000, 75_000), 1)
        self.assertEqual(lots_for(5_000_000, 36_000), 10)

    def test_live_accounts_are_resized_too(self):
        svc, calls, _ = make()
        svc.app.state.trading.state["accounts"]["1:kite"]["live"] = {"harmonic_2y_compound": True}
        asyncio.run(svc.tick(at("2026-11-02", "09:05")))
        self.assertIn(("harmonic_2y_compound", "kite", 2), calls)          # live account follows the capital too
        self.assertIn(("harmonic_2y_compound", "upstox", 2), calls)

    def test_safe_withdraw_is_capital_above_ten_lots(self):
        svc, _, _ = make()
        svc.state["harmonic_2y_compound"].update(capital=900_000.0)
        self.assertEqual(svc.snapshot()["strategies"]["harmonic_2y_compound"]["safe_withdraw"], 150_000.0)
        svc.state["harmonic_2y_compound"].update(capital=300_000.0)
        self.assertEqual(svc.snapshot()["strategies"]["harmonic_2y_compound"]["safe_withdraw"], 0.0)

    def test_nothing_before_start(self):
        svc, calls, _ = make()
        asyncio.run(svc.tick(at("2026-10-30", "10:00")))
        self.assertEqual(calls, [])

    def test_month_resize_and_daily_booking(self):
        svc, calls, _ = make()
        repo = svc.app.state.repository
        repo.days = {"2026-11-02": {"harmonic_2y_compound": {"trades": [{"pnl": 100.0, "entry": 400.0, "exit": 500.0, "open": False}]}}}
        asyncio.run(svc.tick(at("2026-11-02", "09:05")))
        self.assertIn(("harmonic_2y_compound", "kite", 2), calls)
        self.assertIn(("pdh_compound", "upstox", 4), calls)
        self.assertEqual(svc.state["harmonic_2y_compound"]["capital"], 150_000)      # not before 15:35
        asyncio.run(svc.tick(at("2026-11-02", "15:35")))
        expected = round(150_000 + 100 * 130 - compounding.charges(400, 500, 130), 2)
        self.assertAlmostEqual(svc.state["harmonic_2y_compound"]["capital"], expected)
        asyncio.run(svc.tick(at("2026-11-02", "15:40")))                              # booked once
        self.assertAlmostEqual(svc.state["harmonic_2y_compound"]["capital"], expected)
        n = len(calls)
        asyncio.run(svc.tick(at("2026-11-03", "09:05")))                              # same month: no re-size
        self.assertEqual(len(calls), n)
        svc.state["harmonic_2y_compound"]["capital"] = 400_000
        asyncio.run(svc.tick(at("2026-12-01", "09:05")))
        self.assertIn(("harmonic_2y_compound", "kite", 5), calls)

    def test_missed_day_is_booked_later_from_the_journal(self):
        svc, _, _ = make()
        svc.app.state.repository.days = {"2026-11-02": {"pdh_compound": {"trades": [{"pnl": 10.0, "entry": 100.0, "exit": 110.0, "open": False}]}}}
        asyncio.run(svc.tick(at("2026-11-04", "10:00")))        # app was down on the 2nd at 15:35
        self.assertGreater(svc.state["pdh_compound"]["capital"], 150_000)

    def test_account_added_mid_month_gets_current_lots(self):
        svc, calls, _ = make()
        asyncio.run(svc.tick(at("2026-11-02", "09:05")))
        svc.app.state.trading.state["accounts"]["1:indmoney"] = {"user": 1, "broker": "indmoney", "live": {}, "lots": {"harmonic_2y_compound": 1}}
        asyncio.run(svc.tick(at("2026-11-10", "11:00")))
        self.assertIn(("harmonic_2y_compound", "indmoney", 2), calls)


if __name__ == "__main__":
    unittest.main()
