"""Firecrawl calendar extraction with cached fallback and explicit health.

The pipeline must check calendar_is_healthy() before allowing trades: an empty
cold-start fallback alone cannot express a failed safety feed. Cache is local
to this process and intentionally survives failed refreshes.
"""

from copy import deepcopy
from datetime import datetime, timedelta
from threading import Lock
import time
from typing import Literal

import requests
from pydantic import AwareDatetime, BaseModel, Field, field_validator

from brain.config import load_config
from brain.db.supabase_client import insert_news_events
from brain.logging_setup import setup_logging
from brain.news.blackout import UTC

logger = setup_logging("news_calendar")
_lock = Lock()
_cached: list[dict] = []
_last_success: datetime | None = None
_healthy = False


class CalendarEvent(BaseModel):
    title: str = Field(min_length=1)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    impact: Literal["high", "medium", "low"]
    scheduled_at: AwareDatetime
    forecast: str | None = None
    previous: str | None = None

    @field_validator("scheduled_at")
    @classmethod
    def to_utc(cls, value):
        return value.astimezone(UTC)


def calendar_is_healthy() -> bool:
    # NOTE: Fail closed after any failed refresh or 1 hour without a refresh.
    with _lock:
        return bool(_healthy and _last_success and
                    datetime.now(UTC) - _last_success <= timedelta(hours=1))


def fetch_calendar() -> list[dict]:
    """Fetch this week's events, persist validated rows, or return a copy of cache."""
    global _cached, _last_success, _healthy
    with _lock:
        try:
            key = load_config().FIRECRAWL_API_KEY
            if not key:
                raise ValueError("Calendar API key is unavailable")
            schema = {"type": "object", "properties": {
                "events": {"type": "array", "items": CalendarEvent.model_json_schema()}},
                "required": ["events"]}
            payload = {
                "url": "https://www.forexfactory.com/calendar?week=this",
                "maxAge": 0,
                "formats": [{"type": "json", "schema": schema, "prompt": (
                    "Extract ALL calendar events for this week, including past events. "
                    "Use title for event name, ISO currency, and high/medium/low impact. "
                    "Resolve full date/year and scheduled_at to ISO8601 with the page's "
                    "EXPLICIT displayed timezone offset, converting to UTC. Never guess "
                    "timezone or time. If time is tentative/all-day or timezone is unknown, "
                    "use null scheduled_at so validation fails safely. Include forecast "
                    "and previous as strings or null. Do not fabricate events." )}]}
            for attempt in range(3):
                try:
                    response = requests.post(
                        "https://api.firecrawl.dev/v2/scrape",
                        headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=60)
                    response.raise_for_status()
                    body = response.json()
                    if body.get("success") is not True:
                        raise ValueError("Firecrawl reported failure")
                    data = body["data"]
                    status = data.get("metadata", {}).get("statusCode", 200)
                    if status != 304 and not 200 <= status < 300:
                        raise ValueError("Calendar page returned an error")
                    raw = data["json"]["events"]
                    if not isinstance(raw, list) or not raw:
                        # NOTE: Empty/partially invalid extraction never clears safety cache.
                        raise ValueError("Calendar extraction has no usable events")
                    rows = [CalendarEvent.model_validate(row).model_dump() for row in raw]
                    break
                except Exception:
                    logger.warning("Calendar scrape attempt %s failed", attempt + 1)
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
            _cached = deepcopy(rows)
            _last_success = datetime.now(UTC)
            _healthy = True
            try:
                insert_news_events(rows)
            except Exception:
                logger.warning("Calendar persistence failed; retaining fetched events")
                _healthy = False
            return deepcopy(_cached)
        except Exception:
            _healthy = False
            logger.error("Calendar unavailable; returning %s cached events; trading must pause", len(_cached))
            return deepcopy(_cached)
