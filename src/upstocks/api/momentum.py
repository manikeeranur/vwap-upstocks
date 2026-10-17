import csv
import io

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from .. import upstox
from ..schemas import MomentumRequest
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


@router.get("/api/momentum/job")
async def momentum_job(request: Request):
    return request.app.state.momentum.snapshot()


@router.get("/api/momentum/result")
async def momentum_result(request: Request):
    result = request.app.state.momentum.result
    if result is None:
        raise HTTPException(404, "No momentum backtest yet")
    return result


@router.get("/api/momentum/trades.csv")
async def momentum_trades(request: Request):
    result = request.app.state.momentum.result
    if result is None:
        raise HTTPException(404, "No momentum backtest yet")
    buf = io.StringIO()
    cols = ["symbol", "entry_date", "entry_time", "entry", "exit_date", "exit_time", "exit", "qty", "pnl", "pnl_pct", "days", "entry_rank", "exit_rank", "reason"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(result["trades"])
    st = result["settings"]
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="momentum_trades_{st["start"]}_to_{st["end"]}.csv"'})
