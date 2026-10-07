"""Lazy, fault-tolerant database access for unattended processes."""

from datetime import datetime, timedelta
from decimal import Decimal
from functools import wraps
import logging
from zoneinfo import ZoneInfo

from brain.config import load_config

logger = logging.getLogger("brain")
_client = None
UTC = ZoneInfo("UTC")


def _get_client():
    global _client
    if _client is None:
        from supabase import create_client
        config = load_config()
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
    return _client


def _safe(default=None):
    """Catch client creation, transport, API and response decoding failures."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception as exc:
                # Avoid including exceptions that may contain credentials or payloads.
                logger.error("%s failed (%s)", function.__name__, type(exc).__name__)
                return [] if default is list else default
        return wrapped
    return decorate


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Database timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _payload(value):
    if isinstance(value, datetime):
        return _utc_iso(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_payload(item) for item in value]
    return value


def _rows(query) -> list[dict]:
    return query.execute().data or []


def _first(query) -> dict | None:
    rows = _rows(query.limit(1))
    return rows[0] if rows else None


def _all_rows(query) -> list[dict]:
    """Page unbounded reads rather than silently accepting the API row cap."""
    rows = []
    while True:
        page = _rows(query.range(len(rows), len(rows) + 999))
        rows.extend(page)
        if len(page) < 1000:
            return rows


@_safe()
def insert_candles(rows: list[dict]) -> None:
    """Upsert candles by symbol, interval and UTC timestamp."""
    if rows:
        # Collapse duplicates in a single batch; Postgres cannot update a row twice.
        records = _payload(rows)
        unique = {(row["pair"], row["timeframe"], row["ts"]): row for row in records}
        _get_client().table("candles").upsert(
            list(unique.values()), on_conflict="pair,timeframe,ts"
        ).execute()


@_safe(list)
def get_pending_signals() -> list[dict]:
    """Read pending signals oldest first."""
    return _all_rows(_get_client().table("signals").select("*").eq(
        "status", "pending"
    ).order("detected_at").order("id"))


@_safe()
def update_signal_status(signal_id: int, status: str) -> None:
    _get_client().table("signals").update({"status": status}).eq("id", signal_id).execute()


@_safe()
def insert_equity_snapshot(snapshot: dict) -> None:
    _get_client().table("equity_snapshots").insert(_payload(snapshot)).execute()


@_safe()
def get_latest_equity_snapshot() -> dict | None:
    return _first(_get_client().table("equity_snapshots").select("*").order(
        "ts", desc=True
    ).order("id", desc=True))


@_safe(list)
def get_latest_candles(pair: str, timeframe: str, limit: int) -> list[dict]:
    """Return the latest window in chronological order for indicator calculations."""
    if limit <= 0:
        return []
    rows = _rows(_get_client().table("candles").select("*").eq("pair", pair).eq(
        "timeframe", timeframe
    ).order("ts", desc=True).limit(limit))
    return rows[::-1]


@_safe()
def insert_signal(signal: dict) -> int | None:
    rows = _rows(_get_client().table("signals").insert(_payload(signal)))
    return int(rows[0]["id"]) if rows else None


@_safe()
def get_latest_signal() -> dict | None:
    return _first(_get_client().table("signals").select("*").order(
        "detected_at", desc=True
    ).order("id", desc=True))


@_safe(list)
def get_equity_snapshots(since: datetime) -> list[dict]:
    return _all_rows(_get_client().table("equity_snapshots").select("*").gte(
        "ts", _utc_iso(since)
    ).order("ts").order("id"))


@_safe()
def get_max_equity() -> float | None:
    row = _first(_get_client().table("equity_snapshots").select("equity").not_.is_(
        "equity", "null"
    ).order("equity", desc=True))
    return float(row["equity"]) if row else None


_TRADE_SELECT = "*,signals(pair,direction,confluence_score,entry,stop_loss,take_profit)"
_SIGNAL_FIELDS = ("pair", "direction", "confluence_score", "entry", "stop_loss", "take_profit")


def _joined_trades(rows: list[dict]) -> list[dict]:
    result = []
    for row in rows:
        trade = dict(row)
        signal = trade.pop("signals", None) or {}
        trade.update({field: signal.get(field) for field in _SIGNAL_FIELDS})
        result.append(trade)
    return result


@_safe(list)
def get_trades(limit: int) -> list[dict]:
    if limit <= 0:
        return []
    return _joined_trades(_rows(_get_client().table("trades").select(
        _TRADE_SELECT
    ).order("opened_at", desc=True).order("id", desc=True).limit(limit)))


@_safe(list)
def get_trades_since(since: datetime) -> list[dict]:
    # NOTE: The time boundary refers to opened_at, including still-open trades.
    return _joined_trades(_all_rows(_get_client().table("trades").select(
        _TRADE_SELECT
    ).gte("opened_at", _utc_iso(since)).order("opened_at").order("id")))


@_safe(list)
def get_open_trades() -> list[dict]:
    return _joined_trades(_all_rows(_get_client().table("trades").select(
        _TRADE_SELECT
    ).eq("status", "open").order("opened_at").order("id")))


@_safe()
def insert_news_events(rows: list[dict]) -> None:
    if rows:
        _get_client().table("news_events").insert(_payload(rows)).execute()


def _now() -> datetime:
    return datetime.now(UTC)


@_safe(list)
def get_upcoming_news(within_minutes: int) -> list[dict]:
    if within_minutes < 0:
        return []
    now = _now()
    return _all_rows(_get_client().table("news_events").select("*").gte(
        "scheduled_at", _utc_iso(now)
    ).lte("scheduled_at", _utc_iso(now + timedelta(minutes=within_minutes))).order(
        "scheduled_at"
    ).order("id"))
