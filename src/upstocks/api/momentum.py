import asyncio
import csv
from datetime import date, timedelta
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from .. import upstox
from ..schemas import MomentumLive, MomentumRequest
from ..services.momentum import MomentumBusy
from .common import templates

router = APIRouter()


@router.get("/momentum", response_class=HTMLResponse)
async def momentum_page(request: Request):
    end = upstox.today_ist()
    return templates.TemplateResponse(request, "momentum.html", {"start": "2018-01-01", "end": end.isoformat()})


@router.post("/api/momentum/run", status_code=202)
async def momentum_run(body: MomentumRequest, request: Request):
    if body.start > upstox.today_ist():
        raise HTTPException(422, "From must not be in the future")
    try:
        await request.app.state.momentum.submit(body.settings(), body.refresh_all)
    except MomentumBusy as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "10"}) from exc
    return request.app.state.momentum.snapshot()


@router.get("/api/momentum/tuned")
async def momentum_tuned(request: Request):
    """Momentum Tuned's live state; the holding is marked at its last traded price when available."""
    state = request.app.state.momentum_live.snapshot()
    h = state.get("holding")
    if h:
        try:
            h["ltp"] = (await upstox.ltp_quotes([h["key"]])).get(h["key"])
        except Exception:
            h["ltp"] = None
    return state


@router.post("/api/momentum/tuned/live")
async def momentum_tuned_live(body: MomentumLive, request: Request):
    return await request.app.state.momentum_live.configure(body.broker, body.live)


@router.post("/api/momentum/tuned/preview")
async def momentum_tuned_preview(request: Request):
    """What the rules would decide from the latest close (refreshes prices; read-only)."""
    return await request.app.state.momentum_live.preview()


@router.get("/api/momentum/job")
async def momentum_job(request: Request):
    return request.app.state.momentum.snapshot()


async def _saved(request, run_id):
    result = await asyncio.to_thread(request.app.state.momentum.run, run_id)
    if result is None:
        raise HTTPException(404, "No such momentum run" if run_id else "No momentum backtest yet")
    return result


@router.get("/api/momentum/runs")
async def momentum_runs(request: Request):
    return {"runs": await asyncio.to_thread(request.app.state.momentum.runs)}


@router.get("/api/momentum/result")
async def momentum_result(request: Request, id: str | None = Query(None, max_length=12)):
    return await _saved(request, id)


@router.get("/api/momentum/trades.csv")
async def momentum_trades(request: Request, id: str | None = Query(None, max_length=12)):
    result = await _saved(request, id)
    buf = io.StringIO()
    cols = ["symbol", "entry_date", "entry_time", "entry", "exit_date", "exit_time", "exit", "qty", "pnl", "capital_after", "pnl_pct", "days", "entry_rank", "exit_rank", "reason"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(result["trades"])
    st = result["settings"]
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="momentum_trades_{st["start"]}_to_{st["end"]}.csv"'})


@router.get("/api/momentum/chart")
async def momentum_chart(request: Request, symbol: str = Query(..., max_length=40),
                         entry: date = Query(...), exit: date | None = Query(None), day: date | None = Query(None)):
    """Candles for one trade: daily from before the entry to after the exit,
    or with `day`, that session's 1-minute candles (Upstox has them from 2022)."""
    store = request.app.state.momentum.store
    keys = {x["symbol"]: x["key"] for x in await store.instruments()}
    key = keys.get(symbol)
    if key is None:
        raise HTTPException(404, f"Unknown NSE stock {symbol}")
    yesterday = upstox.today_ist() - timedelta(days=1)
    if day is not None:
        if day < date(2022, 1, 1):
            raise HTTPException(404, "Upstox has 1-minute candles only from January 2022")
        rows = await upstox.candles(key, "1m", day, day, False)
        return {"symbol": symbol, "interval": "1m", "candles": [r[:6] for r in rows if "09:15" <= r[0][11:16] <= "15:29"]}
    start = entry - timedelta(days=400)  # room for the moving average before the entry
    end = min((exit or yesterday) + timedelta(days=60), yesterday)
    await store.limiter.wait()
    rows = await upstox.daily_history(key, start, end)
    return {"symbol": symbol, "interval": "1d", "candles": [[r[0][:10], *r[1:6]] for r in rows]}
