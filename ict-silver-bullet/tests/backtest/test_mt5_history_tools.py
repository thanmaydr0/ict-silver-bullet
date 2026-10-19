from collections import namedtuple
from datetime import datetime, time, timezone
from types import SimpleNamespace
import time as time_module

import numpy as np
import pandas as pd
import pytest

from backtest import pull_calendar_probe as calendar_probe
from backtest import pull_history_m1 as history
from backtest import pull_ticks_probe as ticks_probe


RATE_DTYPE = [
    ("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
    ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8"),
]


def rate_rows(times):
    return np.array([(value, 1.0, 1.1, 0.9, 1.05, 10, 2, 0) for value in times], dtype=RATE_DTYPE)


def test_symbols_are_deduplicated_and_validated():
    assert history._parse_symbols("EURUSD, USDJPY,EURUSD") == ["EURUSD", "USDJPY"]
    try:
        history._parse_symbols("EURUSD,../secret")
    except ValueError as exc:
        assert "Invalid MT5 symbol" in str(exc)
    else:
        raise AssertionError("unsafe symbol must be rejected")


def test_all_mt5_tools_reject_unverified_or_live_accounts():
    for module in (history, ticks_probe, calendar_probe):
        module._require_demo_server({"server": "Broker-Demo"})
        with pytest.raises(RuntimeError, match="must identify a demo server"):
            module._require_demo_server({"server": "Broker-Live"})

        demo_terminal = SimpleNamespace(
            ACCOUNT_TRADE_MODE_DEMO=0,
            account_info=lambda: SimpleNamespace(trade_mode=0),
        )
        assert module._require_demo_account(demo_terminal) == "demo"

        live_terminal = SimpleNamespace(
            ACCOUNT_TRADE_MODE_DEMO=0,
            account_info=lambda: SimpleNamespace(trade_mode=2),
        )
        with pytest.raises(RuntimeError, match="not confirmed as demo"):
            module._require_demo_account(live_terminal)


def test_gap_count_includes_long_intervals_without_filling_bars():
    assert history._gap_stats([0, 60, 240, 300]) == {
        "gap_count": 1,
        "missing_m1_bars_between_observed_rows": 2,
        "largest_gap_seconds": 180,
    }


def test_position_chunks_walk_backward_and_keep_raw_columns():
    all_rates = rate_rows([0, 60, 120, 180, 240])
    calls = []

    class Terminal:
        TIMEFRAME_M1 = 1

        @staticmethod
        def copy_rates_from_pos(symbol, timeframe, start_pos, count):
            calls.append(start_pos)
            end = len(all_rates) - start_pos
            begin = max(0, end - count)
            return all_rates[begin:end]

        @staticmethod
        def last_error():
            return (0, "ok")

    frame = history._pull_available_rates(Terminal(), "EURUSD", 2)
    assert calls == [0, 2, 4]
    assert list(frame.columns) == list(history.RATE_COLUMNS)
    assert frame["time"].tolist() == [180, 240, 60, 120, 0]


def test_yearly_parquet_keeps_raw_time_and_hashes_files(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "M1_DIR", tmp_path / "m1")
    monkeypatch.setattr(history, "DATA_DIR", tmp_path)
    start = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
    frame = pd.DataFrame.from_records(rate_rows([start, start + 60, start + 60]))

    summary = history._write_symbol_bars("EURUSD", frame, data_dir=tmp_path)

    file_entry = summary["files"][0]
    path = tmp_path / file_entry["file"]
    stored = pd.read_parquet(path)
    assert path.name == "EURUSD_2024.parquet"
    assert stored["time"].tolist() == [start, start + 60]
    assert list(stored.columns) == list(history.RATE_COLUMNS)
    assert summary["duplicate_rows_removed"] == 1
    assert summary["row_count"] == 2
    assert file_entry["sha256"] == history._sha256(path)


def test_offset_observation_is_diagnostic_and_never_applied():
    raw_msc = int(time_module.time() * 1000) + 3_600_000
    terminal = SimpleNamespace(symbol_info_tick=lambda symbol: SimpleNamespace(time=raw_msc // 1000, time_msc=raw_msc))

    observation = history._observe_server_offset(terminal, "EURUSD")

    assert observation["status"] == "observed_from_fresh_tick"
    assert observation["estimated_offset_hours"] == 1.0
    assert observation["applied_to_saved_timestamps"] is False


def test_symbol_metadata_records_requested_fields_and_available_sessions():
    Info = namedtuple("Info", history.SYMBOL_FIELDS + ("session_deals",))
    info = Info(5, 0.00001, 1.0, 100000.0, 0.01, 0.01, -1.0, -1.0, 0, 0.0, "USD", 3)

    class Terminal:
        @staticmethod
        def symbol_info(symbol):
            return info

        @staticmethod
        def symbol_info_session_trade(symbol, day, session_index):
            if day == 1 and session_index == 0:
                return (time(9, 0), time(17, 0))
            return None

    result = history._symbol_metadata(Terminal(), "EURUSD")
    assert result["fields"]["currency_profit"] == "USD"
    assert result["symbol_info_session_fields"] == {"session_deals": 3}
    assert result["trade_sessions"]["api_available"] is True
    assert result["trade_sessions"]["windows"][0]["day_name"] == "Monday"


def test_ticks_probe_uses_small_annual_windows_and_discards_tick_rows():
    calls = []
    tick_dtype = [("time", "i8"), ("time_msc", "i8")]

    class Terminal:
        COPY_TICKS_ALL = 7

        @staticmethod
        def symbol_select(symbol, enabled):
            return True

        @staticmethod
        def copy_ticks_range(symbol, start, end, flag):
            calls.append((start, end))
            assert end - start == pd.Timedelta(hours=1)
            if len(calls) == 1:
                return np.array([(int(start.timestamp()), int(start.timestamp() * 1000))], dtype=tick_dtype)
            return np.empty(0, dtype=tick_dtype)

        @staticmethod
        def last_error():
            return (0, "ok")

    anchor = datetime(2026, 10, 14, tzinfo=timezone.utc).date()
    result = ticks_probe._probe_symbol(Terminal(), "EURUSD", anchor, 4, 1)

    assert len(calls) == 3
    assert result["deepest_successful_sample_lookback_years"] == 0
    assert result["total_requested_sample_hours"] == 3
    assert all(sample.get("raw_tick_timestamps_saved") is False for sample in result["samples"] if sample["tick_count"])


def test_tick_probe_errors_do_not_look_like_history_ends():
    calls = []
    tick_dtype = [("time", "i8"), ("time_msc", "i8")]

    class Terminal:
        COPY_TICKS_ALL = 7

        @staticmethod
        def symbol_select(symbol, enabled):
            return True

        @staticmethod
        def copy_ticks_range(symbol, start, end, flag):
            calls.append(start)
            if len(calls) == 1:
                return np.array([(int(start.timestamp()), int(start.timestamp() * 1000))], dtype=tick_dtype)
            if len(calls) == 2:
                return None
            if len(calls) == 3:
                return np.empty(0, dtype=tick_dtype)
            if len(calls) == 4:
                return np.array([(int(start.timestamp()), int(start.timestamp() * 1000))], dtype=tick_dtype)
            return np.empty(0, dtype=tick_dtype)

        @staticmethod
        def last_error():
            return (1, "temporary error")

    anchor = datetime(2026, 10, 14, tzinfo=timezone.utc).date()
    result = ticks_probe._probe_symbol(Terminal(), "EURUSD", anchor, 4, 1)

    assert len(calls) == 5
    assert result["deepest_successful_sample_lookback_years"] == 3


def test_calendar_probe_reports_missing_python_api_without_calendar_calls():
    result = calendar_probe.probe_calendar(SimpleNamespace(last_error=lambda: (0, "ok")), 5)
    assert result["status"] == "calendar_api_unavailable"
    assert result["calendar_functions_found"] == []
    assert result["historical_high_impact_events_confirmed"] is False


def test_calendar_probe_finds_old_high_impact_values_and_writes_csv(tmp_path, monkeypatch):
    target_year = datetime.now(timezone.utc).year - 2

    class Terminal:
        @staticmethod
        def calendar_value_history(start, end):
            if start.year == target_year:
                return [{
                    "event_id": 7,
                    "time": int(datetime(target_year, 6, 1, tzinfo=timezone.utc).timestamp()),
                    "importance": None,
                }]
            return []

        @staticmethod
        def calendar_event_by_id(event_id):
            return {"id": event_id, "name": "CPI", "currency": "USD", "importance": 3}

    csv_path = tmp_path / "calendar_events.csv"
    monkeypatch.setattr(calendar_probe, "CSV_PATH", csv_path)
    result = calendar_probe.probe_calendar(Terminal(), 5)

    assert result["status"] == "history_returned"
    assert result["deepest_year_with_values"] == target_year
    assert result["high_impact_event_rows"] == 1
    assert csv_path.exists()
    assert "CPI" in csv_path.read_text(encoding="utf-8")
