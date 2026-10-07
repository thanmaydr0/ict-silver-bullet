"""NY-local half-open killzone windows, with UTC outputs and dynamic DST."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
KILLZONES = {"london": (time(3), time(4)), "ny_am": (time(10), time(11)),
             "ny_pm": (time(14), time(15))}


def _local(now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(NEW_YORK)


def current_killzone(now: datetime) -> str | None:
    """Return the window containing now: open inclusive, close exclusive."""
    local = _local(now)
    return next((name for name, (start, end) in KILLZONES.items()
                 if start <= local.time() < end), None)


def next_killzone_open(now: datetime) -> tuple[str, datetime]:
    """Return the next strictly future open in UTC, including on weekends.

    # NOTE: Windows recur every calendar day; market availability is a separate guard.
    """
    local = _local(now)
    for offset in (0, 1):
        day = local.date() + timedelta(days=offset)
        for name, (start, _) in KILLZONES.items():
            opening = datetime.combine(day, start, NEW_YORK).astimezone(timezone.utc)
            if opening > now:
                return name, opening
    raise AssertionError("a future window always exists")


def killzone_close(name: str, now: datetime) -> datetime:
    """Return named window's close on now's NY-local calendar date, in UTC."""
    local = _local(now)
    if name not in KILLZONES:
        raise ValueError(f"unknown killzone: {name}")
    return datetime.combine(local.date(), KILLZONES[name][1], NEW_YORK).astimezone(timezone.utc)
