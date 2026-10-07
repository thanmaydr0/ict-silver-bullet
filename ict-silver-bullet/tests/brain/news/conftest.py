"""No test in this phase may contact an external service."""
import pytest
import requests
from brain.news import forexfactory


@pytest.fixture(autouse=True)
def block_external_calls(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Unmocked external call")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(forexfactory, "insert_news_events", blocked)
