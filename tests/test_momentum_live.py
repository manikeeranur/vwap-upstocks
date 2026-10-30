import asyncio
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest import mock

from upstocks import brokers, upstox
from upstocks.services import momentum_live
from upstocks.services.momentum_live import MomentumLiveService, decide


def _r(rank, symbol, above=True):
    return {"symbol": symbol, "key": f"NSE_EQ|INE{symbol}", "rank": rank, "above_sma": above, "close": 100.0}


class FakeRepo:
    def __init__(self):
        self.saved = None

    def get_json(self, key, default=None):
        return self.saved if self.saved is not None else default

    def set_json(self, key, value):
        import json
        self.saved = json.loads(json.dumps(value))

    def users(self):
        return [{"id": 1, "telegram_chat_id": ""}]


class FakeCreds:
    def token_valid(self, now=None):
        return True


def make_service(rankings):
    activity = []

    async def _activity(m):
        activity.append(m)

    async def send(chat, text):
        return True

    async def live_ranking(st):
        return rankings.pop(0)

    trading = SimpleNamespace(state={"accounts": {"1:kite": {"broker": "kite", "user": 1}, "1:upstox": {"broker": "upstox", "user": 1}}},
                              _activity=_activity, _credentials=lambda acc: FakeCreds())
    app = SimpleNamespace(state=SimpleNamespace(repository=FakeRepo(), trading=trading, reminders=SimpleNamespace(send=send),
                                                momentum=SimpleNamespace(live_ranking=live_ranking)))
    svc = MomentumLiveService(app)
    svc.load()
    return svc, activity


def at(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    y, mo, d = map(int, day.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=upstox.IST)


class DecideTests(unittest.TestCase):
    def test_empty_buys_best_above_average(self):
        sell, buy, _ = decide([_r(1, "AAA", False), _r(2, "BBB")], None, 5)
        self.assertIsNone(sell)
        self.assertEqual(buy["symbol"], "BBB")

    def test_keep_while_rank_within_exit(self):
        sell, buy, reason = decide([_r(1, "AAA"), _r(5, "HELD")], "HELD", 5)
        self.assertIsNone(sell)
        self.assertIsNone(buy)
        self.assertIn("Keep", reason)

    def test_sell_when_rank_drops_or_below_average(self):
        sell, buy, _ = decide([_r(1, "AAA"), _r(6, "HELD")], "HELD", 5)
        self.assertEqual(sell["symbol"], "HELD")
        self.assertEqual(buy["symbol"], "AAA")
        sell, _, reason = decide([_r(1, "AAA"), _r(2, "HELD", False)], "HELD", 5)
        self.assertEqual(sell["symbol"], "HELD")
        self.assertIn("average", reason)


class ScheduleTests(unittest.TestCase):
    def run_ticks(self, svc, ticks, prices, orders, held=10**6, fail=False):
        async def ltp(keys):
            return {k: prices[k] for k in keys}

        async def candles(*a, **k):
            return [["x"]]

        async def place(creds, symbol, isin, qty, side, tag):
            if fail:
                raise brokers.BrokerError("rejected")
            orders.append((side, symbol, qty))
            return f"OID{len(orders)}"

        async def equity_held(creds, isin):
            return held

        async def go():
            with mock.patch.object(upstox, "ltp_quotes", ltp), mock.patch.object(upstox, "candles", candles), \
                    mock.patch.object(brokers, "place_equity_order", place), mock.patch.object(brokers, "equity_held", equity_held):
                for t in ticks:
                    await svc.tick(t)
        asyncio.run(go())

    def test_nothing_before_start(self):
        svc, _ = make_service([])
        orders = []
        self.run_ticks(svc, [at("2026-10-30", "12:00")], {}, orders)
        self.assertIsNone(svc.state["plan"])
        self.assertEqual(orders, [])

    def test_first_switch_buys_then_rotates(self):
        svc, activity = make_service([
            {"date": "2026-10-30", "failed": 0, "ranking": [_r(1, "AAA"), _r(2, "BBB")]},
            {"date": "2026-11-30", "failed": 0, "ranking": [_r(1, "BBB"), _r(7, "AAA")]},
        ])
        svc.state["live"]["kite"] = True
        orders = []
        prices = {"NSE_EQ|INEAAA": 100.0, "NSE_EQ|INEBBB": 50.0}
        self.run_ticks(svc, [at("2026-11-02", "09:30"), at("2026-11-02", "11:00"), at("2026-11-02", "12:00")], prices, orders)
        h = svc.state["holding"]
        self.assertEqual((h["symbol"], h["qty"], h["entry_time"]), ("AAA", 499, "12:00"))   # ₹50,000 / (100 × 1.001)
        self.assertEqual(orders, [("BUY", "AAA", 499)])
        self.assertEqual(svc.state["positions"]["kite"]["qty"], 499)
        self.assertNotIn("upstox", svc.state["positions"])                                   # upstox live off
        # December: AAA fell to rank 7 → sell at 11:00 (only after 11:00), buy BBB at 12:00 with the proceeds
        prices["NSE_EQ|INEAAA"] = 120.0
        self.run_ticks(svc, [at("2026-12-01", "09:30"), at("2026-12-01", "10:59")], prices, orders, held=499)
        self.assertEqual(svc.state["holding"]["symbol"], "AAA")
        self.run_ticks(svc, [at("2026-12-01", "11:00"), at("2026-12-01", "12:00")], prices, orders, held=499)
        self.assertEqual(orders[1:], [("SELL", "AAA", 499), ("BUY", "BBB", svc.state["holding"]["qty"])])
        self.assertEqual(svc.state["holding"]["symbol"], "BBB")
        self.assertEqual(len(svc.state["history"]), 1)
        self.assertGreater(svc.state["history"][0]["pnl"], 0)
        self.assertTrue(svc.state["plan"]["finished"])

    def test_live_sell_never_exceeds_shares_held(self):
        svc, _ = make_service([{"date": "2026-11-30", "failed": 0, "ranking": [_r(1, "BBB"), _r(9, "AAA")]}])
        svc.state.update(holding={"symbol": "AAA", "key": "NSE_EQ|INEAAA", "isin": "INEAAA", "qty": 499, "entry": 100,
                                  "entry_date": "2026-11-02", "entry_time": "12:00", "rank": 1, "cost": 49950},
                         cash=0.0, positions={"kite": {"symbol": "AAA", "isin": "INEAAA", "qty": 499}},
                         live={"upstox": False, "kite": True, "indmoney": False})
        orders = []
        self.run_ticks(svc, [at("2026-12-01", "09:30"), at("2026-12-01", "11:00")], {"NSE_EQ|INEAAA": 110.0}, orders, held=200)
        self.assertEqual(orders, [("SELL", "AAA", 200)])

    def test_first_switch_buys_rank_one_even_if_long_held(self):
        svc, _ = make_service([{"date": "2026-10-30", "failed": 0, "ranking": [_r(1, "CUPID"), _r(2, "BBB")]}])
        orders = []
        self.run_ticks(svc, [at("2026-11-02", "09:30"), at("2026-11-02", "12:00")], {"NSE_EQ|INECUPID": 320.0}, orders)
        self.assertEqual(svc.state["holding"]["symbol"], "CUPID")

    def test_paused_does_nothing(self):
        svc, _ = make_service([{"date": "2026-10-30", "failed": 0, "ranking": [_r(1, "AAA")]}])
        svc.state["on"] = False
        orders = []
        self.run_ticks(svc, [at("2026-11-02", "09:30"), at("2026-11-02", "12:00")], {"NSE_EQ|INEAAA": 100.0}, orders)
        self.assertIsNone(svc.state["plan"])
        self.assertIsNone(svc.state["holding"])

    def test_missed_first_trading_day_skips_month(self):
        svc, _ = make_service([{"date": "2026-11-02", "failed": 0, "ranking": [_r(1, "AAA")]}])
        orders = []
        self.run_ticks(svc, [at("2026-11-03", "10:00"), at("2026-11-03", "12:00")], {"NSE_EQ|INEAAA": 100.0}, orders)
        self.assertIn("skip", svc.state["plan"])
        self.assertIsNone(svc.state["holding"])
        self.assertEqual(orders, [])

    def test_failed_order_is_not_retried(self):
        svc, _ = make_service([{"date": "2026-10-30", "failed": 0, "ranking": [_r(1, "AAA")]}])
        svc.state["live"]["kite"] = True
        orders = []
        self.run_ticks(svc, [at("2026-11-02", "09:30"), at("2026-11-02", "12:00"), at("2026-11-02", "12:01")],
                       {"NSE_EQ|INEAAA": 100.0}, orders, fail=True)
        sent = [o for o in svc.state["orders"]]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["status"], "unknown")
        self.assertNotIn("kite", svc.state["positions"])
        self.assertEqual(svc.state["holding"]["symbol"], "AAA")   # paper still bought


if __name__ == "__main__":
    unittest.main()
