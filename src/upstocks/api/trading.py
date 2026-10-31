import asyncio
from datetime import date

from fastapi import APIRouter, HTTPException, Request

from .. import upstox
from ..schemas import ConfigUpdate, NoteUpdate, PresetRequest, IntradayRecovery

router = APIRouter()


@router.get("/api/paper")
async def paper_state(request: Request, intraday: bool = False):
    return request.app.state.trading.snapshot(request.state.user_id, intraday=intraday)


@router.get("/api/paper/execution-details")
async def paper_execution_details(request: Request):
    trading = request.app.state.trading
    return await trading.execution_details.get(trading.snapshot(request.state.user_id, intraday=True))


@router.get("/api/paper/fills")
async def paper_fills(request: Request):
    """Real fill prices of today's live orders, for the Overview's live P&L."""
    return await request.app.state.trading.live_fills(request.state.user_id)


@router.get("/api/paper/quotes")
async def paper_quotes(request: Request):
    return await request.app.state.quotes.get()


@router.post("/api/paper/config")
async def paper_config(body: ConfigUpdate, request: Request):
    changes = body.model_dump(exclude={"preset", "broker", "confirm"}, exclude_none=True)
    await request.app.state.trading.configure(body.preset, user=request.state.user_id, broker=body.broker, **changes)
    return {"state": request.app.state.trading.snapshot(request.state.user_id)["state"]}


@router.get("/api/journal")
async def journal_data(request: Request, start: date, end: date):
    if start > end:
        raise HTTPException(422, "start must be before end")
    return await asyncio.to_thread(request.app.state.repository.journal, start.isoformat(), end.isoformat(), request.state.user_id)


@router.post("/api/journal/note")
async def journal_note(body: NoteUpdate, request: Request):
    await asyncio.to_thread(request.app.state.repository.set_note, body.id, body.text)
    return {"ok": True}


@router.post("/api/paper/squareoff")
async def paper_squareoff(body: PresetRequest, request: Request):
    return {"sold": await request.app.state.trading.square_off(body.preset, user=request.state.user_id, broker=body.broker)}


@router.post("/api/paper/scan")
async def paper_scan(request: Request, day: date | None = None):
    if day and day > upstox.today_ist():
        raise HTTPException(422, "Cannot scan a future date")
    return await request.app.state.trading.scan(day, execute_live=False)

@router.post("/api/paper/recover-exit")
async def recover_exit(body: IntradayRecovery, request: Request):
    return await request.app.state.trading.recover_exit(request.state.user_id,body)
