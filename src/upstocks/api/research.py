import asyncio
import csv
import io
import zipfile
from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .. import pdf_report, upstox, vwap
from ..schemas import BacktestRequest
from ..services.backtests import BacktestsBusy
from .common import candles_csv, csv_name

router = APIRouter()
PRESET_LABELS = {"frontend_live": "VWAP 9:30 — frontend LIVE rules", "smc_live": "SMC — frontend LIVE rules",
                 "ema25_vwap": "VWAP + EMA25 + SuperTrend", "ema_pullback": "EMA9/EMA26 pullback (Mani)"}


async def _job(request, jid):
    job = await asyncio.to_thread(request.app.state.repository.get_job, jid)
    if not job:
        raise HTTPException(404, "Unknown job")
    settings = job["settings"]
    job["settings"] = vwap.Settings(**{**settings, "start": date.fromisoformat(settings["start"]), "end": date.fromisoformat(settings["end"])})
    return job


@router.post("/api/vwap/run", status_code=202)
async def vwap_run(body: BacktestRequest, request: Request):
    if body.start > upstox.today_ist():
        raise HTTPException(422, "Start must not be in the future")
    try:
        jid = await request.app.state.backtests.submit(body.settings(), body.preset)
    except BacktestsBusy as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "10"}) from exc
    return {"id": jid}


@router.get("/api/vwap/job/{jid}")
async def vwap_job(request: Request, jid: str, since: int = Query(0, ge=0)):
    job = await _job(request, jid)
    return {**{k: job[k] for k in ("id", "status", "done", "total", "current", "error", "missing", "settings", "preset")},
            "days": job["days"][since:] if job["status"] == "running" else job["days"]}


@router.get("/api/vwap/jobs")
async def recent_jobs(request: Request, limit: int = Query(30, ge=1, le=100)):
    return {"jobs": await asyncio.to_thread(request.app.state.repository.recent_jobs, limit)}


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


@router.get("/api/vwap/job/{jid}/zip")
async def vwap_zip(request: Request, jid: str, qty: int = Query(65, ge=1, le=100000000)):
    job = await _job(request, jid)
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


@router.get("/api/vwap/job/{jid}/summary.csv")
async def vwap_summary(request: Request, jid: str, qty: int = Query(65, ge=1, le=100000000)):
    job = await _job(request, jid)
    if not job or job["status"] != "done":
        raise HTTPException(409, "run not finished")
    st: vwap.Settings = job["settings"]
    return StreamingResponse(iter([_summary_csv(sorted(job["days"], key=lambda x: x["date"]), qty)]),
                             media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="vwap930_summary_{st.start}_to_{st.end}.csv"'})


@router.get("/api/vwap/job/{jid}/report.pdf")
async def vwap_pdf(request: Request, jid: str, lots: int = Query(20, ge=1, le=10000), lot_size: int = Query(65, ge=1, le=10000)):
    job = await _job(request, jid)
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
