from types import SimpleNamespace
from unittest.mock import Mock, call
import pytest
from brain.news import forexfactory as ff

EVENT = {"title": "CPI", "currency": "USD", "impact": "high",
         "scheduled_at": "2026-10-07T17:30:00+05:30", "forecast": "2%", "previous": "3%"}


@pytest.fixture(autouse=True)
def reset_calendar(monkeypatch):
    monkeypatch.setattr(ff, "_cached", [])
    monkeypatch.setattr(ff, "_last_success", None)
    monkeypatch.setattr(ff, "_healthy", False)
    monkeypatch.setattr(ff, "load_config", lambda: SimpleNamespace(FIRECRAWL_API_KEY="fake"))
    monkeypatch.setattr(ff.time, "sleep", Mock())
    monkeypatch.setattr(ff, "insert_news_events", Mock())


def response(events):
    return Mock(json=Mock(return_value={"success": True, "data": {
        "metadata": {"statusCode": 200}, "json": {"events": events}}}))


def test_parse_persist_and_cache(monkeypatch):
    post = Mock(return_value=response([EVENT]))
    monkeypatch.setattr(ff.requests, "post", post)
    rows = ff.fetch_calendar()
    assert rows[0]["scheduled_at"].hour == 12
    assert rows[0]["scheduled_at"].utcoffset().total_seconds() == 0
    ff.insert_news_events.assert_called_once_with(rows)
    assert ff.calendar_is_healthy()
    rows[0]["title"] = "mutated"
    post.side_effect = RuntimeError("offline")
    assert ff.fetch_calendar()[0]["title"] == "CPI"
    assert not ff.calendar_is_healthy()
    assert post.call_count == 4
    assert ff.time.sleep.call_args_list == [call(1), call(2)]


@pytest.mark.parametrize("events", [[], [{**EVENT, "scheduled_at": "2026-10-07T12:00:00"}],
                                      [EVENT, {**EVENT, "impact": "unknown"}]])
def test_bad_extract_never_clears_cache(monkeypatch, events):
    monkeypatch.setattr(ff, "_cached", [EVENT])
    monkeypatch.setattr(ff.requests, "post", Mock(return_value=response(events)))
    assert ff.fetch_calendar() == [EVENT]
    assert not ff.calendar_is_healthy()
    ff.insert_news_events.assert_not_called()


def test_cold_start_failure(monkeypatch):
    post = Mock(side_effect=RuntimeError())
    monkeypatch.setattr(ff.requests, "post", post)
    assert ff.fetch_calendar() == []
    assert post.call_count == 3
    assert not ff.calendar_is_healthy()


def test_db_failure_retains_new_data(monkeypatch):
    monkeypatch.setattr(ff.requests, "post", Mock(return_value=response([EVENT])))
    ff.insert_news_events.side_effect = RuntimeError("DB unavailable")
    assert ff.fetch_calendar()[0]["title"] == "CPI"
    assert not ff.calendar_is_healthy()


def test_stale_health(monkeypatch):
    from datetime import datetime, timedelta
    monkeypatch.setattr(ff, "_healthy", True)
    monkeypatch.setattr(ff, "_last_success", datetime.now(ff.UTC) - timedelta(hours=2))
    assert not ff.calendar_is_healthy()


def test_retry_recovers(monkeypatch):
    post = Mock(side_effect=[RuntimeError("temporary"), response([EVENT])])
    monkeypatch.setattr(ff.requests, "post", post)
    assert ff.fetch_calendar()[0]["title"] == "CPI"
    assert post.call_count == 2
    ff.time.sleep.assert_called_once_with(1)
    assert ff.calendar_is_healthy()


def test_blocked_target_page_is_failure(monkeypatch):
    blocked = response([EVENT])
    blocked.json.return_value["data"]["metadata"]["statusCode"] = 403
    monkeypatch.setattr(ff.requests, "post", Mock(return_value=blocked))
    assert ff.fetch_calendar() == []
    assert not ff.calendar_is_healthy()
    ff.insert_news_events.assert_not_called()
