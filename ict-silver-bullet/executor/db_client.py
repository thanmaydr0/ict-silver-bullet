"""Lazy, fault-tolerant database access for unattended processes."""

from datetime import datetime, timedelta
from decimal import Decimal
from functools import wraps
import logging
from zoneinfo import ZoneInfo

from executor.config import load_config

logger = logging.getLogger("candles")
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


@_safe()
def insert_trade(trade: dict) -> int | None:
    rows = _rows(_get_client().table("trades").insert(_payload(trade)))
    return int(rows[0]["id"]) if rows else None


@_safe()
def update_trade(trade_id: int, fields: dict) -> None:
    _get_client().table("trades").update(_payload(fields)).eq("id", trade_id).execute()
