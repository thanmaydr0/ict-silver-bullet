"""All database calls use fluent mocks; no real client or network is used."""

from datetime import datetime
from decimal import Decimal
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, call
from zoneinfo import ZoneInfo

import pytest

from executor import db_client as db

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
    ("insert_trade", ({"signal_id": 7, "opened_at": SINCE},), "trades",
     [call.insert({"signal_id": 7, "opened_at": ISO}), call.execute()], int),
    ("update_trade", (7, {"closed_at": SINCE, "status": "closed"}), "trades",
     [call.update({"closed_at": ISO, "status": "closed"}), call.eq("id", 7), call.execute()], None),
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


# Feeder and history verification uses only injected terminal mocks.
from datetime import timedelta
from executor import mt5_bridge as bridge
from backtest import pull_history as history


@pytest.fixture
def terminal(monkeypatch):
    terminal = MagicMock()
    terminal.TIMEFRAME_M1 = 1
    config = SimpleNamespace(MT5_LOGIN=123, MT5_PASSWORD="mock", MT5_SERVER="mock",
                             MT5_TERMINAL_PATH=None, BROKER_UTC_OFFSET_HOURS=2,
                             TRADED_PAIRS=["EURUSD", "GBPUSD"])
    monkeypatch.setattr(bridge, "mt5", terminal)
    monkeypatch.setattr(bridge, "load_config", lambda: config)
    monkeypatch.setattr(bridge, "_offsets", {})
    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return SINCE
    monkeypatch.setattr(bridge, "datetime", ClockDatetime)
    return terminal, config


@pytest.mark.parametrize("path", [None, "C:/MT5/terminal64.exe"])
def test_mt5_connect(terminal, path):
    mt5, config = terminal
    config.MT5_TERMINAL_PATH = path
    bridge.connect()
    if path:
        mt5.initialize.assert_called_once_with(path=path, login=123, password="mock", server="mock")
    else:
        mt5.initialize.assert_called_once_with(login=123, password="mock", server="mock")


def test_mt5_failed_connect(terminal):
    terminal[0].initialize.return_value = False
    terminal[0].last_error.return_value = (-1, "failed")
    with pytest.raises(RuntimeError, match="MT5 initialization failed"):
        bridge.connect()


def test_mt5_offset_cache_and_refresh(terminal, monkeypatch):
    mt5, _ = terminal
    clock = [0.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    mt5.symbol_info_tick.return_value = SimpleNamespace(time=SINCE.timestamp() + 7200 + 20)
    assert bridge.get_broker_utc_offset("EURUSD") == timedelta(hours=2)
    mt5.symbol_info_tick.return_value = SimpleNamespace(time=SINCE.timestamp() + 10800)
    clock[0] = 3599
    assert bridge.get_broker_utc_offset("EURUSD") == timedelta(hours=2)
    mt5.symbol_info_tick.assert_called_once()
    clock[0] = 3600
    assert bridge.get_broker_utc_offset("EURUSD") == timedelta(hours=3)


@pytest.mark.parametrize("tick", [None, SimpleNamespace(time=0), SimpleNamespace(time=SINCE.timestamp() + 7200 - 300)])
def test_mt5_unreliable_tick_fallback(terminal, tick):
    terminal[0].symbol_info_tick.return_value = tick
    assert bridge.get_broker_utc_offset("EURUSD") == timedelta(hours=2)


def test_mt5_weekend_fallback(terminal, monkeypatch):
    class WeekendDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 3, tzinfo=UTC)
    monkeypatch.setattr(bridge, "datetime", WeekendDatetime)
    terminal[0].symbol_info_tick.return_value = SimpleNamespace(time=datetime(2026, 10, 3, tzinfo=UTC).timestamp())
    assert bridge.get_broker_utc_offset("EURUSD") == timedelta(hours=2)


def test_mt5_tick_failure_fallback(terminal):
    terminal[0].symbol_info_tick.side_effect = RuntimeError()
    assert bridge.get_broker_utc_offset("EURUSD") == timedelta(hours=2)


def _bar(ts, close=1.2):
    return dict(time=int(ts.timestamp()), open=1.0, high=1.3, low=0.9, close=close)


def test_mt5_pull_and_upsert(terminal, monkeypatch):
    mt5, _ = terminal
    monkeypatch.setattr(bridge, "get_broker_utc_offset", lambda pair: timedelta(hours=2))
    mt5.copy_rates_from_pos.return_value = [_bar(SINCE + timedelta(hours=2))]
    rows = bridge.pull_recent_candles("EURUSD", "M1", 3)
    mt5.copy_rates_from_pos.assert_called_once_with("EURUSD", 1, 0, 3)
    assert rows == [dict(pair="EURUSD", timeframe="M1", open=1.0, high=1.3, low=0.9, close=1.2, ts=SINCE)]
    insert = MagicMock()
    monkeypatch.setattr(bridge, "insert_candles", insert)
    bridge.push_candles_to_supabase("EURUSD", "M1", 3)
    insert.assert_called_once_with(rows)


def test_mt5_failed_pull(terminal, monkeypatch):
    monkeypatch.setattr(bridge, "get_broker_utc_offset", lambda pair: timedelta(hours=2))
    terminal[0].copy_rates_from_pos.return_value = None
    with pytest.raises(RuntimeError, match="candle read failed"):
        bridge.pull_recent_candles("EURUSD", "M1", 3)


def test_mt5_loop_continues_after_pair_failure(terminal, monkeypatch):
    push = MagicMock(side_effect=[RuntimeError(), None])
    monkeypatch.setattr(bridge, "push_candles_to_supabase", push)
    monkeypatch.setattr(bridge.time, "sleep", MagicMock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        bridge.run_loop(["EURUSD", "GBPUSD"], "M1")
    assert push.call_args_list == [call("EURUSD", "M1", 3), call("GBPUSD", "M1", 3)]


def test_mt5_loop_reconnects(terminal, monkeypatch):
    terminal[0].terminal_info.return_value = SimpleNamespace(connected=False)
    connect = MagicMock()
    monkeypatch.setattr(bridge, "connect", connect)
    monkeypatch.setattr(bridge, "push_candles_to_supabase", MagicMock())
    monkeypatch.setattr(bridge.time, "sleep", MagicMock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        bridge.run_loop(["EURUSD"], "M1")
    terminal[0].shutdown.assert_called_once()
    connect.assert_called_once()


def test_mt5_main_retries_then_shutdown(terminal, monkeypatch):
    monkeypatch.setattr(bridge, "setup_logging", MagicMock())
    connect = MagicMock(side_effect=[RuntimeError(), None])
    monkeypatch.setattr(bridge, "connect", connect)
    monkeypatch.setattr(bridge.time, "sleep", MagicMock())
    loop = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(bridge, "run_loop", loop)
    bridge.main()
    assert connect.call_count == 2
    loop.assert_called_once_with(["EURUSD", "GBPUSD"], "M1")
    terminal[0].shutdown.assert_called_once()


def test_history_month_chunks_and_duplicates(terminal):
    mt5, _ = terminal
    start = datetime(2026, 1, 15, tzinfo=UTC)
    boundary = datetime(2026, 2, 1, tzinfo=UTC)
    end = datetime(2026, 2, 3, tzinfo=UTC)
    offset = timedelta(hours=2)
    mt5.copy_rates_range.side_effect = [
        [_bar(start + offset), _bar(boundary + offset)],
        [_bar(boundary + offset), _bar(boundary + offset, 1.25), _bar(end + offset)],
    ]
    rows = history._pull_pair(mt5, "EURUSD", "M1", start, end, offset)
    assert [row["ts"] for row in rows] == [start, boundary]
    assert rows[-1]["close"] == 1.25
    assert mt5.copy_rates_range.call_args_list == [
        call("EURUSD", 1, start + offset, boundary + offset),
        call("EURUSD", 1, boundary + offset, end + offset),
    ]


def test_history_failed_chunk_does_not_succeed(terminal):
    terminal[0].copy_rates_range.return_value = None
    with pytest.raises(RuntimeError, match="history failed"):
        history._pull_pair(terminal[0], "EURUSD", "M1", SINCE, SINCE + timedelta(days=1), timedelta())


def test_history_calendar_boundaries():
    assert history._month_end(datetime(2025, 12, 15, tzinfo=UTC)) == datetime(2026, 1, 1, tzinfo=UTC)
    assert history._years_ago(datetime(2024, 2, 29, tzinfo=UTC), 1) == datetime(2023, 2, 28, tzinfo=UTC)


def test_history_main_parquet_export(terminal, monkeypatch, tmp_path):
    mt5, _ = terminal
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt5)
    monkeypatch.setattr(sys, "argv", ["pull_history", "--pairs", "EURUSD", "--years", "3"])
    monkeypatch.setattr(history, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bridge, "connect", MagicMock())
    monkeypatch.setattr(bridge, "get_broker_utc_offset", lambda pair: timedelta(hours=2))
    monkeypatch.setattr(history, "_pull_pair", lambda *args: [dict(pair="EURUSD", timeframe="M1", ts=SINCE, open=1.0, high=1.3, low=0.9, close=1.2)])
    import executor.logging_setup
    monkeypatch.setattr(executor.logging_setup, "setup_logging", MagicMock())
    frame = MagicMock()
    frame.__getitem__.return_value = [SINCE]
    def save(path, index):
        path.write_bytes(b"mock-parquet")
    frame.to_parquet.side_effect = save
    pandas = SimpleNamespace(DataFrame=MagicMock(return_value=frame), to_datetime=MagicMock(return_value=[SINCE]))
    monkeypatch.setitem(sys.modules, "pandas", pandas)
    history.main()
    assert (tmp_path / "EURUSD_M1.parquet").read_bytes() == b"mock-parquet"
    assert not list(tmp_path.glob("*.tmp"))
    pandas.to_datetime.assert_called_once_with([SINCE], utc=True)
    mt5.shutdown.assert_called_once()
