import unittest
from dataclasses import replace
from datetime import date

from upstocks import vwap
from upstocks.vol_breakout import atr, resolve, signal


def bar(t, o, h, l, c):
    return (t, o, h, l, c, 1.0)


S = replace(vwap.Settings(start=date(2026, 1, 1), end=date(2026, 1, 1)), **vwap.PRESETS["vol_breakout"])


class SignalTest(unittest.TestCase):
    def test_atr_is_the_average_daily_range(self):
        self.assertEqual(atr([[bar("09:15", 0, 110, 100, 0)], [bar("09:15", 0, 130, 100, 0)]]), 20)

    def test_two_closes_beyond_a_level_enter(self):
        bars = [bar("09:15", 100, 101, 99, 100), bar("09:16", 100, 106, 100, 105), bar("09:17", 105, 107, 104, 106)]
        self.assertEqual(signal(bars, 104, 96, 2, "09:16", "10:00"), (2, "CE"))
        down = [bar("09:15", 100, 101, 99, 100), bar("09:16", 100, 100, 94, 95), bar("09:17", 95, 96, 93, 94)]
        self.assertEqual(signal(down, 104, 96, 2, "09:16", "10:00"), (2, "PE"))

    def test_one_close_or_a_wick_is_not_enough(self):
        bars = [bar("09:15", 100, 101, 99, 100), bar("09:16", 100, 106, 100, 105), bar("09:17", 105, 110, 101, 103),
                bar("09:18", 103, 108, 102, 103)]
        self.assertIsNone(signal(bars, 104, 96, 2, "09:16", "10:00"))

    def test_signals_after_the_last_entry_are_skipped(self):
        bars = [bar("09:15", 100, 101, 99, 100), bar("10:01", 100, 106, 100, 105), bar("10:02", 105, 107, 104, 106)]
        self.assertIsNone(signal(bars, 104, 96, 2, "09:16", "10:00"))


class ResolveTest(unittest.TestCase):
    def test_stop_loss_at_ten_percent(self):
        ob = [bar("09:18", 100, 102, 99, 101), bar("09:19", 101, 101, 88, 90)]
        self.assertEqual(resolve(ob, 0, S)[:3], ("SL", 90, "09:19"))

    def test_trail_gives_back_half_the_best_gain(self):
        ob = [bar("09:18", 100, 101, 99, 100), bar("09:19", 100, 140, 100, 138), bar("09:20", 138, 139, 115, 116)]
        status, price, t, peak, trailed = resolve(ob, 0, S)
        self.assertEqual((status, price, t, peak, trailed), ("TRAIL", 120, "09:20", 40, True))

    def test_held_to_the_last_minute(self):
        ob = [bar("09:18", 100, 105, 98, 104), bar("15:29", 104, 106, 103, 105)]
        self.assertEqual(resolve(ob, 0, S)[:3], ("TIME_EXIT", 105, "15:29"))


if __name__ == "__main__":
    unittest.main()
