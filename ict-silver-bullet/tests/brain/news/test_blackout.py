from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest
from brain.news.blackout import is_in_news_blackout

TIME = datetime(2026, 10, 7, 12, tzinfo=ZoneInfo("UTC"))


@pytest.mark.parametrize("minutes,expected", [(-15, True), (15, True), (0, True), (-15.01, False), (15.01, False)])
def test_boundaries(minutes, expected):
    assert is_in_news_blackout(TIME + timedelta(minutes=minutes), "EUR/USD", [
        {"currency": "USD", "impact": "high", "scheduled_at": TIME}]) is expected


@pytest.mark.parametrize("currency,impact", [("USD", "medium"), ("USD", "low"), ("JPY", "high")])
def test_ignored_events(currency, impact):
    assert not is_in_news_blackout(TIME, "EURUSD", [
        {"currency": currency, "impact": impact, "scheduled_at": TIME}])


def test_offsets_and_empty():
    assert is_in_news_blackout(TIME, "eurusd", [
        {"currency": "EUR", "impact": "high", "scheduled_at": "2026-10-07T17:30:00+05:30"}])
    assert not is_in_news_blackout(TIME, "EURUSD", [])


def test_naive_timestamp_fails_closed():
    assert is_in_news_blackout(TIME, "EURUSD", [
        {"currency": "USD", "impact": "high", "scheduled_at": TIME.replace(tzinfo=None)}])
    with pytest.raises(ValueError):
        is_in_news_blackout(TIME.replace(tzinfo=None), "EURUSD", [])
