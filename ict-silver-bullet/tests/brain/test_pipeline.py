"""Orchestration tests with no live credentials, providers or database."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from brain import app
from brain.llm.schema import LLMVerdict
from brain.pairs import pair_spec
from brain.risk.circuit_breakers import trading_day_start

NOW = datetime(2026, 10, 7, 14, 30, tzinfo=timezone.utc)
REAL_RISK = app.engine.evaluate_signal
REAL_DETECT = app._detect_candidate


def candidate():
    return dict(pair="EURUSD", direction="buy", entry=1.10, stop_loss=1.099,
        take_profit=1.103, htf_bias="bullish", sweep={"index": 1, "wick_extreme": 1.0991},
        mss={"index": 2, "displacement": 0.002}, atr=0.001,
        fvg={"top": 1.1001, "bottom": 1.0999},
        factors=dict.fromkeys(app.confluence.FACTOR_POINTS, True))


@pytest.fixture
def live(monkeypatch):
    order = []
    patches = {}

    def patch(target, name, value=None, effect=None):
        def call(*args, **kwargs):
            order.append(name)
            return effect(*args, **kwargs) if effect else value
        mocked = Mock(side_effect=call)
        monkeypatch.setattr(target, name, mocked)
        patches[name] = mocked
        return mocked

    bars = [dict(ts=NOW - timedelta(minutes=2), open=1.1, high=1.101,
                 low=1.099, close=1.1)]
    patch(app, "load_config", SimpleNamespace(TRADED_PAIRS=["EURUSD"]))
    patch(app.killzone_clock, "current_killzone", "ny_am")
    patch(app.db, "get_latest_candles", bars)
    # Even accidental calls cannot reach an actual Supabase client.
    patch(app.db, "_get_client", effect=lambda: pytest.fail("unexpected client access"))
    patch(app, "_news_events", [])
    real_blackout = app.blackout.is_in_news_blackout
    patch(app.blackout, "is_in_news_blackout", effect=real_blackout)
    patch(app, "_detect_candidate", candidate())
    real_score = app.confluence.score_setup
    patch(app.confluence, "score_setup", effect=real_score)
    patch(app.reasoning, "review_setup", LLMVerdict(verdict="approve", conviction=0.8,
        reasoning="Coherent setup", key_risk="slippage"))
    patch(app.db, "get_latest_equity_snapshot", {"equity": "10000", "balance": "10000", "ts": NOW})
    patch(app.db, "get_equity_snapshots", [{"balance": "10000", "equity": "10000", "ts": trading_day_start(NOW)}])
    patch(app.db, "get_max_equity", 10000)
    patch(app.db, "get_trades_since", [])
    patch(app.db, "get_open_trades", [])
    patch(app.engine, "evaluate_signal", {"approved": True, "reason": "approved", "lot_size": 0.25})
    patch(app.db, "insert_signal", 42)
    return SimpleNamespace(order=order, mocks=patches, bars=bars)


def test_outside_killzone_reads_nothing(live):
    live.mocks["current_killzone"].side_effect = lambda now: None
    decision = app.run_pipeline_once(NOW)[0]
    assert decision.reason == "outside_killzone"
    assert live.order == []
    for name, mocked in live.mocks.items():
        if name != "current_killzone":
            mocked.assert_not_called()


def test_pure_outside_killzone_does_not_resolve_candles(live):
    reader = Mock(side_effect=AssertionError("must not read"))
    decision = app.evaluate_candles(reader, {"now": NOW, "current_killzone": lambda now: None})
    assert decision.reason == "outside_killzone"
    reader.assert_not_called()


def test_low_score_stops_before_llm_and_account_reads(live, caplog):
    setup = candidate()
    setup["factors"]["htf_bias_aligned"] = False
    setup["factors"]["liquidity_sweep_before_mss"] = False
    live.mocks["_detect_candidate"].side_effect = lambda *args: setup
    with caplog.at_level("INFO", logger="brain"):
        decision = app.run_pipeline_once(NOW)[0]
    assert decision.score_result["score"] == 6
    assert decision.reason == "score_below_minimum"
    assert "score:" in caplog.text and "score_below_minimum" in caplog.text
    for name in ("review_setup", "get_latest_equity_snapshot", "evaluate_signal", "insert_signal"):
        live.mocks[name].assert_not_called()


@pytest.mark.parametrize("offset", [-15, 0, 15])
def test_blackout_is_computed_before_score_and_forces_zero(live, offset):
    live.mocks["_news_events"].side_effect = lambda now: [dict(currency="USD", impact="high",
        scheduled_at=NOW + timedelta(minutes=offset))]
    decision = app.run_pipeline_once(NOW)[0]
    assert decision.score_result["score"] == 0
    assert decision.reason == "news_blackout"
    assert live.order.index("is_in_news_blackout") < live.order.index("_detect_candidate") < live.order.index("score_setup")
    assert live.mocks["score_setup"].call_args.kwargs["no_high_impact_news_next_15min"] is False
    live.mocks["review_setup"].assert_not_called()
    live.mocks["evaluate_signal"].assert_not_called()
    live.mocks["insert_signal"].assert_not_called()


def test_veto_stops_before_risk_and_account_reads(live):
    live.mocks["review_setup"].side_effect = lambda context: LLMVerdict(
        verdict="veto", conviction=0.9, reasoning="Contradictory", key_risk="structure")
    decision = app.run_pipeline_once(NOW)[0]
    assert decision.reason == "llm_veto"
    for name in ("get_latest_equity_snapshot", "get_trades_since", "evaluate_signal", "insert_signal"):
        live.mocks[name].assert_not_called()


def test_approved_setup_inserts_one_pending_signal_in_order(live):
    decision = app.run_pipeline_once(NOW)[0]
    assert decision.approved
    assert live.order == ["current_killzone", "load_config", "get_latest_candles",
        "get_latest_candles", "current_killzone", "_news_events", "is_in_news_blackout",
        "_detect_candidate", "score_setup", "review_setup", "get_latest_equity_snapshot",
        "get_equity_snapshots", "get_max_equity", "get_trades_since", "get_open_trades",
        "evaluate_signal", "insert_signal"]
    live.mocks["insert_signal"].assert_called_once_with(decision.signal)
    assert decision.signal["status"] == "pending"
    assert decision.signal["lot_size"] == 0.25
    assert decision.signal["llm_verdict"] == "approve"
    assert decision.signal["llm_conviction"] == 0.8
    assert decision.signal["llm_reasoning"] == "Coherent setup"
    assert decision.signal["detected_at"] == NOW
    assert live.mocks["get_trades_since"].call_args.args == (trading_day_start(NOW),)
    args = live.mocks["evaluate_signal"].call_args.args
    assert args[1].high_water_mark_equity == 10000
    assert args[2]["stop_loss_pips"] == pytest.approx(10)


def test_downweight_is_applied_before_real_risk_engine(live):
    setup = candidate()
    setup["factors"]["fvg_present_unmitigated"] = False
    setup["factors"]["entry_in_ote_zone"] = False
    live.mocks["_detect_candidate"].side_effect = lambda *args: setup
    live.mocks["review_setup"].side_effect = lambda ctx: dict(verdict="downweight", conviction=0.0, reasoning="weak")
    live.mocks["evaluate_signal"].side_effect = REAL_RISK
    decision = app.run_pipeline_once(NOW)[0]
    assert decision.score_result["score"] == 7
    assert not decision.approved
    assert live.mocks["evaluate_signal"].call_args.args[0]["score"] == 7
    assert decision.reason == "score_below_minimum"
    live.mocks["insert_signal"].assert_not_called()


def test_replay_matches_live_without_persisting_or_mutating(live):
    live_decision = app.run_pipeline_once(NOW)[0]
    inputs = deepcopy(live.bars)
    ctx = dict(now=NOW, pair="EURUSD", htf_candles=inputs, news_events=[],
        risk_state=dict(equity=10000, drawdown_state={"daily_dd_pct": 0, "overall_dd_pct": 0},
            trades_today=0, consecutive_losses_today=0, open_positions=[]),
        review_setup=live.mocks["review_setup"])
    count = live.mocks["insert_signal"].call_count
    replay = app.evaluate_candles(inputs, ctx)
    assert replay.signal == live_decision.signal
    assert inputs == live.bars
    assert live.mocks["insert_signal"].call_count == count


def test_news_query_includes_both_boundaries(monkeypatch):
    query = Mock()
    for name in ("table", "select", "gte", "lte", "order", "range"):
        getattr(query, name).return_value = query
    query.execute.return_value = SimpleNamespace(data=[])
    monkeypatch.setattr(app.db, "_get_client", Mock(return_value=query))
    assert app._news_events(NOW) == []
    query.gte.assert_called_once_with("scheduled_at", (NOW - timedelta(minutes=15)).isoformat())
    query.lte.assert_called_once_with("scheduled_at", (NOW + timedelta(minutes=15)).isoformat())


def test_risk_state_groups_legs_and_restores_peak(live):
    closed = NOW - timedelta(minutes=30)
    trades = [dict(signal_id=1, closed_at=closed, realized_usd=-10),
              dict(signal_id=1, closed_at=closed, realized_usd=20),
              dict(signal_id=2, closed_at=NOW, realized_usd=-20),
              dict(signal_id=3, closed_at=None)]
    live.mocks["get_trades_since"].side_effect = lambda since: trades
    live.mocks["get_max_equity"].side_effect = lambda: 10500
    live.mocks["get_open_trades"].side_effect = lambda: [
        dict(signal_id=3, pair="EURUSD", direction="buy"),
        dict(signal_id=3, pair="EURUSD", direction="buy")]
    state = app._risk_state(NOW)
    assert state["trades_today"] == 3
    assert state["consecutive_losses_today"] == 1
    assert len(state["open_positions"]) == 1
    assert state["drawdown_state"].overall_dd_pct == pytest.approx(500 / 10500)


def test_missing_equity_fails_closed(live):
    live.mocks["get_max_equity"].side_effect = lambda: None
    assert app.run_pipeline_once(NOW) == []
    live.mocks["evaluate_signal"].assert_not_called()
    live.mocks["insert_signal"].assert_not_called()


def test_loop_logs_then_backs_off_and_survives(monkeypatch):
    order = []
    monkeypatch.setattr(app, "setup_logging", lambda name: order.append(name))
    run = Mock(side_effect=[RuntimeError("failed"), None])
    monkeypatch.setattr(app, "run_pipeline_once", run)
    sleep = Mock(side_effect=[None, KeyboardInterrupt])
    monkeypatch.setattr(app.time, "sleep", sleep)
    app.main()
    assert order == ["brain"]
    assert run.call_count == 2
    assert sleep.call_args_list[0].args == (60,)


def test_jpy_pip_value_tracks_price():
    assert pair_spec("USDJPY", 150) == {"pip_size": 0.01, "pip_value_per_lot": 1000 / 150}
    assert pair_spec("EURUSD", 1.1)["pip_value_per_lot"] == 10


def test_no_candidate_stops_before_score(live):
    live.mocks["_detect_candidate"].side_effect = lambda *args: None
    assert app.run_pipeline_once(NOW)[0].reason == "no_candidate"
    for name in ("score_setup", "review_setup", "evaluate_signal", "insert_signal"):
        live.mocks[name].assert_not_called()


@pytest.mark.parametrize("bias", ["bullish", "bearish"])
def test_detection_wires_primitives_and_price_geometry(monkeypatch, bias):
    bullish = bias == "bullish"
    bars = [dict(index=i, ts=NOW - timedelta(minutes=21-i), open=1.102,
        high=1.104, low=1.100, close=1.102) for i in range(20)]
    htf = [dict(ts=NOW - timedelta(hours=3-i), open=1.10, high=1.12,
        low=1.09, close=1.1 + (0.01 if bullish else -0.01) * i) for i in range(2)]
    pivots = [app.swings.SwingPoint(5, 1.101 if bullish else 1.103,
                                   "low" if bullish else "high")]
    calls = {}
    def patch(module, name, result):
        calls[name] = Mock(return_value=result)
        monkeypatch.setattr(module, name, calls[name])
    patch(app.swings, "find_swing_points", pivots)
    patch(app.mss, "detect_mss", dict(index=15, level=1.102, displacement=0.007))
    patch(app.liquidity, "detect_liquidity_sweep", dict(index=14,
        wick_extreme=1.099 if bullish else 1.105))
    patch(app.fvg, "detect_fvg", [dict(index=16, top=1.103, bottom=1.101, type=bias)])
    patch(app.order_block, "find_order_block", bars[13])
    patch(app.liquidity, "find_nearest_liquidity_pool", dict(level=1.108 if bullish else 1.096))
    result = REAL_DETECT(bars, dict(now=NOW, pair="EURUSD", htf_candles=htf))
    assert result["direction"] == ("buy" if bullish else "sell")
    assert result["factors"]["liquidity_sweep_before_mss"]
    assert result["factors"]["displacement_ge_1_5x_atr"]
    assert calls["detect_liquidity_sweep"].call_args.args[0] == bars[:15]
    assert calls["detect_liquidity_sweep"].call_args.args[2] == ("buy_side" if bullish else "sell_side")
    assert result["stop_loss"] < result["entry"] < result["take_profit"] if bullish else result["take_profit"] < result["entry"] < result["stop_loss"]
    assert all(mocked.call_count == 1 for mocked in calls.values())


def test_incomplete_candles_never_enter_detection(live):
    live.mocks["get_latest_candles"].side_effect = lambda *args: live.bars + [
        dict(live.bars[0], ts=NOW), dict(live.bars[0], ts=NOW + timedelta(minutes=1))]
    app.run_pipeline_once(NOW)
    detected_bars = live.mocks["_detect_candidate"].call_args.args[0]
    assert len(detected_bars) == 1
    assert detected_bars[0]["index"] == 0
