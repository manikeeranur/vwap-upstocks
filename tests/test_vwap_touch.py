import unittest

from upstocks.vwap_touch import signal


def bar(t, o, h, l, c):
    return (t, o, h, l, c, 1.0)


ARM_TOUCH = [bar("09:15", 100, 101, 99, 100),    # first candle: never arms
             bar("09:16", 100, 106, 100, 105),   # close above VWAP → armed
             bar("09:17", 105, 106, 102, 103)]   # low within 2 % of VWAP, close above it → touch (high 106)


class SignalTest(unittest.TestCase):
    def test_close_above_touch_high_enters_when_the_candle_completes(self):
        bars = ARM_TOUCH + [bar("09:18", 103, 108, 103, 107), bar("09:19", 107, 108, 106, 107)]
        j, entry, candle, _vwap, touch = signal(bars, 1, 2.0, "09:15", "14:30")
        self.assertEqual((j, entry, candle[0], touch[0]), (4, 107, "09:18", "09:17"))

    def test_close_below_vwap_starts_over(self):
        bars = ARM_TOUCH + [bar("09:18", 103, 104, 99, 100), bar("09:19", 100, 108, 100, 107), bar("09:20", 107, 108, 106, 107)]
        self.assertIsNone(signal(bars, 1, 2.0, "09:15", "14:30"))

    def test_confirmation_outside_the_entry_window_is_skipped(self):
        bars = ARM_TOUCH + [bar("09:18", 103, 108, 103, 107), bar("09:19", 107, 108, 106, 107)]
        self.assertIsNone(signal(bars, 1, 2.0, "09:20", "14:30"))
        self.assertIsNone(signal(bars, 1, 2.0, "09:15", "09:18"))


if __name__ == "__main__":
    unittest.main()
