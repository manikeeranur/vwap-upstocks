import asyncio
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from starlette.background import BackgroundTask

from .. import diagnostics, upstox
from .common import templates

router = APIRouter()


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request):
    return templates.TemplateResponse(request, "activity.html", {"today": upstox.today_ist().isoformat()})


@router.get("/api/activity")
async def activity(request: Request, day: date, level: Literal["INFO", "WARNING", "ERROR", "CRITICAL"] | None = None,
                   component: str | None = Query(None, max_length=40), q: str = Query("", max_length=200),
                   include_http: bool = False, before: int | None = Query(None, ge=1), limit: int = Query(100, ge=1, le=500)):
    return await asyncio.to_thread(diagnostics.events, request.app.state.database, day=day, level=level,
                                   component=component, query=q, include_http=include_http, before=before, limit=limit)


@router.get("/api/activity/session")
async def session(request: Request, day: date):
    return await asyncio.to_thread(diagnostics.session_summary, request.app.state.database, day)


@router.get("/api/activity/session.zip")
async def session_export(request: Request, day: date):
    lock = request.app.state.export_lock
    if lock.locked():
        raise HTTPException(429, "A session export is being prepared. Try again shortly.", headers={"Retry-After": "5"})
    async with lock:
        archive = await asyncio.to_thread(diagnostics.build_session_export, request.app.state.database, day)

    def chunks():
        try:
            while block := archive.read(64 * 1024):
                yield block
        finally:
            archive.close()

    return StreamingResponse(chunks(), media_type="application/zip", background=BackgroundTask(archive.close),
                             headers={"Content-Disposition": f'attachment; filename="upstocks-session-{day}.zip"'})
