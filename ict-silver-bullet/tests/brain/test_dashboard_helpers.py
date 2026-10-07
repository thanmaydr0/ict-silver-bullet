"""Dashboard checks: all DB/config reads are mocked; no service credentials needed."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from brain import dashboard as d

NOW = datetime(2026, 10, 7, 14, 5, tzinfo=ZoneInfo("UTC"))


def test_empty_figures_and_tables():
    for fig in (d.build_equity_figure([]), d.build_drawdown_gauge(None), d.build_setup_figure(None, [])):
        assert "No data yet" in fig.layout.annotations[0].text
    assert len(d.trade_rows([])[0]) == len(d.TRADE_HEADERS)
    assert len(d.position_rows([])[0]) == len(d.POSITION_HEADERS)


def test_equity_limits_recover_persisted_baselines_and_sort():
    rows = [{"ts": "2026-10-07T14:00:00Z", "equity": "9700", "daily_dd_pct": ".03", "overall_dd_pct": ".03"},
            {"ts": "2026-10-06T14:00:00Z", "equity": "10000", "daily_dd_pct": "0", "overall_dd_pct": "0"}]
    fig = d.build_equity_figure(rows)
    assert list(fig.data[0].y) == [10000, 9700]
    assert list(fig.data[1].y) == pytest.approx([9600, 9600])
    assert list(fig.data[2].y) == pytest.approx([9400, 9400])


def test_equity_missing_dd_is_not_fabricated():
    fig = d.build_equity_figure([{"ts": NOW.isoformat(), "equity": 10000}])
    assert fig.data[1].y[0] is None
    assert fig.data[2].y[0] is None


@pytest.mark.parametrize("daily,soft,hard", [(True, d.SOFT_DAILY_DD_PCT, d.HARD_DAILY_DD_PCT),
                                            (False, d.SOFT_OVERALL_DD_PCT, d.HARD_OVERALL_DD_PCT)])
def test_gauge_uses_fractional_thresholds(daily, soft, hard):
    trace = d.build_drawdown_gauge(.07, daily).data[0]
    assert trace.value == pytest.approx(7)
    assert trace.gauge.threshold.value == pytest.approx(hard*100)
    assert soft*100 in trace.gauge.axis.tickvals
    assert trace.gauge.axis.range[1] > 7


def test_trade_rr_uses_actual_fill_and_keeps_zero_realized_r():
    rows = d.trade_rows([{"entry_fill": "1.11", "entry": "1.10", "stop_loss": "1.09",
                         "take_profit": "1.15", "realized_r": "0", "opened_at": NOW.isoformat()}])
    assert rows[0][3] == 1.11
    assert rows[0][7] == 2
    assert rows[0][9] == 0
    assert d.trade_rows([{"entry": 1, "stop_loss": 1, "take_profit": 2}])[0][7] is None


def test_floating_pnl_never_uses_realized():
    assert "Unavailable" in d.position_rows([{"realized_usd": 42}])[0][-1]
    assert d.position_rows([{"floating_pnl": 0}])[0][-1] == 0


def test_blackout_includes_recent_event_and_currency_scope():
    past = {"title": "CPI", "currency": "USD", "impact": "high", "scheduled_at": "2026-10-07T14:00:00Z"}
    text = d.news_text([], [past], ["EURUSD", "EURGBP"], NOW)
    assert "EURUSD: blocked" in text
    assert "EURGBP: armed / clear" in text
    assert "unknown" in d.news_text([], None, ["EURUSD"], NOW)


def test_news_sorts_high_impact_and_reports_minutes():
    events = [{"title": "Later", "impact": "high", "scheduled_at": "2026-10-07T15:00:00Z"},
              {"title": "Next", "impact": "high", "scheduled_at": "2026-10-07T14:10:00Z"}]
    assert "Next () — 5.0 minutes" in d.news_text(events, [], [], NOW)


def test_killzone_armed_and_idle():
    assert "Armed — ny_am" in d.killzone_text(NOW)
    assert "Idle" in d.killzone_text(NOW.replace(hour=12))


def test_setup_annotates_recorded_evidence_and_excludes_future():
    candles = [{"ts": "2026-10-07T14:00:00Z", "open": 1, "high": 3, "low": .5, "close": 2},
               {"ts": "2026-10-07T14:10:00Z", "open": 2, "high": 3, "low": 1, "close": 2}]
    signal = {"id": 9, "pair": "EURUSD", "detected_at": NOW.isoformat(), "setup": {
        "candles": candles, "fvg": {"bottom": 1, "top": 2}, "ob": {"low": .5, "high": 1},
        "mss": {"level": 2}, "sweep": {"wick_extreme": .5}}}
    fig = d.build_setup_figure(signal, [])
    assert len(fig.data[0].x) == 1
    assert len(fig.layout.shapes) == 4
    assert not any("not stored" in a.text for a in fig.layout.annotations)


def test_setup_missing_evidence_is_explicit():
    fig = d.build_setup_figure({"pair": "EURUSD", "detected_at": NOW.isoformat()}, [
        {"ts": NOW.isoformat(), "open": 1, "high": 2, "low": .5, "close": 1.5}])
    assert "Evidence not stored" in fig.layout.annotations[0].text


def test_refresh_empty_and_failed_reads_survive(monkeypatch):
    d.equity_history.cache_clear()
    for name in ("get_equity_snapshots", "get_trades", "get_open_trades", "get_upcoming_news"):
        monkeypatch.setattr(d.db, name, Mock(return_value=[]))
    for name in ("get_latest_equity_snapshot", "get_latest_signal"):
        monkeypatch.setattr(d.db, name, Mock(return_value=None))
    monkeypatch.setattr(d, "recent_news", Mock(return_value=None))
    monkeypatch.setattr(d, "load_config", Mock(return_value=SimpleNamespace(TRADED_PAIRS=["EURUSD"])))
    result = d.refresh()
    assert len(result) == 9
    assert "No data yet" in result[0]
    assert "unknown" in result[7]
    monkeypatch.setattr(d.db, "get_trades", Mock(side_effect=RuntimeError("offline")))
    assert d.refresh()[5][0][0] == "No data yet"


def test_recent_news_queries_past_fifteen_minutes(monkeypatch):
    query = Mock()
    for name in ("table", "select", "gte", "lte", "order"):
        getattr(query, name).return_value = query
    monkeypatch.setattr(d.db, "_get_client", Mock(return_value=query))
    monkeypatch.setattr(d.db, "_all_rows", Mock(return_value=[]))
    assert d.recent_news(NOW) == []
    query.gte.assert_called_once_with("scheduled_at", "2026-10-07T13:50:00+00:00")
    query.lte.assert_called_once_with("scheduled_at", "2026-10-07T14:20:00+00:00")
    monkeypatch.setattr(d.db, "_get_client", Mock(side_effect=RuntimeError("offline")))
    assert d.recent_news(NOW) is None


def test_main_refuses_missing_auth_before_building(monkeypatch):
    monkeypatch.setattr(d, "setup_logging", Mock())
    monkeypatch.setattr(d, "load_dashboard_config", Mock(side_effect=RuntimeError("missing credentials")))
    build = Mock()
    monkeypatch.setattr(d, "create_dashboard", build)
    with pytest.raises(RuntimeError, match="missing credentials"):
        d.main()
    build.assert_not_called()


def test_main_launches_with_builtin_auth_and_closes(monkeypatch):
    monkeypatch.setattr(d, "setup_logging", Mock())
    monkeypatch.setattr(d, "load_dashboard_config", Mock(return_value=SimpleNamespace(
        DASHBOARD_HOST="127.0.0.1", DASHBOARD_PORT=7860, DASHBOARD_USERNAME="test", DASHBOARD_PASSWORD="dummy")))
    app = Mock()
    monkeypatch.setattr(d, "create_dashboard", Mock(return_value=app))
    d.main()
    app.launch.assert_called_once_with(server_name="127.0.0.1", server_port=7860, auth=("test", "dummy"), share=False)
    app.close.assert_called_once()


def test_gradio_app_registers_ten_second_timer():
    pytest.importorskip("gradio")
    app = d.create_dashboard()
    try:
        timers = [c for c in app.config["components"] if c["type"] == "timer"]
        assert timers[0]["props"]["value"] == 10
        assert len(app.config["dependencies"]) == 2
    finally:
        app.close()
