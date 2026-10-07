from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from brain.news import apis

CLIENTS = [apis.FredClient, apis.FinnhubClient, apis.MarketauxClient,
           apis.AlphaVantageClient, apis.NewsDataClient]


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(apis, "load_config", lambda: SimpleNamespace(**{c.key_name: "test-key" for c in CLIENTS}))


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("failure", ["network", "rate_limit", "json"])
def test_failure(client, failure, keys, monkeypatch):
    response = Mock()
    if failure == "network":
        call = Mock(side_effect=RuntimeError("offline"))
    else:
        if failure == "rate_limit":
            response.raise_for_status.side_effect = apis.requests.HTTPError("429")
        else:
            response.json.side_effect = ValueError("malformed")
        call = Mock(return_value=response)
    monkeypatch.setattr(apis.requests, "get", call)
    assert client().fetch_headlines() == []
    assert call.call_args.kwargs["timeout"] == 20


@pytest.mark.parametrize("client", CLIENTS)
def test_no_key(client, monkeypatch):
    monkeypatch.setattr(apis, "load_config", lambda: SimpleNamespace())
    call = Mock(side_effect=AssertionError("disabled client contacted network"))
    monkeypatch.setattr(apis.requests, "get", call)
    assert client().fetch_headlines() == []
    call.assert_not_called()


@pytest.mark.parametrize("client,payload", [
    (apis.FredClient, {"release_dates": [{"release_name": "CPI release", "date": "2026-10-07", "release_id": 10}]}),
    (apis.FinnhubClient, [{"headline": "CPI release", "datetime": 1791374400, "source": "wire", "url": "https://example.com/a"}]),
    (apis.MarketauxClient, {"data": [{"title": "CPI release", "published_at": "2026-10-07T12:00:00Z", "url": "https://example.com/a"}]}),
    (apis.AlphaVantageClient, {"feed": [{"title": "CPI release", "time_published": "20261007T120000", "url": "https://example.com/a"}]}),
    (apis.NewsDataClient, {"status": "success", "results": [{"title": "CPI release", "pubDate": "2026-10-07 12:00:00", "link": "https://example.com/a"}]}),
])
def test_normalization(client, payload, keys, monkeypatch):
    monkeypatch.setattr(apis.requests, "get", Mock(return_value=Mock(json=Mock(return_value=payload))))
    rows = client().fetch_headlines("CPI")
    assert len(rows) == 1
    assert set(rows[0]) == {"title", "source", "published_at", "url"}
    assert rows[0]["published_at"].utcoffset().total_seconds() == 0


def test_aggregate_survives_failure_and_deduplicates():
    row = {"title": "CPI release", "url": "https://example.com/a"}
    clients = [Mock(fetch_headlines=Mock(side_effect=RuntimeError())),
               Mock(fetch_headlines=Mock(return_value=[row])),
               Mock(fetch_headlines=Mock(return_value=[{**row, "title": "Another title"},
                   {**row, "title": " CPI RELEASE ", "url": "https://other.com"},
                   {"title": "New headline", "url": ""}]))]
    assert apis.fetch_all_headlines(clients) == [row, {"title": "New headline", "url": ""}]


@pytest.mark.parametrize("payload", [{"Information": "rate limit"}, {"error": "bad key"}])
def test_provider_errors(keys, monkeypatch, payload):
    monkeypatch.setattr(apis.requests, "get", Mock(return_value=Mock(json=Mock(return_value=payload))))
    assert apis.AlphaVantageClient().fetch_headlines() == []
