from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from brain.scheduler.killzone_clock import (
    KILLZONES, current_killzone, killzone_close, next_killzone_open,
)

NY = ZoneInfo("America/New_York")


@pytest.mark.parametrize("date,offset", [
    ((2026, 3, 7), 5), ((2026, 3, 8), 4), ((2026, 3, 9), 4),
    ((2026, 10, 31), 4), ((2026, 11, 1), 5), ((2026, 11, 2), 5),
])
@pytest.mark.parametrize("name", KILLZONES)
def test_edges_across_dst(date, offset, name):
    start, end = KILLZONES[name]
    opening = datetime(*date, start.hour, tzinfo=NY).astimezone(timezone.utc)
    closing = datetime(*date, end.hour, tzinfo=NY).astimezone(timezone.utc)
    assert opening.hour == start.hour + offset
    assert current_killzone(opening - timedelta(microseconds=1)) is None
    assert current_killzone(opening) == name
    assert current_killzone(closing - timedelta(microseconds=1)) == name
    assert current_killzone(closing) is None
    assert killzone_close(name, opening) == closing
    assert next_killzone_open(opening - timedelta(microseconds=1)) == (name, opening)


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 3, 7, 15, tzinfo=NY), datetime(2026, 3, 8, 7, tzinfo=timezone.utc)),
    (datetime(2026, 10, 31, 15, tzinfo=NY), datetime(2026, 11, 1, 8, tzinfo=timezone.utc)),
])
def test_next_day_dst_conversion(now, expected):
    assert next_killzone_open(now) == ("london", expected)


def test_next_open_is_strictly_future():
    now = datetime(2026, 7, 1, 3, tzinfo=NY)
    assert next_killzone_open(now) == ("ny_am", datetime(2026, 7, 1, 14, tzinfo=timezone.utc))
    assert next_killzone_open(now.replace(hour=10, minute=30))[0] == "ny_pm"


def test_close_uses_ny_date_even_when_utc_date_differs():
    now = datetime(2026, 7, 2, 1, tzinfo=timezone.utc)
    assert killzone_close("ny_pm", now) == datetime(2026, 7, 1, 19, tzinfo=timezone.utc)


@pytest.mark.parametrize("function,args", [(current_killzone, ()), (next_killzone_open, ()),
                                            (killzone_close, ("london",))])
def test_reject_naive(function, args):
    with pytest.raises(ValueError, match="timezone-aware"):
        function(*args, datetime(2026, 1, 1))


def test_unknown_window():
    with pytest.raises(ValueError, match="unknown"):
        killzone_close("bad", datetime.now(timezone.utc))
