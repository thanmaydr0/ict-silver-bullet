"""Versioned dashboard contract. Datetimes are aware; numbers are finite."""
from typing import Generic, Literal, TypeVar
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, FiniteFloat

T = TypeVar("T")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class Panel(Model, Generic[T]):
    status: Literal["ready", "empty", "stale", "unavailable"] = "unavailable"
    data: T | None = None
    updated_at: AwareDatetime | None = None
    message: str | None = "No data yet"


class EquityPoint(Model):
    ts: AwareDatetime
    equity: FiniteFloat
    daily_limit: FiniteFloat | None = None
    overall_limit: FiniteFloat | None = None


class Account(Model):
    ts: AwareDatetime
    equity: FiniteFloat
    balance: FiniteFloat | None = None
    daily_dd_pct: FiniteFloat | None = None
    overall_dd_pct: FiniteFloat | None = None


class EquityData(Model):
    points: list[EquityPoint]
    latest: Account | None = None
    window_hours: int = 24


class Gauge(Model):
    value: FiniteFloat | None
    soft: FiniteFloat
    hard: FiniteFloat
    limit: FiniteFloat


class RiskData(Model):
    daily: Gauge
    overall: Gauge


class Killzone(Model):
    state: Literal["armed", "idle"]
    active_window: str | None
    closes_at: AwareDatetime | None
    next_window: str
    opens_at: AwareDatetime


class Trade(Model):
    id: int | None = None
    timestamp: AwareDatetime | None
    pair: str | None
    direction: str | None
    entry: FiniteFloat | None
    stop_loss: FiniteFloat | None
    take_profit: FiniteFloat | None
    confluence_score: FiniteFloat | None
    rr: FiniteFloat | None
    status: str | None
    realized_r: FiniteFloat | None


class Position(Model):
    id: int | None = None
    pair: str | None
    direction: str | None
    leg: str | None
    lots: FiniteFloat | None
    floating_pnl: FiniteFloat | None


class NewsEvent(Model):
    title: str
    currency: str
    scheduled_at: AwareDatetime


class Blackout(Model):
    pair: str
    blocked: bool | None


class NewsData(Model):
    events: list[NewsEvent]
    blackout: list[Blackout]


class Candle(Model):
    ts: AwareDatetime
    open: FiniteFloat
    high: FiniteFloat
    low: FiniteFloat
    close: FiniteFloat


class Overlay(Model):
    label: str
    kind: Literal["line", "band"]
    bottom: FiniteFloat
    top: FiniteFloat


class SetupData(Model):
    signal_id: int | None
    pair: str
    direction: str | None
    detected_at: AwareDatetime | None
    candles: list[Candle]
    overlays: list[Overlay]
    missing_evidence: list[str]
    source: Literal["recorded", "historical_context"]


class DashboardSnapshot(Model):
    schema_version: Literal[1] = 1
    generated_at: AwareDatetime
    collected_at: AwareDatetime | None = None
    poll_seconds: int = 10
    stale_after_seconds: int = 30
    equity: Panel[EquityData] = Field(default_factory=Panel[EquityData])
    risk: Panel[RiskData] = Field(default_factory=Panel[RiskData])
    killzone: Killzone
    trades: Panel[list[Trade]] = Field(default_factory=Panel[list[Trade]])
    positions: Panel[list[Position]] = Field(default_factory=Panel[list[Position]])
    news: Panel[NewsData] = Field(default_factory=Panel[NewsData])
    setup: Panel[SetupData] = Field(default_factory=Panel[SetupData])


class Login(Model):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


class SessionInfo(Model):
    username: str
    expires_at: AwareDatetime
