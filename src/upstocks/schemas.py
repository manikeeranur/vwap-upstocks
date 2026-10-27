"""HTTP input contracts. In particular, strings never enable live orders."""
from dataclasses import replace
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from . import vwap

Preset = Literal["frontend_live", "smc_live", "ema25_vwap", "ema_pullback", "smc_ema_combo", "smc_tuned", "harmonic"]
Broker = Literal["upstox", "kite"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ConfigUpdate(Input):
    revision: int = Field(strict=True, ge=0)
    preset: Preset | None = None
    broker: Broker | None = None
    on: StrictBool | None = None
    live: StrictBool | None = None
    lots: int | None = Field(default=None, strict=True, ge=1, le=10000)
    lot_size: int | None = Field(default=None, strict=True, ge=1, le=10000)
    confirm: Literal["LIVE"] | None = None
    entries_paused: StrictBool | None = None

    @model_validator(mode="after")
    def valid_change(self):
        if self.preset is None and any(v is not None for v in (self.on, self.live, self.lots)):
            raise ValueError("preset is required for strategy changes")
        if (self.live is not None or self.lots is not None) and self.broker is None:
            raise ValueError("broker is required for live and lots")
        if (self.live is True or self.entries_paused is False) and self.confirm != "LIVE":
            raise ValueError("Enabling live orders or resuming entries needs confirm: LIVE")
        if all(v is None for v in (self.on, self.live, self.lots, self.lot_size, self.entries_paused)):
            raise ValueError("Provide a setting to update")
        return self


class PresetRequest(Input):
    preset: Preset
    broker: Broker | None = None


class Login(Input):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class BrokerKeys(Input):
    broker: Broker
    api_key: str | None = Field(default=None, max_length=200, pattern=r"^[A-Za-z0-9_\-]*$")
    api_secret: str | None = Field(default=None, max_length=200)


class BrokerRequest(Input):
    broker: Broker


class TelegramSetting(Input):
    chat_id: str = Field(default="", max_length=40, pattern=r"^-?[0-9]*$")


class NoteUpdate(Input):
    id: str = Field(min_length=1, max_length=512)
    text: str = Field(default="", max_length=2000)


class BacktestRequest(Input):
    start: date
    end: date
    preset: Preset = "frontend_live"
    min_premium: float | None = Field(default=None, ge=0, le=100000)
    max_premium: float | None = Field(default=None, gt=0, le=100000)
    sl_pct: float | None = Field(default=None, gt=0, le=100)
    target_pct: float | None = Field(default=None, gt=0, le=10000)
    target1_pct: float | None = Field(default=None, ge=0, le=10000)
    breakeven_pct: float | None = Field(default=None, ge=0, le=10000)
    stagnant_hours: float | None = Field(default=None, ge=0, le=24)
    stagnant_points: float | None = Field(default=None, ge=0, le=100000)
    candle_minutes: int | None = Field(default=None, ge=1, le=60)
    max_trades: int | None = Field(default=None, ge=1, le=100)
    cooldown_min: int | None = Field(default=None, ge=0, le=1440)
    chain_strikes: int | None = Field(default=None, ge=1, le=100)
    max_entry_visits: int | None = Field(default=None, ge=0, le=100)
    index_trend_bars: int | None = Field(default=None, ge=1, le=100)
    ep_rev_activate: float | None = Field(default=None, ge=0, le=10000)
    entry_start: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    time_exit: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    last_entry: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    strong_green: StrictBool | None = None
    live: StrictBool | None = None
    atm_offsets_only: StrictBool | None = None
    align_checkpoints: StrictBool | None = None
    include_entry_candle: StrictBool | None = None
    time_exit_first: StrictBool | None = None
    ema_cross: StrictBool | None = None
    any_strike_in_band: StrictBool | None = None
    supertrend: StrictBool | None = None
    index_trend: StrictBool | None = None
    trend_entry: StrictBool | None = None
    rev_exit: StrictBool | None = None

    @field_validator("start", "end", mode="before")
    @classmethod
    def iso_date(cls, value):
        if isinstance(value, str):
            return date.fromisoformat(value)
        if type(value) is date:
            return value
        raise ValueError("Use a YYYY-MM-DD date")

    @model_validator(mode="after")
    def valid_range(self):
        if self.start > self.end or (self.end - self.start).days > 400:
            raise ValueError("Choose a date range of at most 400 days, with start before end")
        st = self.settings()
        if st.min_premium > st.max_premium:
            raise ValueError("Minimum premium must not exceed maximum premium")
        if st.entry_start > st.last_entry or st.entry_start > st.time_exit:
            raise ValueError("Entry start must precede last entry and exit time")
        return self

    def settings(self):
        changes = self.model_dump(exclude={"start", "end", "preset"}, exclude_none=True)
        return replace(vwap.Settings(start=self.start, end=self.end), **(vwap.PRESETS[self.preset] | changes))


class MomentumRequest(Input):
    start: date
    end: date
    capital: float = Field(default=1_000_000, ge=10_000, le=1e11)
    slots: int = Field(default=1, strict=True, ge=1, le=50)
    universe: int = Field(default=500, strict=True, ge=20, le=2000)
    exit_rank: int = Field(default=20, strict=True, ge=1, le=2000)
    sma_days: int = Field(default=100, strict=True, ge=10, le=250)
    cost_pct: float = Field(default=0.1, ge=0, le=5)
    score: Literal["risk_adjusted", "12-1", "tuned"] = "risk_adjusted"
    refresh_all: StrictBool = False

    @model_validator(mode="after")
    def valid_range(self):
        if self.start >= self.end:
            raise ValueError("From must be before To")
        if self.start < date(2001, 1, 1):
            raise ValueError("From must be 2001 or later")
        return self

    def settings(self):
        from . import momentum
        return momentum.Settings(**self.model_dump(exclude={"refresh_all"}))
