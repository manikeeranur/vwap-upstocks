"""upstocks — download NIFTY 50 option premium candles (live or expired) as CSV.

Pick an expiry, a strike and CE/PE, a start date and an interval; the range
always ends on (or before) the contract's expiry. The page is server-rendered
(expiry list included); strikes for the chosen expiry load from /api/contracts.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import asyncio
import uuid
import zipfile

from . import pdf_report, smc, upstox, vwap

app = FastAPI(title="upstocks")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _parse_day(s: str, name: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, f"{name} must be YYYY-MM-DD")


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


def candles_csv(rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "time", "open", "high", "low", "close", "volume", "oi"])
    for ts, o, h, l, c, v, oi in rows:
        w.writerow([ts[:10], ts[11:16], o, h, l, c, v, oi])
    return buf.getvalue()


def csv_name(expiry: str, strike: float, opt_type: str, interval: str, a, b) -> str:
    """Same pattern the Backtest page reads the expiry/strike/type back from."""
    exp_tag = datetime.strptime(expiry, "%Y-%m-%d").strftime("%d%b%y").upper()
    return f"NIFTY_{exp_tag}_{strike:g}_{opt_type.upper()}_{interval}_{a}_to_{b}.csv"


@app.exception_handler(upstox.UpstoxError)
async def _upstox_error(_: Request, e: upstox.UpstoxError):
    return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/", response_class=HTMLResponse)
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


@app.get("/backtest", response_class=HTMLResponse)
async def backtest(request: Request):
    """SL/target check over uploaded CSVs — parsing and maths run in the browser."""
    return templates.TemplateResponse(request, "backtest.html", {})


@app.get("/api/contracts")
async def api_contracts(expiry: str):
    rows = await upstox.contracts(expiry)
    strikes = sorted({r["strike_price"] for r in rows})
    return {"expiry": expiry, "strikes": strikes, "lotSize": rows[0]["lot_size"] if rows else None}


@app.get("/api/find-strike")
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


@app.get("/api/preview")
async def api_preview(expiry: str, strike: float, type: str = Query(..., alias="type"),
                      start: str = Query(..., alias="from"), end: str | None = Query(None, alias="to"),
                      interval: str = "1m", market_hours: bool = True):
    contract, expired, a, b, rows = await _load(expiry, strike, type, start, end, interval, market_hours)
    return {
        "symbol": contract["trading_symbol"], "instrumentKey": contract["instrument_key"],
        "expired": expired, "from": a.isoformat(), "to": b.isoformat(),
        "count": len(rows), "head": rows[:10], "tail": rows[-5:] if len(rows) > 10 else [],
    }


@app.get("/download")
async def download(expiry: str, strike: float, type: str = Query(..., alias="type"),
                   start: str = Query(..., alias="from"), end: str | None = Query(None, alias="to"),
                   interval: str = "1m", market_hours: bool = True):
    contract, _, a, b, rows = await _load(expiry, strike, type, start, end, interval, market_hours)
    name = csv_name(expiry, strike, type, interval, a, b)
    return StreamingResponse(iter([candles_csv(rows)]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


def main() -> None:
    # Local only: the token gives full read access to your Upstox account data.
    uvicorn.run("upstocks.app:app", host="127.0.0.1", port=3400, reload=False)


# ─── VWAP 9:30 strategy runs ──────────────────────────────────────────────────
# A run over many days takes minutes (Upstox rate limit), so it's a background
# job: POST starts it, the page polls its progress, and the ZIP is built from
# the cached candles once it's done. Jobs live in memory (lost on restart).
JOBS: dict[str, dict] = {}


def _settings_from(body: dict) -> vwap.Settings:
    from dataclasses import replace
    preset = body.get("preset") or "frontend_live"
    if preset not in vwap.PRESETS:
        raise HTTPException(400, f"unknown preset {preset!r}")
    try:
        st = replace(vwap.Settings(start=date.fromisoformat(body["start"]), end=date.fromisoformat(body["end"])),
                     **vwap.PRESETS[preset])
    except (KeyError, ValueError):
        raise HTTPException(400, "start and end (YYYY-MM-DD) are required")
    for f in ("min_premium", "max_premium", "sl_pct", "target_pct", "target1_pct", "breakeven_pct", "stagnant_hours", "stagnant_points"):
        if body.get(f) not in (None, ""):
            setattr(st, f, float(body[f]))
    for f in ("candle_minutes", "max_trades", "cooldown_min", "chain_strikes"):
        if body.get(f) not in (None, ""):
            setattr(st, f, int(body[f]))
    for f in ("entry_start", "time_exit", "last_entry"):
        if body.get(f):
            setattr(st, f, str(body[f])[:5])
    for f in ("strong_green", "live", "atm_offsets_only", "align_checkpoints", "include_entry_candle", "time_exit_first"):
        if f in body:
            setattr(st, f, bool(body[f]))
    if st.start > st.end:
        raise HTTPException(400, "start is after end")
    if (st.end - st.start).days > 400:
        raise HTTPException(400, "range too long (max ~13 months)")
    return st


async def _run_job(job: dict) -> None:
    st: vwap.Settings = job["settings"]
    try:
        last = min(st.end, upstox.today_ist())
        spot = await vwap.trading_days_and_spot(st.start, last)
        job["total"] = len(spot)
        # Weekdays with no NIFTY candles: exchange holidays, or a recent session
        # Upstox hasn't published yet — say so instead of skipping silently.
        wk = [st.start + timedelta(days=i) for i in range((last - st.start).days + 1)]
        job["missing"] = [d.isoformat() for d in wk if d.weekday() < 5 and d.isoformat() not in spot]
        sem = asyncio.Semaphore(3)  # overlap network waits; the throttle caps the rate

        async def one(d: str, bars):
            async with sem:
                job["current"] = d
                try:
                    engine = smc.run_day if st.strategy == "smc" else vwap.run_day
                    res = await engine(date.fromisoformat(d), st, bars)
                    out = {"date": res.date, "expiry": res.expiry, "note": res.note,
                           "trades": [vwap.trade_dict(t) for t in res.trades]}
                except Exception as e:  # one bad day must not kill the run
                    out = {"date": d, "expiry": None, "note": f"error: {e}", "trades": []}
                job["days"].append(out)
                job["done"] += 1

        await asyncio.gather(*(one(d, b) for d, b in spot.items()))
        job["days"].sort(key=lambda x: x["date"])
        job["status"] = "done"
    except Exception as e:
        job["status"], job["error"] = "error", str(e)


@app.get("/vwap", response_class=HTMLResponse)
async def vwap_page(request: Request):
    end = upstox.today_ist() - timedelta(days=1)
    start = end - timedelta(days=61)
    from dataclasses import asdict, replace
    presets = {k: {f: v for f, v in asdict(replace(vwap.Settings(start=start, end=end), **ov)).items()
                   if f not in ("start", "end")} for k, ov in vwap.PRESETS.items()}
    return templates.TemplateResponse(request, "vwap.html", {"start": start.isoformat(), "end": end.isoformat(),
                                                             "presets": presets})


@app.post("/api/vwap/run")
async def vwap_run(request: Request):
    st = _settings_from(await request.json())
    jid = uuid.uuid4().hex[:10]
    body_preset = (await request.json()).get("preset") or "frontend_live"
    job = {"id": jid, "settings": st, "preset": body_preset, "status": "running", "done": 0, "total": None,
           "current": None, "days": [], "error": None, "missing": []}
    JOBS[jid] = job
    job["task"] = asyncio.create_task(_run_job(job))
    return {"id": jid}


@app.get("/api/vwap/job/{jid}")
async def vwap_job(jid: str, since: int = 0):
    job = JOBS.get(jid) or {}
    if not job:
        raise HTTPException(404, "unknown job (the server may have restarted)")
    return {"id": jid, "status": job["status"], "done": job["done"], "total": job["total"],
            "current": job["current"], "error": job["error"], "missing": job["missing"],
            "days": sorted(job["days"], key=lambda x: x["date"]) if job["status"] != "running" else job["days"][since:]}


def _summary_csv(days: list[dict], qty: int) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "expiry", "trade", "option", "type", "strike", "entry_time", "entry_price", "sl", "target",
                "signal_candle", "candle_close", "vwap", "other_side", "result", "exit_time", "exit_price",
                "pnl_pts", "pnl_pct", f"pnl_rupees_qty{qty}", "peak_pts", "sl_moved_to_breakeven", "t1_time", "why", "file", "note"])
    for d in days:
        if not d["trades"]:
            w.writerow([d["date"], d["expiry"] or "", "", "", "", "", "", "", "", "", "", "", "", "", "NO_TRADE",
                        "", "", "", "", "", "", "", "", "", "", d["note"]])
        for i, t in enumerate(d["trades"], 1):
            w.writerow([t["date"], t["expiry"], i, t["symbol"], t["type"], f'{t["strike"]:g}', t["entry_time"], t["entry"],
                        t["sl"], t["target"], t["candle"], t["close"], t["vwap"], t["other"], t["status"],
                        t["exit_time"] or "", t["exit"], t["pnl"], t["pnl_pct"], round(t["pnl"] * qty, 2), t["peak"],
                        t["sl_to_breakeven"], t.get("t1_time") or "", t.get("why") or "",
                        csv_name(t["expiry"], t["strike"], t["type"], "1m", t["date"], t["date"]), ""])
    return buf.getvalue()


@app.get("/api/vwap/job/{jid}/zip")
async def vwap_zip(jid: str, qty: int = 65):
    job = JOBS.get(jid)
    if not job or job["status"] != "done":
        raise HTTPException(409, "run not finished")
    st: vwap.Settings = job["settings"]
    days = sorted(job["days"], key=lambda x: x["date"])
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.csv", _summary_csv(days, qty))
        seen = set()
        exp_set = set(await upstox.expired_expiries())
        for d in days:
            for t in d["trades"]:
                name = csv_name(t["expiry"], t["strike"], t["type"], "1m", t["date"], t["date"])
                if name in seen:
                    continue
                seen.add(name)
                day = date.fromisoformat(t["date"])
                rows = await upstox.candles(t["instrument_key"], "1m", day, day, t["expiry"] in exp_set)
                rows = [r for r in rows if "09:15" <= r[0][11:16] <= "15:29"]
                z.writestr(f"options/{name}", candles_csv(rows))
    fname = f"vwap930_{st.start}_to_{st.end}.zip"
    return StreamingResponse(iter([mem.getvalue()]), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/vwap/job/{jid}/summary.csv")
async def vwap_summary(jid: str, qty: int = 65):
    job = JOBS.get(jid)
    if not job or job["status"] != "done":
        raise HTTPException(409, "run not finished")
    st: vwap.Settings = job["settings"]
    return StreamingResponse(iter([_summary_csv(sorted(job["days"], key=lambda x: x["date"]), qty)]),
                             media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="vwap930_summary_{st.start}_to_{st.end}.csv"'})


PRESET_LABELS = {"frontend_live": "VWAP 9:30 — frontend LIVE rules", "smc_live": "SMC — frontend LIVE rules"}


@app.get("/api/vwap/job/{jid}/report.pdf")
async def vwap_pdf(jid: str, lots: int = 20, lot_size: int = 65):
    job = JOBS.get(jid)
    if not job or job["status"] != "done":
        raise HTTPException(409, "run not finished")
    st: vwap.Settings = job["settings"]
    days = sorted(job["days"], key=lambda x: x["date"])
    label = PRESET_LABELS.get(job.get("preset"), "Strategy backtest")
    rules = (f"₹{st.min_premium:g}–{st.max_premium:g} · SL {st.sl_pct:g}% · target {st.target_pct:g}%"
             + (f" · T1 {st.target1_pct:g}%" if st.target1_pct else "")
             + (f" · breakeven at +{st.breakeven_pct:g}%" if st.breakeven_pct else "")
             + f" · entries from {st.entry_start} · max {st.max_trades} trades/day")
    missing = f" · no Upstox data: {', '.join(job['missing'])}" if job.get("missing") else ""
    pdf = pdf_report.build(days, title=f"{label}  ·  {st.start} → {st.end}",
                           subtitle=f"{rules} · {len(days)} trading days · lots {lots}×{lot_size}{missing}",
                           lots=lots, lot_size=lot_size)
    name = f"{'smc' if st.strategy == 'smc' else 'vwap930'}_{st.start}_to_{st.end}.pdf"
    return StreamingResponse(iter([pdf]), media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})
