"""Broker connectors for live orders: Upstox and Zerodha Kite.

Market data and strategy scans always use the app's Upstox data token (upstox.py).
Orders use each person's own daily broker token, issued by that broker's login:

Upstox  login  https://api.upstox.com/v2/login/authorization/dialog → /broker/upstox/callback
        or the "access token request" pushed to the Upstox app → /broker/upstox/notify
        token  valid until 03:30 IST the next day
Kite    login  https://kite.zerodha.com/connect/login → /broker/kite/callback
        token  valid until about 06:00 IST the next day
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import io
import time
from urllib.parse import urlencode

import httpx

from . import upstox

BROKERS = ("upstox", "kite")
LABELS = {"upstox": "Upstox", "kite": "Kite"}
UPSTOX_API = "https://api.upstox.com"
KITE_API = "https://api.kite.trade"
KITE_LOGIN = "https://kite.zerodha.com/connect/login"
# A token is usable until this IST time on the day after it was issued.
EXPIRY = {"upstox": (3, 30), "kite": (6, 0)}


class BrokerError(RuntimeError):
    pass


@dataclass
class Credentials:
    broker: str
    api_key: str
    api_secret: str
    access_token: str
    token_issued: str | None   # ISO datetime (IST)

    def valid_until(self) -> datetime | None:
        if not (self.access_token and self.token_issued):
            return None
        issued = datetime.fromisoformat(self.token_issued)
        h, m = EXPIRY[self.broker]
        cutoff = issued.replace(hour=h, minute=m, second=0, microsecond=0)
        return cutoff if issued < cutoff else cutoff + timedelta(days=1)

    def token_valid(self, now: datetime | None = None) -> bool:
        until = self.valid_until()
        return bool(until and (now or datetime.now(upstox.IST)) < until)


def now_iso() -> str:
    return datetime.now(upstox.IST).isoformat(timespec="seconds")


async def _json(r: httpx.Response, name: str) -> dict:
    try:
        body = r.json()
    except ValueError:
        raise BrokerError(f"{name} {r.status_code}: {r.text[:200]}")
    return body


# ── Upstox ──────────────────────────────────────────────────────────────────
def upstox_login_url(api_key: str, redirect_uri: str, state: str) -> str:
    return f"{UPSTOX_API}/v2/login/authorization/dialog?" + urlencode(
        {"response_type": "code", "client_id": api_key, "redirect_uri": redirect_uri, "state": state})


def _profile(user_id="", name="", email="", avatar="") -> dict:
    return {"user_id": user_id or "", "name": name or "", "email": email or "", "avatar": avatar or ""}


async def upstox_exchange_code(api_key: str, api_secret: str, code: str, redirect_uri: str) -> tuple[str, dict]:
    """Authorization code → (access_token, profile)."""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{UPSTOX_API}/v2/login/authorization/token", headers={"Accept": "application/json"},
                         data={"code": code, "client_id": api_key, "client_secret": api_secret,
                               "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
    body = await _json(r, "Upstox")
    if r.status_code != 200 or not body.get("access_token"):
        errs = body.get("errors") or [{}]
        raise BrokerError(f"Upstox login failed: {errs[0].get('message') or body}")
    return body["access_token"], _profile(body.get("user_id"), body.get("user_name"), body.get("email"))


async def upstox_request_token(api_key: str, api_secret: str) -> dict:
    """Ask Upstox to push an approval to the user's Upstox app; the token arrives at the app's notifier URL."""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{UPSTOX_API}/v3/login/auth/token/request/{api_key}",
                         headers={"Accept": "application/json", "Content-Type": "application/json"},
                         json={"client_secret": api_secret})
    body = await _json(r, "Upstox")
    if r.status_code != 200 or body.get("status") != "success":
        errs = body.get("errors") or [{}]
        raise BrokerError(f"Upstox token request failed: {errs[0].get('message') or body}")
    return body.get("data") or {}


async def upstox_profile(token: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{UPSTOX_API}/v2/user/profile", headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
    body = await _json(r, "Upstox")
    if r.status_code != 200:
        raise BrokerError("Upstox token was not accepted")
    d = body.get("data") or {}
    return _profile(d.get("user_id"), d.get("user_name"), d.get("email"))


# ── Kite ────────────────────────────────────────────────────────────────────
def kite_login_url(api_key: str, state: str) -> str:
    return f"{KITE_LOGIN}?" + urlencode({"v": 3, "api_key": api_key, "redirect_params": urlencode({"state": state})})


def _kite_headers(api_key: str, token: str = "") -> dict:
    h = {"X-Kite-Version": "3"}
    if token:
        h["Authorization"] = f"token {api_key}:{token}"
    return h


async def kite_exchange_token(api_key: str, api_secret: str, request_token: str) -> tuple[str, dict]:
    """Request token → (access_token, profile)."""
    checksum = hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{KITE_API}/session/token", headers=_kite_headers(api_key),
                         data={"api_key": api_key, "request_token": request_token, "checksum": checksum})
    body = await _json(r, "Kite")
    if r.status_code != 200 or body.get("status") != "success":
        raise BrokerError(f"Kite login failed: {body.get('message') or body}")
    d = body["data"]
    return d["access_token"], _profile(d.get("user_id"), d.get("user_name"), d.get("email"), d.get("avatar_url"))


_kite_instruments: dict[str, tuple[float, dict]] = {}


async def kite_nifty_options(api_key: str, token: str) -> dict:
    """{(expiry 'YYYY-MM-DD', strike, 'CE'|'PE'): {'tradingsymbol', 'lot_size'}} for NIFTY options, cached 6 hours."""
    hit = _kite_instruments.get(api_key)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(f"{KITE_API}/instruments/NFO", headers=_kite_headers(api_key, token))
    if r.status_code != 200:
        raise BrokerError(f"Kite instruments {r.status_code}: {r.text[:200]}")
    out = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        if row.get("name") == "NIFTY" and row.get("instrument_type") in ("CE", "PE") and row.get("segment") == "NFO-OPT":
            out[(row["expiry"], float(row["strike"]), row["instrument_type"])] = {
                "tradingsymbol": row["tradingsymbol"], "lot_size": int(float(row["lot_size"] or 0))}
    if not out:
        raise BrokerError("Kite instruments list had no NIFTY options")
    _kite_instruments[api_key] = (time.time(), out)
    return out


async def kite_instrument(creds: Credentials, trade: dict) -> dict:
    """The Kite contract for the same NIFTY expiry, strike and CE/PE as the strategy's Upstox contract."""
    key = (str(trade["expiry"])[:10], float(trade["strike"]), trade["type"])
    found = (await kite_nifty_options(creds.api_key, creds.access_token)).get(key)
    if not found:
        raise BrokerError(f"No Kite NIFTY contract for {key[0]} {key[1]:g} {key[2]}")
    return found


async def kite_place(creds: Credentials, tradingsymbol: str, qty: int, side: str, tag: str) -> str:
    """Intraday (MIS) MARKET order on NFO. Exchanges require market protection on API market orders: -1 = automatic."""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{KITE_API}/orders/regular", headers=_kite_headers(creds.api_key, creds.access_token), data={
            "tradingsymbol": tradingsymbol, "exchange": "NFO", "transaction_type": side, "order_type": "MARKET",
            "quantity": qty, "product": "MIS", "validity": "DAY", "market_protection": -1,
            "tag": "".join(ch for ch in tag if ch.isalnum())[:20]})
    body = await _json(r, "Kite")
    if r.status_code != 200 or body.get("status") != "success":
        raise BrokerError(f"Kite {r.status_code}: {body.get('message') or body}")
    return (body.get("data") or {}).get("order_id", "")


# ── Dispatch ────────────────────────────────────────────────────────────────
async def place_order(creds: Credentials, trade: dict, qty_lots: int, lot_size: int, side: str, tag: str,
                      qty: int | None = None) -> tuple[str, int, str]:
    """Send one MARKET order with this person's token. Returns (order_id, quantity, broker symbol).
    `qty` (the original buy quantity) overrides lots for exits."""
    if creds.broker == "upstox":
        q = qty or qty_lots * lot_size
        oid = await upstox.place_market_order(trade["instrument_key"], q, side, tag, token=creds.access_token)
        return oid, q, trade.get("symbol", "")
    inst = await kite_instrument(creds, trade)
    q = qty or qty_lots * (inst["lot_size"] or lot_size)
    oid = await kite_place(creds, inst["tradingsymbol"], q, side, tag)
    return oid, q, inst["tradingsymbol"]
