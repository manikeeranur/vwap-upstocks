import asyncio
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from .. import brokers, upstox, vwap
from .accounts import _creds
from .common import _parse_day, candles_csv, csv_name, templates

router = APIRouter()

async def _resolve(expiry: str, strike: float, opt_type: str) -> tuple[dict, bool]:
    exp_set = set(await upstox.expired_expiries())
    for c in await upstox.contracts(expiry):
        if c["strike_price"] == strike and c["instrument_type"] == opt_type:
            return c, expiry in exp_set
    raise HTTPException(404, f"No NIFTY {strike:g} {opt_type} for expiry {expiry}")


def _in_market_hours(ts: str) -> bool:
    hm = ts[11:16]
    return "09:15" <= hm <= "15:29"


async def _load(expiry: str, strike: float, opt_type: str, start: str, end: str | None, interval: str, market_hours: bool):
    opt_type = opt_type.upper()
    if opt_type not in ("CE", "PE"):
        raise HTTPException(400, "type must be CE or PE")
    exp_day = _parse_day(expiry, "expiry")
    a = _parse_day(start, "from")
    b = min(_parse_day(end, "to") if end else exp_day, exp_day, upstox.today_ist())
    if a > b:
        raise HTTPException(400, f"from ({a}) is after to ({b})")
    contract, expired = await _resolve(expiry, strike, opt_type)
    try:
        rows = await upstox.candles(contract["instrument_key"], interval, a, b, expired)
    except upstox.UpstoxError as e:
        raise HTTPException(502, str(e))
    if market_hours and interval != "1d":
        rows = [r for r in rows if _in_market_hours(r[0])]
    return contract, expired, a, b, rows


@router.get("/data", response_class=HTMLResponse)
async def index(request: Request):
    error = None
    try:
        expiries = await upstox.expiries()
    except upstox.UpstoxError as e:
        expiries, error = [], str(e)
    return templates.TemplateResponse(request, "index.html", {
        "expiries": expiries, "error": error, "intervals": list(upstox.INTERVALS),
        "today": upstox.today_ist().isoformat(),
    })


@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, broker: str = "upstox"):
    """Profile, funds, holdings, positions, orders and trades — server-rendered, for Upstox or Kite.
    Each section loads independently; a static-IP refusal is shown per section."""
    broker = broker if broker in brokers.BROKERS else "upstox"
    now = datetime.now(upstox.IST).strftime("%d %b %Y %H:%M")
    if broker in ("kite", "indmoney"):
        creds = await asyncio.to_thread(_creds, request, request.state.user_id, broker)
        if not creds.token_valid():
            return templates.TemplateResponse(request, "account.html", {"broker": broker, "a": None, "blocked": False,
                                                                        "login_needed": True, "now": now})
        acct = await (brokers.kite_account(creds) if broker == "kite" else brokers.ind_account(creds))
        blocked = all(v["error"] for v in acct.values())
    else:
        acct = await upstox.account()
        blocked = all(v["error"] and "static IP" in v["error"] for v in acct.values())
    return templates.TemplateResponse(request, "account.html", {"broker": broker, "a": acct, "blocked": blocked,
                                                                "login_needed": False, "now": now})


@router.get("/api/account")
async def api_account():
    return await upstox.account()


@router.get("/api/contracts")
async def api_contracts(expiry: str):
    rows = await upstox.contracts(expiry)
    strikes = sorted({r["strike_price"] for r in rows})
    return {"expiry": expiry, "strikes": strikes, "lotSize": rows[0]["lot_size"] if rows else None}


@router.get("/api/find-strike")
async def api_find_strike(expiry: str, premium: float, type: str = Query(..., alias="type"),
                          day: str = Query(..., alias="date"), time: str = "09:15"):
    """Strike whose premium at `date time` was nearest the typed `premium`."""
    opt_type = type.upper()
    if opt_type not in ("CE", "PE"):
        raise HTTPException(400, "type must be CE or PE")
    if premium <= 0:
        raise HTTPException(400, "premium must be > 0")
    try:
        hh, mm = (int(x) for x in time.split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except (ValueError, AssertionError):
        raise HTTPException(400, "time must be HH:MM")
    d = _parse_day(day, "date")
    if d > _parse_day(expiry, "expiry"):
        raise HTTPException(400, f"{d} is after expiry {expiry}")
    return await upstox.find_strike_by_premium(expiry, opt_type, d, f"{hh:02d}:{mm:02d}", premium)


@router.get("/api/preview")
async def api_preview(expiry: str, strike: float, type: str = Query(..., alias="type"),
                      start: str = Query(..., alias="from"), end: str | None = Query(None, alias="to"),
                      interval: str = "1m", market_hours: bool = True):
    contract, expired, a, b, rows = await _load(expiry, strike, type, start, end, interval, market_hours)
    return {
        "symbol": contract["trading_symbol"], "instrumentKey": contract["instrument_key"],
        "expired": expired, "from": a.isoformat(), "to": b.isoformat(),
        "count": len(rows), "head": rows[:10], "tail": rows[-5:] if len(rows) > 10 else [],
    }


@router.get("/download")
async def download(expiry: str, strike: float, type: str = Query(..., alias="type"),
                   start: str = Query(..., alias="from"), end: str | None = Query(None, alias="to"),
                   interval: str = "1m", market_hours: bool = True):
    contract, _, a, b, rows = await _load(expiry, strike, type, start, end, interval, market_hours)
    name = csv_name(expiry, strike, type, interval, a, b)
    return StreamingResponse(iter([candles_csv(rows)]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/api/chart")
async def chart_data(key: str, day: str = Query(..., alias="date"), expiry: str = Query(...)):
    """One option's day for the trade chart: 1-min bars (market hours), the
    5-min bars the strategy uses, and its 5-min VWAP (same maths as the engine).
    Times are IST "HH:MM"; a 5-min bar is stamped with its first minute.
    `prev_close1` / `prev_close5`: the previous session's closes (same cached
    span, no extra call) so the chart's EMA is warmed up from 9:15."""
    d = _parse_day(day, "date")
    expired = expiry in set(await upstox.expired_expiries())
    rows = await vwap.span_rows(key, d, expiry, expired)
    bars = vwap._market([r for r in rows if r[0][:10] == day])
    prev_day = max((r[0][:10] for r in rows if r[0][:10] < day), default=None)
    prev = vwap._market([r for r in rows if r[0][:10] == prev_day]) if prev_day else []
    nm = vwap.aggregate(bars, 5)
    vw = vwap.vwap_series(nm)
    return {
        "date": day,
        "bars1": [list(b[:6]) for b in bars],
        "bars5": [[vwap._add(b[0], -4), *b[1:6]] for b in nm],
        "prev_close1": [b[4] for b in prev],
        "prev_close5": [b[4] for b in vwap.aggregate(prev, 5)],
        # VWAP of a 5-min candle is known when it closes → stamp at its last minute
        "vwap5": [[b[0], v] for b, v in zip(nm, vw)],
    }
