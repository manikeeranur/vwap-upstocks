"""Shared NSE/BSE calendar for presentation and broker-login reminders.

This metadata never infers fills or enables special-session trading.
"""

import asyncio
from calendar import monthrange
from datetime import date, datetime
import json
import logging
import sqlite3
from pathlib import Path
import time
from typing import Any

import httpx

from .. import upstox

log = logging.getLogger(__name__)
SOURCE = "https://api.upstox.com/v2/market/holidays"
EXCHANGES = ("NSE", "NFO", "BSE", "BFO")
Record = dict[str, Any]


def validate_snapshot(value: Any) -> Record:
    if not isinstance(value, dict) or type(value.get("year")) is not int:
        raise ValueError("Invalid calendar year")
    year = value["year"]
    stamp = datetime.fromisoformat(value["checked_at"])
    if not 2001 <= year <= 2100 or stamp.tzinfo is None:
        raise ValueError("Invalid calendar timestamp")
    rows = value["records"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 370:
        raise ValueError("Incomplete exchange calendar")
    seen: set[str] = set()
    covered: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid holiday record")
        day = date.fromisoformat(row["date"])
        if day.year != year or row["date"] != day.isoformat() or row["date"] in seen:
            raise ValueError("Calendar dates do not match the requested year")
        seen.add(row["date"])
        if not isinstance(row["description"], str) or not 1 <= len(row["description"]) <= 200:
            raise ValueError("Invalid holiday description")
        if row["holiday_type"] not in ("TRADING_HOLIDAY", "SETTLEMENT_HOLIDAY", "SPECIAL_TIMING"):
            raise ValueError("Unknown holiday type")
        closed, opened = row["closed_exchanges"], row["open_exchanges"]
        if (
            not isinstance(closed, list)
            or not all(isinstance(x, str) for x in closed)
            or not isinstance(opened, list)
        ):
            raise ValueError("Invalid exchange sessions")
        open_codes = set()
        for session in opened:
            if not isinstance(session, dict):
                raise ValueError("Invalid session record")
            code = session["exchange"]
            if not isinstance(code, str) or code in open_codes or code in closed:
                raise ValueError("Conflicting exchange sessions")
            open_codes.add(code)
            start = datetime.fromtimestamp(session["start_time"] / 1000, upstox.IST)
            end = datetime.fromtimestamp(session["end_time"] / 1000, upstox.IST)
            if start.date() != day or end.date() != day or end <= start:
                raise ValueError("Invalid exchange session times")
        covered.update(set(closed).intersection(EXCHANGES))
    if covered != set(EXCHANGES):
        raise ValueError("Calendar does not cover the supported exchanges")
    return value


class ExchangeCalendar:
    def __init__(self, repository):
        self.repository = repository
        self.years: dict[int, Record] = {}
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self._retry_after = 0.0
        paths = sorted((Path(__file__).parents[1] / "calendar_data").glob("*.json"))
        for path in paths:
            try:
                self._load(json.loads(path.read_text(encoding="utf-8-sig")))
            except (ValueError, OSError):
                log.warning("Ignoring unreadable bundled exchange calendar")
        try:
            cached = repository.get_json("exchange_calendar")
        except (ValueError, TypeError, sqlite3.Error):
            cached = None
            log.warning("Could not read cached exchange calendar; using bundled data")
        if isinstance(cached, dict):
            for value in cached.values():
                self._load(value)
        elif cached is not None:
            log.warning("Ignoring invalid cached exchange calendar")

    def _load(self, value):
        try:
            snapshot = validate_snapshot(value)
            previous = self.years.get(snapshot["year"])
            if previous is None or datetime.fromisoformat(snapshot["checked_at"]) > datetime.fromisoformat(
                previous["checked_at"]
            ):
                self.years[snapshot["year"]] = snapshot
        except (ValueError, KeyError, TypeError, OverflowError, OSError):
            log.warning("Ignoring invalid saved exchange calendar")

    async def _fetch(self) -> list[Record]:
        # Public endpoint: holiday lookup must not depend on the token it guards.
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(SOURCE, headers={"Accept": "application/json"})
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict) or body.get("status") != "success":
            raise ValueError("Holiday feed unavailable")
        return body["data"]

    async def refresh(self, now: datetime | None = None) -> None:
        now = (now or datetime.now(upstox.IST)).astimezone(upstox.IST)
        async with self._lock:
            saved = self.years.get(now.year)
            if saved and 0 <= (now - datetime.fromisoformat(saved["checked_at"])).total_seconds() < 21600:
                return
            if time.monotonic() < self._retry_after:
                return
            self._retry_after = time.monotonic() + 900
            try:
                records = await self._fetch()
                value = validate_snapshot(
                    {"year": now.year, "checked_at": now.isoformat(), "source": SOURCE, "records": records}
                )
                updated = {**self.years, now.year: value}
                await asyncio.to_thread(
                    self.repository.set_json, "exchange_calendar", {str(k): v for k, v in updated.items()}
                )
                self.years = updated
                self.last_error = None
            except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError, OSError, sqlite3.Error):
                self.last_error = (
                    "Could not refresh holiday dates; showing the last verified calendar where available."
                )
                log.warning(
                    "Exchange calendar refresh failed; retained the last verified calendar", exc_info=True
                )

    def day(self, day: date) -> Record:
        snapshot = self.years.get(day.year)
        event = (
            next((r for r in snapshot["records"] if r["date"] == day.isoformat()), None) if snapshot else None
        )
        weekend = day.weekday() >= 5
        exchanges = {}
        for code in EXCHANGES:
            session = (
                next((r for r in event["open_exchanges"] if r["exchange"] == code), None) if event else None
            )
            label, kind = (
                ("Weekend", "weekend")
                if weekend
                else ("Trading day", "open")
                if snapshot
                else ("Calendar unavailable", "unknown")
            )
            times = None
            if event:
                if code in event["closed_exchanges"]:
                    label, kind = event["description"], "holiday"
                elif session:
                    label = event["description"]
                    kind = "special" if weekend or event["holiday_type"] == "SPECIAL_TIMING" else "open"
                    times = (
                        "–".join(
                            datetime.fromtimestamp(session[k] / 1000, upstox.IST).strftime("%H:%M")
                            for k in ("start_time", "end_time")
                        )
                        + " IST"
                    )
            exchanges[code] = {"kind": kind, "label": label, "session": times}
        return {
            "date": day.isoformat(),
            "weekend": weekend,
            "exchanges": exchanges,
            "verified": bool(snapshot),
            "login_alerts_required": not weekend
            and any(exchanges[x]["kind"] in ("open", "special") for x in EXCHANGES),
            "checked_at": snapshot["checked_at"] if snapshot else None,
        }

    def month(self, year: int, month: int) -> Record:
        return {
            "year": year,
            "month": month,
            "source": SOURCE,
            "warning": self.last_error,
            "verified": year in self.years,
            "days": {
                f"{year:04d}-{month:02d}-{d:02d}": self.day(date(year, month, d))
                for d in range(1, monthrange(year, month)[1] + 1)
            },
        }
