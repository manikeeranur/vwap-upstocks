import csv
import io
from datetime import date, datetime
from pathlib import Path

from fastapi import HTTPException
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

def _parse_day(s: str, name: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, f"{name} must be YYYY-MM-DD")


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
