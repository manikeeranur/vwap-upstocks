"""Broker connectors for live orders: Upstox and Zerodha Kite.

Market data and strategy scans always use the app's Upstox data token (upstox.py).
Orders use each person's own daily broker token, issued by that broker's login:

Upstox  login  https://api.upstox.com/v2/login/authorization/dialog → /broker/upstox/callback
        or the "access token request" pushed to the Upstox app → /broker/upstox/notify
        token  valid until 03:30 IST the next day
Kite    login  https://kite.zerodha.com/connect/login → /broker/kite/callback
        token  valid until about 06:00 IST the next day
INDmoney (INDstocks) has no login redirect: the person pastes the access token generated on
        indstocks.com, or types their MPIN and authenticator code here (POST /generate/token)
        token  valid 24 hours from generation
"""
from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import io
import time
from urllib.parse import urlencode

import httpx

from . import upstox

BROKERS = ("upstox", "kite", "indmoney")
LABELS = {"upstox": "Upstox", "kite": "Kite", "indmoney": "INDmoney"}
UPSTOX_API = "https://api.upstox.com"
KITE_API = "https://api.kite.trade"
KITE_LOGIN = "https://kite.zerodha.com/connect/login"
IND_API = "https://api.indstocks.com"
# A token is usable until this IST time on the day after it was issued (INDmoney: 24 hours).
EXPIRY = {"upstox": (3, 30), "kite": (6, 0)}
IND_TOKEN_HOURS = 24


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
        if self.broker == "indmoney":
            return issued + timedelta(hours=IND_TOKEN_HOURS)
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
    if creds.broker == "indmoney":
        inst = await ind_instrument(creds, trade)
        q = qty or qty_lots * (inst["lot_size"] or lot_size)
        oid = await ind_place(creds, inst["security_id"], q, side, tag)
        return oid, q, inst["trading_symbol"]
    inst = await kite_instrument(creds, trade)
    q = qty or qty_lots * (inst["lot_size"] or lot_size)
    oid = await kite_place(creds, inst["tradingsymbol"], q, side, tag)
    return oid, q, inst["tradingsymbol"]


async def kite_account(creds: Credentials) -> dict:
    """Kite profile, funds, positions, holdings, orders and trades in the shape the account page uses."""
    headers = _kite_headers(creds.api_key, creds.access_token)

    async def one(c, name, path):
        try:
            r = await c.get(f"{KITE_API}{path}", headers=headers)
            body = await _json(r, "Kite")
            if r.status_code != 200 or body.get("status") != "success":
                raise BrokerError(f"Kite: {body.get('message') or r.status_code}")
            return name, {"data": body.get("data"), "error": None}
        except (BrokerError, httpx.HTTPError) as exc:
            return name, {"data": None, "error": str(exc) or "Kite request failed"}

    async with httpx.AsyncClient(timeout=20) as c:
        out = dict(await asyncio.gather(*(one(c, n, p) for n, p in (
            ("profile", "/user/profile"), ("funds", "/user/margins"), ("positions", "/portfolio/positions"),
            ("holdings", "/portfolio/holdings"), ("orders", "/orders"), ("trades", "/trades"),
            ("mf_holdings", "/mf/holdings"), ("sips", "/mf/sips")))))
    try:
        out["charges"] = {"data": await kite_charges(creds, out["orders"]["data"]) if out["orders"]["data"] is not None else None,
                          "error": out["orders"]["error"]}
    except (BrokerError, httpx.HTTPError, KeyError) as exc:
        out["charges"] = {"data": None, "error": str(exc) or "Kite charges unavailable"}
    if out["profile"]["data"]:
        p = out["profile"]["data"]
        out["profile"]["data"] = {**p, "broker": "ZERODHA", "is_active": True}
    if out["funds"]["data"]:
        def seg(x):
            if not x or not x.get("enabled", True):
                return None
            a, u = x.get("available") or {}, x.get("utilised") or {}
            # Kite's own figures; `cash` is usually 0 — the wallet balance is `live_balance`.
            return {"fields": [("Available margin", x.get("net"), "up"), ("Cash balance", a.get("live_balance"), ""),
                               ("Opening balance", a.get("opening_balance"), ""), ("Collateral (pledged)", a.get("collateral"), ""),
                               ("Used margin", u.get("debits"), ""), ("Pay-in today", a.get("intraday_payin"), "")]}
        out["funds"]["data"] = {k: seg(out["funds"]["data"].get(k)) for k in ("equity", "commodity")}
    if out["positions"]["data"] is not None:
        out["positions"]["data"] = (out["positions"]["data"] or {}).get("net") or []
    return out


def charge_row(order: dict, *, brokerage=0, stt=0, exchange=0, sebi=0, stamp=0, gst=0, total=0) -> dict:
    """One executed order's charges, the same for every broker."""
    n = lambda v: float(v or 0)
    return {"time": str(order.get("order_timestamp") or order.get("exchange_timestamp") or "")[11:19],
            "symbol": order.get("tradingsymbol") or order.get("trading_symbol") or "",
            "side": order.get("transaction_type") or "", "qty": order.get("filled_quantity") or order.get("quantity") or 0,
            "price": n(order.get("average_price")), "brokerage": n(brokerage), "stt": n(stt), "exchange": n(exchange),
            "sebi": n(sebi), "stamp": n(stamp), "gst": n(gst), "total": n(total)}


async def kite_charges(creds: Credentials, orders: list) -> list:
    """Charges for today's executed Kite orders (Kite's virtual contract note)."""
    done = [o for o in orders or [] if o.get("status") == "COMPLETE" and (o.get("filled_quantity") or 0) > 0]
    if not done:
        return []
    params = [{"order_id": o["order_id"], "exchange": o["exchange"], "tradingsymbol": o["tradingsymbol"],
               "transaction_type": o["transaction_type"], "variety": o.get("variety") or "regular",
               "product": o["product"], "order_type": o["order_type"], "quantity": o["filled_quantity"],
               "average_price": o["average_price"]} for o in done]
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{KITE_API}/charges/orders", headers=_kite_headers(creds.api_key, creds.access_token), json=params)
    body = await _json(r, "Kite")
    if r.status_code != 200 or body.get("status") != "success":
        raise BrokerError(f"Kite charges: {body.get('message') or r.status_code}")
    rows = []
    for o, note in zip(done, body.get("data") or []):
        ch = note.get("charges") or {}
        rows.append(charge_row(o, brokerage=ch.get("brokerage"), stt=ch.get("transaction_tax"),
                               exchange=ch.get("exchange_turnover_charge"), sebi=ch.get("sebi_turnover_charge"),
                               stamp=ch.get("stamp_duty"), gst=(ch.get("gst") or {}).get("total"), total=ch.get("total")))
    return rows


# ── INDmoney (INDstocks) ────────────────────────────────────────────────────
def _ind_headers(token: str) -> dict:
    return {"Authorization": token, "Accept": "application/json"}


async def _ind(method: str, path: str, token: str = "", *, headers: dict | None = None, **kw) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, f"{IND_API}{path}", headers=headers or _ind_headers(token), **kw)
    body = await _json(r, "INDmoney")
    if r.status_code != 200 or body.get("status") != "success":
        raise BrokerError(f"INDmoney {r.status_code}: {body.get('message') or body.get('error_type') or body}")
    return body.get("data")


def _ind_profile(d: dict) -> dict:
    name = " ".join(x for x in ((d or {}).get("first_name"), (d or {}).get("last_name")) if x)
    return _profile((d or {}).get("user_id"), name, (d or {}).get("email"))


async def ind_profile(token: str) -> dict:
    """Checks a pasted token and returns whose it is."""
    return _ind_profile(await _ind("GET", "/user/profile", token))


async def ind_generate_token(client_id: str, mpin: str, totp: str) -> str:
    """Fresh 24-hour token from the person's MPIN and current authenticator code (neither is stored).
    Each call invalidates the previous token."""
    data = await _ind("POST", "/generate/token", headers={"x-api-key": client_id, "Content-Type": "application/json"},
                      json={"mpin": mpin, "totp": totp})
    token = (data or {}).get("token") or (data or {}).get("access_token")
    if not token:
        raise BrokerError("INDmoney did not return a token")
    return token


_ind_contracts: dict[tuple, tuple[float, dict]] = {}


async def ind_instrument(creds: Credentials, trade: dict) -> dict:
    """The INDstocks NIFTY option with the same expiry, strike and CE/PE as the strategy's contract."""
    key = (str(trade["expiry"])[:10], float(trade["strike"]), trade["type"])
    hit = _ind_contracts.get(key)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    data = await _ind("GET", "/market/instruments/search", creds.access_token, params={
        "underlying": "NIFTY", "segment": "DERIVATIVE", "instrument_type": "OPTIDX", "expiry": key[0],
        "strike_from": key[1], "strike_to": key[1], "option_type": key[2]})
    rows = [r for r in (data or {}).get("instruments") or []
            if r.get("option_type") == key[2] and r.get("strike_price") is not None and float(r["strike_price"]) == key[1]]
    if not rows:
        raise BrokerError(f"No INDmoney NIFTY contract for {key[0]} {key[1]:g} {key[2]}")
    found = {"security_id": str(rows[0]["security_id"]), "trading_symbol": rows[0].get("trading_symbol") or "",
             "lot_size": int(rows[0].get("lot_size") or 0)}
    _ind_contracts[key] = (time.time(), found)
    return found


async def ind_place(creds: Credentials, security_id: str, qty: int, side: str, tag: str) -> str:
    """Intraday MARKET order on NSE derivatives (INDstocks turns it into a protected LIMIT order)."""
    data = await _ind("POST", "/order", creds.access_token, json={
        "txn_type": side, "exchange": "NSE", "segment": "DERIVATIVE", "product": "INTRADAY", "order_type": "MARKET",
        "validity": "DAY", "security_id": security_id, "qty": qty, "is_amo": False, "remarks": tag[:100]})
    return (data or {}).get("order_id", "")


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def ind_account(creds: Credentials) -> dict:
    """INDmoney profile, funds, positions, holdings, orders, trades and today's charges in the account page's shape."""
    async def one(name, path, params=None):
        try:
            return name, {"data": await _ind("GET", path, creds.access_token, params=params), "error": None}
        except (BrokerError, httpx.HTTPError) as exc:
            return name, {"data": None, "error": str(exc) or "INDmoney request failed"}

    out = dict(await asyncio.gather(
        one("profile", "/user/profile"), one("funds", "/funds"), one("positions", "/portfolio/positions"),
        one("holdings", "/portfolio/holdings"), one("orders", "/order-book"),
        one("trades", "/trade-book", {"segment": "DERIVATIVE"})))
    funds = out["funds"]["data"] or {}
    if out["profile"]["data"]:
        d = out["profile"]["data"]
        exch = [n for n, f in (("NSE", "is_nse_onboarded"), ("BSE", "is_bse_onboarded"),
                               ("NFO", "is_nse_fno_onboarded"), ("BFO", "is_bse_fno_onboarded")) if d.get(f)]
        out["profile"]["data"] = {"user_name": _ind_profile(d)["name"], "user_id": d.get("user_id"), "email": d.get("email"),
                                  "broker": "INDMONEY", "user_type": d.get("ucc"), "is_active": True,
                                  "exchanges": exch, "products": ["CNC", "INTRADAY", "MARGIN"]}
    if out["funds"]["data"]:
        avl = funds.get("detailed_avl_balance") or {}
        out["funds"]["data"] = {"equity": {"available_margin": avl.get("option_buy"), "used_margin": None,
                                           "payin_amount": funds.get("funds_added"), "span_margin": None,
                                           "exposure_margin": None, "notional_cash": funds.get("sod_balance")}}
    if out["positions"]["data"] is not None:
        out["positions"]["data"] = [{
            "tradingsymbol": " ".join(str(x) for x in (p.get("symbol"), p.get("drv_strike_price") or "", p.get("drv_option_type") or "") if x != ""),
            "exchange": p.get("exchange") or "", "product": p.get("product"), "quantity": p.get("net_qty"),
            "average_price": p.get("avg_price"), "last_price": None,
            "buy_value": (p.get("buy_qty") or 0) * (p.get("buy_avg") or 0), "sell_value": (p.get("sell_qty") or 0) * (p.get("sell_avg") or 0),
            "realised": p.get("realized_profit"), "unrealised": None, "pnl": p.get("realized_profit")}
            for p in out["positions"]["data"] or []]
    if out["holdings"]["data"] is not None:
        out["holdings"]["data"] = [{"tradingsymbol": h.get("symbol"), "quantity": h.get("total_qty"), "average_price": h.get("avg_price"),
                                    "last_price": None, "pnl": None, "day_change": None} for h in out["holdings"]["data"] or []]
    names = {}
    if out["orders"]["data"] is not None:
        rows = []
        for o in out["orders"]["data"] or []:
            names[o.get("exch_order_id")] = (o.get("name"), o.get("txn_type"), o.get("product"))
            rows.append({"order_timestamp": (o.get("created_at") or "").replace("T", " "), "tradingsymbol": o.get("name"),
                         "transaction_type": o.get("txn_type"), "order_type": o.get("order_type"), "product": o.get("product"),
                         "quantity": o.get("requested_qty"), "filled_quantity": o.get("traded_qty"),
                         "price": _num(o.get("requested_price")), "trigger_price": _num(o.get("sl_trigger_price")),
                         "average_price": _num(o.get("traded_price")), "status": o.get("status"), "status_message": o.get("extra_info")})
        out["orders"]["data"] = rows
    if out["trades"]["data"] is not None:
        out["trades"]["data"] = [{"exchange_timestamp": (t.get("trade_date") or "").replace("T", " "),
                                  "tradingsymbol": (names.get(t.get("exch_order_id")) or (t.get("scrip_code"),))[0],
                                  "transaction_type": (names.get(t.get("exch_order_id")) or (None, ""))[1],
                                  "quantity": t.get("quantity"), "average_price": t.get("price"),
                                  "product": (names.get(t.get("exch_order_id")) or (None, None, ""))[2],
                                  "order_id": t.get("exch_order_id"), "trade_id": t.get("fill_id")} for t in out["trades"]["data"] or []]
    # INDstocks reports only the day's totals, not a per-order breakdown.
    if out["funds"]["data"]:
        total = sum(_num(funds.get(k)) or 0 for k in ("brokerage", "eq_charges", "fno_charges"))
        done = sum(1 for o in out["orders"]["data"] or [] if (o.get("filled_quantity") or 0) > 0)
        row = {"time": "", "symbol": "All orders today", "side": "", "qty": "", "price": None, "brokerage": _num(funds.get("brokerage")),
               "stt": None, "exchange": None, "sebi": None, "stamp": None, "gst": None, "total": total}
        out["charges"] = {"data": [row] if done or total else [], "error": None, "orders": done,
                          "note": "INDmoney gives only the day's total: brokerage plus F&O and equity charges."}
    else:
        out["charges"] = {"data": None, "error": out["funds"]["error"]}
    return out


async def order_fills(creds: Credentials) -> dict:
    """{order id: average fill price} for today's filled orders at this broker."""
    if creds.broker == "kite":
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{KITE_API}/orders", headers=_kite_headers(creds.api_key, creds.access_token))
        body = await _json(r, "Kite")
        if r.status_code != 200 or body.get("status") != "success":
            raise BrokerError(f"Kite orders: {body.get('message') or r.status_code}")
        rows = [(o.get("order_id"), o.get("average_price"), o.get("filled_quantity")) for o in body.get("data") or []]
    elif creds.broker == "upstox":
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{UPSTOX_API}/v2/order/retrieve-all",
                            headers={"Accept": "application/json", "Authorization": f"Bearer {creds.access_token}"})
        body = await _json(r, "Upstox")
        if r.status_code != 200:
            raise BrokerError(f"Upstox orders: {r.status_code}")
        rows = [(o.get("order_id"), o.get("average_price"), o.get("filled_quantity")) for o in body.get("data") or []]
    else:
        rows = [(o.get("id"), _num(o.get("traded_price")), o.get("traded_qty"))
                for o in await _ind("GET", "/order-book", creds.access_token) or []]
    return {str(oid): float(price) for oid, price, filled in rows if oid and price and (filled or 0) > 0}

