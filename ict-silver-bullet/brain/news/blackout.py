"""Inclusive ±15 minute gate; scorer input is the negation of this result."""

from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value.astimezone(UTC)


def _currencies(pair: str) -> tuple[str, str]:
    symbol = re.sub(r"[^A-Z]", "", pair.upper())
    if len(symbol) < 6:
        raise ValueError("Expected a pair such as EURUSD or EUR/USD")
    return symbol[:3], symbol[3:6]


def is_in_news_blackout(now, active_pair, upcoming_events) -> bool:
    now = _utc(now)
    currencies = _currencies(active_pair)
    for event in upcoming_events:
        if (str(event.get("impact", "")).lower() != "high"
                or str(event.get("currency", "")).upper() not in currencies):
            continue
        try:
            scheduled = _utc(event["scheduled_at"])
        except (KeyError, TypeError, ValueError):
            # NOTE: An ambiguous relevant high-impact time fails closed.
            return True
        if abs(now - scheduled) <= timedelta(minutes=15):
            return True
    return False
