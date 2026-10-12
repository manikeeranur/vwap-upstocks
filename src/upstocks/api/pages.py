from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import live, upstox, vwap
from .common import templates

router = APIRouter()

@router.get("/")
async def home():
    return RedirectResponse("/paper")


@router.get("/paper", response_class=HTMLResponse)
async def paper_page(request: Request):
    return templates.TemplateResponse(request, "paper.html", {"labels": live.LABELS, "presets": live.PRESETS})


@router.get("/journal", response_class=HTMLResponse)
async def journal_page(request: Request):
    return templates.TemplateResponse(request, "journal.html", {"labels": live.LABELS, "presets": live.PRESETS,
                                                                "today": upstox.today_ist().isoformat()})


@router.get("/calendar", response_class=HTMLResponse)
async def calendar_page(request: Request):
    return templates.TemplateResponse(request, "calendar.html", {"labels": live.LABELS, "presets": live.PRESETS,
                                                                 "today": upstox.today_ist().isoformat()})


@router.get("/backtest", response_class=HTMLResponse)
async def backtest(request: Request):
    """SL/target check over uploaded CSVs — parsing and maths run in the browser."""
    return templates.TemplateResponse(request, "backtest.html", {})


@router.get("/vwap", response_class=HTMLResponse)
async def vwap_page(request: Request):
    # From = 1st of the current month, To = today (IST).
    end = upstox.today_ist()
    start = end.replace(day=1)
    from dataclasses import asdict, replace
    presets = {k: {f: v for f, v in asdict(replace(vwap.Settings(start=start, end=end), **ov)).items()
                   if f not in ("start", "end")} for k, ov in vwap.PRESETS.items()}
    return templates.TemplateResponse(request, "vwap.html", {"start": start.isoformat(), "end": end.isoformat(),
                                                             "presets": presets})
