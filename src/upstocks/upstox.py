"""Upstox API client — only the read-only market-data calls this app needs.

Verified live against the account's token (2026-10-09):
- Expired expiries / contracts / candles need the Plus plan (this token has it).
  NIFTY expired contracts go back to 2024-10-03.
- Expired candle intervals: 1minute 3minute 5minute 15minute 30minute day.
  A whole contract life of 1-minute data comes back in one call.
- Live (not yet expired) contracts: /v3/historical-candle covers up to
  yesterday; today's candles only come from /v3/historical-candle/intraday,
  so a range ending today is the two merged.
- Candles arrive newest-first as [ts, open, high, low, close, volume, oi].
- /v2/user/profile is blocked for this token (static-IP restricted) — not used.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

from collections import deque
from pathlib import Path
import hashlib
import json

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

BASE = "https://api.upstox.com"
NIFTY = "NSE_INDEX|Nifty 50"
IST = timezone(timedelta(hours=5, minutes=30))

# UI label -> (expired v2 interval, live v3 unit, live v3 interval)
INTERVALS: dict[str, tuple[str, str, str]] = {
    "1m":  ("1minute", "minutes", "1"),
    "3m":  ("3minute", "minutes", "3"),
    "5m":  ("5minute", "minutes", "5"),
    "15m": ("15minute", "minutes", "15"),
    "30m": ("30minute", "minutes", "30"),
    "1d":  ("day", "days", "1"),
}

# Keep each request well inside the API's range limits.
CHUNK_DAYS = 28


class UpstoxError(RuntimeError):
    pass


def _token() -> str:
    t = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not t:
        raise UpstoxError("UPSTOX_ACCESS_TOKEN is not set (upstocks/.env)")
    return t


_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=BASE, timeout=30, http2=False)
    return _client


# Upstox limits: 50/s, 500/min AND 2000 per 30 min. Spacing alone (8/s)
# still blew the 30-min budget across a few multi-week runs, so this keeps a
# sliding window of request start times and waits until a call fits under
# all three (with headroom). Past data is cached on disk, so most re-runs
# don't call Upstox at all.
_MIN_GAP = 0.125
_WINDOWS = ((60.0, 450), (1800.0, 1900))
_rate_lock = asyncio.Lock()
_last_start = 0.0
_starts: deque[float] = deque()


async def _throttle() -> None:
    global _last_start
    async with _rate_lock:
        while True:
            now = time.monotonic()
            while _starts and now - _starts[0] > _WINDOWS[-1][0]:
                _starts.popleft()
            wait = _last_start + _MIN_GAP - now
            for span, cap in _WINDOWS:
                in_win = [t for t in _starts if now - t < span]
                if len(in_win) >= cap:
                    wait = max(wait, in_win[0] + span - now + 0.05)
            if wait <= 0:
                break
            await asyncio.sleep(min(wait, 5.0))
        _last_start = time.monotonic()
        _starts.append(_last_start)


async def _get(path: str, params: dict | None = None) -> dict:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {_token()}"}
    for attempt in range(6):
        await _throttle()
        r = await client().get(path, params=params, headers=headers)
        if r.status_code == 429 and attempt < 5:  # still limited (e.g. budget used elsewhere) — wait it out
            await asyncio.sleep(min(15.0 * (attempt + 1), 60.0))
            continue
        break
    try:
        body = r.json()
    except ValueError:
        raise UpstoxError(f"Upstox {r.status_code}: {r.text[:200]}")
    if r.status_code != 200 or body.get("status") != "success":
        errs = body.get("errors") or [{}]
        raise UpstoxError(f"Upstox {r.status_code}: {errs[0].get('message') or body}")
    return body


# ─── Small TTL cache (contract lists change at most daily; expired ones never) ─
_cache: dict[str, tuple[float, object]] = {}
_inflight: dict[str, asyncio.Future] = {}

# ─── Disk cache for data that can never change (past candles, expired
# contract lists) — survives restarts, so re-runs cost no API calls. ────────
_DISK = Path(__file__).resolve().parents[2] / ".cache"


async def _disk_cached(key: str, fn):
    if key in _cache:
        return _cache[key][1]
    f = _DISK / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    if f.exists():
        try:
            val = json.loads(f.read_text())
            _cache[key] = (time.time(), val)
            return val
        except (OSError, ValueError):
            pass
    val = await fn()
    _cache[key] = (time.time(), val)
    try:
        _DISK.mkdir(exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(val))
        tmp.replace(f)
    except OSError:
        pass
    return val


async def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and (ttl <= 0 or time.time() - hit[0] < ttl):
        return hit[1]
    val = await fn()
    _cache[key] = (time.time(), val)
    return val


def today_ist() -> date:
    return datetime.now(IST).date()


async def expired_expiries() -> list[str]:
    async def f():
        return (await _get("/v2/expired-instruments/expiries", {"instrument_key": NIFTY}))["data"]
    return await _cached("expired_expiries", 3600, f)


async def live_contracts() -> list[dict]:
    async def f():
        return (await _get("/v2/option/contract", {"instrument_key": NIFTY}))["data"]
    return await _cached("live_contracts", 3600, f)


async def expiries() -> list[dict]:
    """All NIFTY expiries, newest first: [{date, expired}]."""
    exp, live = await asyncio.gather(expired_expiries(), live_contracts())
    out = {d: True for d in exp}
    for c in live:
        out.setdefault(c["expiry"], False)
    return [{"date": d, "expired": out[d]} for d in sorted(out, reverse=True)]


async def contracts(expiry: str) -> list[dict]:
    """Option contracts of one expiry, sorted by strike then CE/PE."""
    exp = set(await expired_expiries())
    if expiry in exp:
        async def f():
            return (await _get("/v2/expired-instruments/option/contract",
                               {"instrument_key": NIFTY, "expiry_date": expiry}))["data"]
        rows = await _disk_cached(f"contracts:{expiry}", f)  # expired: never changes
    else:
        rows = [c for c in await live_contracts() if c["expiry"] == expiry]
    return sorted(rows, key=lambda c: (c["strike_price"], c["instrument_type"]))


def _chunks(start: date, end: date):
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=CHUNK_DAYS - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


async def candles(instrument_key: str, interval: str, start: date, end: date, expired: bool) -> list[list]:
    """Candles oldest-first: [iso_ts, open, high, low, close, volume, oi].
    Ranges that end 2+ days ago never change, so they're cached for good —
    the premium search and the download that follows reuse the same data."""
    key_ = f"candles:{instrument_key}:{interval}:{start}:{end}"
    # Only cache ranges that end 2+ days ago: Upstox publishes a session's
    # historical candles some time after it closes (e.g. on Saturday night
    # Friday wasn't there yet), so a recent range can still grow.
    today = today_ist()
    if not expired and start < today <= end:
        # A range running into today = the days before (cached for a while)
        # + today's intraday (refreshed each minute): the live scan re-reads
        # these every minute and mustn't re-download the past days each time.
        hist = await candles(instrument_key, interval, start, today - timedelta(days=1), expired)
        intra = await candles(instrument_key, interval, today, end, expired)
        return hist + [r for r in intra if r[0][:10] >= today.isoformat()]
    if key_ in _inflight:  # same range already being fetched (parallel days) — share it
        return await _inflight[key_]
    if end <= today - timedelta(days=2):
        fut = asyncio.ensure_future(_disk_cached(key_, lambda: _fetch_candles(instrument_key, interval, start, end, expired)))
    else:  # recent: may still grow — today's for a minute, yesterday's for 15 min
        ttl = 50 if end >= today else 900
        fut = asyncio.ensure_future(_cached(key_, ttl, lambda: _fetch_candles(instrument_key, interval, start, end, expired)))
    _inflight[key_] = fut
    try:
        return await fut
    finally:
        _inflight.pop(key_, None)


async def _fetch_candles(instrument_key: str, interval: str, start: date, end: date, expired: bool) -> list[list]:
    if interval not in INTERVALS:
        raise UpstoxError(f"unknown interval {interval!r}")
    v2_iv, unit, iv = INTERVALS[interval]
    key = quote(instrument_key, safe="")
    rows: list[list] = []

    if expired:
        for a, b in _chunks(start, end):
            d = await _get(f"/v2/expired-instruments/historical-candle/{key}/{v2_iv}/{b}/{a}")
            rows += d["data"]["candles"]
    else:
        today = today_ist()
        hist_end = min(end, today - timedelta(days=1))
        if start <= hist_end:
            for a, b in _chunks(start, hist_end):
                d = await _get(f"/v3/historical-candle/{key}/{unit}/{iv}/{b}/{a}")
                rows += d["data"]["candles"]
        if end >= today and start <= today:
            d = await _get(f"/v3/historical-candle/intraday/{key}/{unit}/{iv}")
            rows += d["data"]["candles"]

    # De-duplicate chunk boundaries, oldest first.
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r[0]):
        if r[0] in seen:
            continue
        seen.add(r[0])
        out.append(r)
    return out


# ─── Orders (live auto-trade) ─────────────────────────────────────────────────
# Upstox serves order placement from its HFT host; like the account calls it
# only works from the static IP registered for this token.
HFT = "https://api-hft.upstox.com"


async def place_market_order(instrument_key: str, qty: int, side: str, tag: str) -> str:
    """Intraday MARKET order (side BUY/SELL); returns the Upstox order id."""
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "Authorization": f"Bearer {_token()}"}
    await _throttle()
    r = await client().post(f"{HFT}/v2/order/place", headers=headers, json={
        "quantity": qty, "product": "I", "validity": "DAY", "price": 0, "tag": tag[:20],
        "instrument_token": instrument_key, "order_type": "MARKET", "transaction_type": side,
        "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False,
    })
    try:
        body = r.json()
    except ValueError:
        raise UpstoxError(f"Upstox {r.status_code}: {r.text[:200]}")
    if r.status_code != 200 or body.get("status") != "success":
        errs = body.get("errors") or [{}]
        raise UpstoxError(f"Upstox {r.status_code}: {errs[0].get('message') or body}")
    return (body.get("data") or {}).get("order_id", "")


def price_at(rows: list[list], hhmm: str) -> tuple[float, str] | None:
    """Premium at a clock time on one day's 1m candles: that minute's OPEN (the
    price at hh:mm:00); if that minute didn't trade, the last close before it;
    failing that, the first open after it."""
    before = None
    for r in rows:
        t = r[0][11:16]
        if t == hhmm:
            return float(r[1]), r[0]
        if t < hhmm:
            before = r
        elif before is None:
            return float(r[1]), r[0]
        else:
            break
    return (float(before[4]), before[0]) if before else None


async def find_strike_by_premium(expiry: str, opt_type: str, day: date, hhmm: str, target: float) -> dict:
    """Strike of `expiry`/`opt_type` whose premium at `day hhmm` is nearest `target`.

    A CE's premium falls as the strike rises and a PE's rises with it, so a
    binary search over the sorted strikes needs ~log2(N) one-day fetches
    (~8 for 230 strikes) instead of all of them. Strikes with no candle that
    day are stepped over."""
    exp_set = set(await expired_expiries())
    expired = expiry in exp_set
    rows = [c for c in await contracts(expiry) if c["instrument_type"] == opt_type]
    if not rows:
        raise UpstoxError(f"No {opt_type} contracts for {expiry}")
    strikes = [c["strike_price"] for c in rows]
    by_strike = {c["strike_price"]: c for c in rows}
    checked: dict[float, tuple[float, str] | None] = {}

    async def premium(i: int):
        k = strikes[i]
        if k not in checked:
            cs = await candles(by_strike[k]["instrument_key"], "1m", day, day, expired)
            checked[k] = price_at(cs, hhmm)
        return checked[k]

    async def nearest_priced(i: int, lo: int, hi: int):
        """Closest strike index to i inside (lo, hi) that traded that day."""
        for d in range(0, hi - lo):
            for k in (i + d, i - d):
                if lo < k < hi:
                    p = await premium(k)
                    if p:
                        return k, p
        return None

    # Premium vs strike index is decreasing for CE, increasing for PE; keep
    # the target between lo and hi and halve the gap each step.
    lo, hi = 0, len(strikes) - 1
    while hi - lo > 2:
        hit = await nearest_priced((lo + hi) // 2, lo, hi)
        if not hit:
            break
        j, (p, _) = hit
        too_rich = p > target
        if (opt_type == "CE") == too_rich:   # CE too rich / PE too cheap → higher strike
            lo = j
        else:
            hi = j
    cands = []
    for i in range(max(0, lo - 1), min(len(strikes), hi + 2)):
        p = await premium(i)
        if p:
            cands.append({"strike": strikes[i], "premium": p[0], "at": p[1], "diff": round(abs(p[0] - target), 2)})
    if not cands:
        raise UpstoxError(f"No {opt_type} for {expiry} traded on {day} around {hhmm}")
    cands.sort(key=lambda c: c["diff"])
    best = cands[0]
    return {"expiry": expiry, "type": opt_type, "date": day.isoformat(), "time": hhmm, "target": target,
            "strike": best["strike"], "premium": best["premium"], "at": best["at"],
            "symbol": by_strike[best["strike"]]["trading_symbol"],
            "nearby": sorted(cands, key=lambda c: c["strike"]), "lookups": len(checked)}


# ─── Account (needs the request to come from the account's static IP) ─────────
# Upstox refuses every user/portfolio/order endpoint for this token unless the
# call comes from the static IP registered in the developer console, so each
# section is fetched on its own and fails on its own (the page shows why).
ACCOUNT_ENDPOINTS = {
    "profile":   "/v2/user/profile",
    "funds":     "/v2/user/get-funds-and-margin",
    "holdings":  "/v2/portfolio/long-term-holdings",
    "positions": "/v2/portfolio/short-term-positions",
    "orders":    "/v2/order/retrieve-all",
    "trades":    "/v2/order/trades/get-trades-for-day",
}


async def account() -> dict:
    async def one(name: str, path: str):
        try:
            return name, {"data": (await _get(path)).get("data"), "error": None}
        except UpstoxError as e:
            return name, {"data": None, "error": str(e)}
    pairs = await asyncio.gather(*(one(n, p) for n, p in ACCOUNT_ENDPOINTS.items()))
    return dict(pairs)
