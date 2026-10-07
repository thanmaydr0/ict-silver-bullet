"""All database calls use fluent mocks; no real client or network is used."""

from datetime import datetime
from decimal import Decimal
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, call
from zoneinfo import ZoneInfo

import pytest

from brain.db import supabase_client as db

UTC = ZoneInfo("UTC")
SINCE = datetime(2026, 10, 1, tzinfo=UTC)
ISO = SINCE.isoformat()


@pytest.fixture
def query(monkeypatch):
    client = MagicMock()
    builder = MagicMock()
    for method in ("select", "insert", "upsert", "update", "eq", "gte", "lte", "order", "limit", "range", "is_"):
        getattr(builder, method).return_value = builder
    builder.not_ = builder
    builder.execute.return_value = SimpleNamespace(data=[{"id": 7}])
    client.table.return_value = builder
    monkeypatch.setattr(db, "_client", client)
    return client, builder


CASES = [
    ("insert_candles", ([{"pair": "EURUSD", "timeframe": "M1", "ts": SINCE}],), "candles",
     [call.upsert([{"pair": "EURUSD", "timeframe": "M1", "ts": ISO}], on_conflict="pair,timeframe,ts"), call.execute()], None),
    ("get_pending_signals", (), "signals",
     [call.select("*"), call.eq("status", "pending"), call.order("detected_at"), call.order("id"), call.range(0, 999), call.execute()], list),
    ("update_signal_status", (7, "executed"), "signals",
     [call.update({"status": "executed"}), call.eq("id", 7), call.execute()], None),
    ("insert_equity_snapshot", ({"equity": Decimal("100.25"), "ts": SINCE},), "equity_snapshots",
     [call.insert({"equity": "100.25", "ts": ISO}), call.execute()], None),
    ("get_latest_equity_snapshot", (), "equity_snapshots",
     [call.select("*"), call.order("ts", desc=True), call.order("id", desc=True), call.limit(1), call.execute()], dict),
    ("get_latest_candles", ("EURUSD", "M1", 5), "candles",
     [call.select("*"), call.eq("pair", "EURUSD"), call.eq("timeframe", "M1"), call.order("ts", desc=True), call.limit(5), call.execute()], list),
    ("insert_signal", ({"pair": "EURUSD", "lot_size": 0.2},), "signals",
     [call.insert({"pair": "EURUSD", "lot_size": 0.2}), call.execute()], int),
    ("get_latest_signal", (), "signals",
     [call.select("*"), call.order("detected_at", desc=True), call.order("id", desc=True), call.limit(1), call.execute()], dict),
    ("get_equity_snapshots", (SINCE,), "equity_snapshots",
     [call.select("*"), call.gte("ts", ISO), call.order("ts"), call.order("id"), call.range(0, 999), call.execute()], list),
    ("get_max_equity", (), "equity_snapshots",
     [call.select("equity"), call.is_("equity", "null"), call.order("equity", desc=True), call.limit(1), call.execute()], float),
    ("get_trades", (5,), "trades",
     [call.select(db._TRADE_SELECT), call.order("opened_at", desc=True), call.order("id", desc=True), call.limit(5), call.execute()], list),
    ("get_trades_since", (SINCE,), "trades",
     [call.select(db._TRADE_SELECT), call.gte("opened_at", ISO), call.order("opened_at"), call.order("id"), call.range(0, 999), call.execute()], list),
    ("get_open_trades", (), "trades",
     [call.select(db._TRADE_SELECT), call.eq("status", "open"), call.order("opened_at"), call.order("id"), call.range(0, 999), call.execute()], list),
    ("insert_news_events", ([{"title": "CPI", "scheduled_at": SINCE}],), "news_events",
     [call.insert([{"title": "CPI", "scheduled_at": ISO}]), call.execute()], None),
    ("get_upcoming_news", (30,), "news_events",
     [call.select("*"), call.gte("scheduled_at", ISO), call.lte("scheduled_at", "2026-10-01T00:30:00+00:00"), call.order("scheduled_at"), call.order("id"), call.range(0, 999), call.execute()], list),
]


@pytest.mark.parametrize("name,args,table,expected,result_type", CASES, ids=[item[0] for item in CASES])
def test_wrapper_query(query, name, args, table, expected, result_type):
    client, builder = query
    builder.execute.return_value.data = [{"id": 7, "equity": "120.5"}]
    result = getattr(db, name)(*args)
    client.table.assert_called_once_with(table)
    assert builder.mock_calls == expected
    if result_type is None:
        assert result is None
    elif result_type is int:
        assert result == 7
    elif result_type is float:
        assert result == 120.5
    else:
        assert isinstance(result, result_type)


@pytest.mark.parametrize("name,args,table,expected,result_type", CASES, ids=[item[0] for item in CASES])
@pytest.mark.parametrize("failure_point", ["client", "execute"])
def test_wrapper_failure_is_logged(query, monkeypatch, caplog, name, args, table, expected, result_type, failure_point):
    client, builder = query
    if failure_point == "client":
        monkeypatch.setattr(db, "_get_client", MagicMock(side_effect=ConnectionError("private-key")))
    else:
        builder.execute.side_effect = ConnectionError("private-key")
    result = getattr(db, name)(*args)
    assert result == ([] if result_type is list else None)
    assert name in caplog.text
    assert "ConnectionError" in caplog.text
    assert "private-key" not in caplog.text


def test_lazy_client_creation(monkeypatch):
    factory = MagicMock()
    config = SimpleNamespace(SUPABASE_URL="https://example.invalid", SUPABASE_SERVICE_KEY="mock-key")
    monkeypatch.setitem(sys.modules, "supabase", SimpleNamespace(create_client=factory))
    monkeypatch.setattr(db, "_client", None)
    loader = MagicMock(return_value=config)
    monkeypatch.setattr(db, "load_config", loader)
    assert db._get_client() is db._get_client()
    loader.assert_called_once_with()
    factory.assert_called_once_with(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)


def test_lazy_creation_retries_after_failure(monkeypatch, caplog):
    factory = MagicMock(side_effect=[ConnectionError(), MagicMock()])
    monkeypatch.setitem(sys.modules, "supabase", SimpleNamespace(create_client=factory))
    monkeypatch.setattr(db, "_client", None)
    monkeypatch.setattr(db, "load_config", lambda: SimpleNamespace(SUPABASE_URL="mock", SUPABASE_SERVICE_KEY="mock"))
    assert db.get_latest_equity_snapshot() is None
    assert db._client is None
    db._get_client()
    assert factory.call_count == 2


def test_empty_writes_skip_network(query):
    db.insert_candles([])
    query[0].table.assert_not_called()


def test_candle_duplicates_and_utc_conversion(query):
    local = datetime(2026, 10, 1, 5, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    rows = [{"pair": "EURUSD", "timeframe": "M1", "ts": SINCE, "close": 1},
            {"pair": "EURUSD", "timeframe": "M1", "ts": local, "close": 2}]
    db.insert_candles(rows)
    query[1].upsert.assert_called_once_with(
        [{"pair": "EURUSD", "timeframe": "M1", "ts": ISO, "close": 2}], on_conflict="pair,timeframe,ts")
    assert rows[0]["ts"] is SINCE


def test_naive_timestamp_rejected_without_network(query, caplog):
    db.insert_equity_snapshot({"ts": datetime(2026, 10, 1)})
    query[1].execute.assert_not_called()
    assert "ValueError" in caplog.text


def test_latest_empty_response(query):
    query[1].execute.return_value.data = []
    assert db.get_latest_equity_snapshot() is None


def test_pagination(query):
    query[1].execute.side_effect = [SimpleNamespace(data=[{"id": 1}] * 1000), SimpleNamespace(data=[{"id": 2}])]
    assert len(db.get_pending_signals()) == 1001
    assert query[1].range.call_args_list == [call(0, 999), call(1000, 1999)]


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(db, "_now", lambda: SINCE)


@pytest.mark.parametrize("name,args", [("get_trades", (5,)), ("get_trades_since", (SINCE,)), ("get_open_trades", ())])
def test_trade_signal_fields_are_flattened(query, name, args):
    signal = dict(pair="EURUSD", direction="long", confluence_score=5, entry=1.1, stop_loss=1.0, take_profit=1.3)
    row = {"id": 7, "signals": signal}
    query[1].execute.return_value.data = [row]
    assert getattr(db, name)(*args) == [{"id": 7, **signal}]
    assert row["signals"] is signal
    query[1].execute.return_value.data = [{"id": 8, "signals": None}]
    assert getattr(db, name)(*args)[0]["pair"] is None


def test_latest_candles_chronological(query):
    query[1].execute.return_value.data = [{"ts": 3}, {"ts": 2}, {"ts": 1}]
    assert db.get_latest_candles("EURUSD", "M1", 3) == [{"ts": 1}, {"ts": 2}, {"ts": 3}]


@pytest.mark.parametrize("name,args", [("get_latest_candles", ("EURUSD", "M1", 0)), ("get_trades", (0,)), ("get_upcoming_news", (-1,))])
def test_invalid_window_skips_network(query, name, args):
    assert getattr(db, name)(*args) == []
    query[0].table.assert_not_called()


@pytest.mark.parametrize("name,args", [("get_max_equity", ()), ("get_latest_signal", ()), ("insert_signal", ({"pair": "EURUSD"},))])
def test_empty_scalar(query, name, args):
    query[1].execute.return_value.data = None
    assert getattr(db, name)(*args) is None


def test_empty_news_write(query):
    db.insert_news_events([])
    query[0].table.assert_not_called()
