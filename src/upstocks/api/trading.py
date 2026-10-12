import asyncio
from datetime import date

from fastapi import APIRouter, HTTPException, Request

from .. import upstox
from ..schemas import ConfigUpdate, NoteUpdate, PresetRequest

router = APIRouter()


@router.get("/api/paper")
async def paper_state(request: Request):
    return request.app.state.trading.snapshot()


@router.get("/api/paper/quotes")
async def paper_quotes(request: Request):
    return await request.app.state.quotes.get()


@router.post("/api/paper/config")
async def paper_config(body: ConfigUpdate, request: Request):
    changes = body.model_dump(exclude={"preset", "confirm"}, exclude_none=True)
    return {"state": await request.app.state.trading.configure(body.preset, **changes)}


@router.get("/api/journal")
async def journal_data(request: Request, start: date, end: date):
    if start > end:
        raise HTTPException(422, "start must be before end")
    return await asyncio.to_thread(request.app.state.repository.journal, start.isoformat(), end.isoformat())


@router.post("/api/journal/note")
async def journal_note(body: NoteUpdate, request: Request):
    await asyncio.to_thread(request.app.state.repository.set_note, body.id, body.text)
    return {"ok": True}


@router.post("/api/paper/squareoff")
async def paper_squareoff(body: PresetRequest, request: Request):
    return {"sold": await request.app.state.trading.square_off(body.preset)}


@router.post("/api/paper/scan")
async def paper_scan(request: Request, day: date | None = None):
    if day and day > upstox.today_ist():
        raise HTTPException(422, "Cannot scan a future date")
    return await request.app.state.trading.scan(day)
