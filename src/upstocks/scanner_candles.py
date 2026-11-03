"""Bounded, read-only candle snapshots observed by scanner engines.

This module has no broker client or fetch path. Context-local capture excludes
research/download requests, even when they share an in-flight network read.
"""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
import logging

log = logging.getLogger(__name__)
_capture = ContextVar("scanner_candle_capture", default=None)
IST = timezone(timedelta(hours=5, minutes=30))


class ScannerCandles:
    MAX_INSTRUMENT_DAYS = 256
    MAX_ROWS_PER_DAY = 1440

    def __init__(self):
        self._days = OrderedDict()

    @contextmanager
    def capture(self, day: date):
        token = _capture.set((self, day))
        try:
            yield
        finally:
            _capture.reset(token)

    def record(self, instrument, interval, day, rows):
        if interval != "1m":
            return
        # Copy only the scanned session from the engine's wider contract range.
        # Merge partial ranges and replace revised minutes; never mutate engine data.
        updates = {}
        for row in rows:
            try:
                at = datetime.fromisoformat(row[0])
                at = at.replace(tzinfo=IST) if at.tzinfo is None else at.astimezone(IST)
                if at.date() == day and len(row) >= 5 and at.second == 0 and at.microsecond == 0:
                    updates[at.isoformat()] = (at.isoformat(), *row[1:])
            except (ValueError, TypeError, IndexError):
                continue
        if not updates:
            return
        key = (instrument, day)
        previous = self._days.get(key, {})
        merged = {**previous, **updates}
        self._days[key] = dict(sorted(merged.items())[-self.MAX_ROWS_PER_DAY:])
        self._days.move_to_end(key)
        while len(self._days) > self.MAX_INSTRUMENT_DAYS:
            self._days.popitem(last=False)

    def rows(self, instrument, day):
        """Return immutable rows already observed; a miss is always empty."""
        return tuple(self._days.get((instrument, day), {}).values())


def observe(instrument, interval, rows):
    capture = _capture.get()
    if capture is not None:
        store, day = capture
        try:
            store.record(instrument, interval, day, rows)
        except Exception:
            # Optional reporting must never fail an engine or alter order decisions.
            log.warning("Unable to retain scanner candles for reporting", exc_info=True)
