from dataclasses import replace
from datetime import timedelta

import pytest

from backtest.replay import (BreakoutPullback, FailedBreakout, StopCascadeContinuation, ResearchConfig,
                             monthly_statistics, research_replay)
from backtest import walk_forward as wf
from tests.backtest.test_replay import BASE, candles


def candidate(rows, context):
    assert all(r["ts"] + timedelta(minutes=1) <= context["now"] for r in rows)
    assert all(r["ts"] + timedelta(hours=1) <= context["now"] for r in context["htf_candles"])
    if len(rows) != 1:
        return None
    return dict(pair="EURUSD", direction="buy", entry=1.1, stop_loss=1.099,
                setup_id="first", opposing_level=None)


def test_research_fill_has_bid_ask_slippage_and_both_commission_legs():
    rows = candles(2)
    rows[1] = dict(rows[1], low=1.1, high=1.103)
    report = research_replay(rows, candidate_provider=candidate)
    trade = report["trades"][0]
    assert trade["entry"] == pytest.approx(1.10006)
    assert trade["take_profit"] == pytest.approx(1.10218)
    assert trade["exit"] == pytest.approx(trade["take_profit"] - .00001)
    assert trade["commission"] == pytest.approx(trade["lot_size"] * 7)
    assert trade["pnl"] == pytest.approx(trade["gross_pnl"] - trade["commission"])
    assert report["balance"] == pytest.approx(10000 + trade["pnl"])
    assert trade["r_multiple"] < 2
    assert report["research_only"] and not report["live_eligible"]


def test_short_uses_ask_for_stop():
    def sell(rows, context):
        setup = candidate(rows, context)
        return dict(setup, direction="sell", stop_loss=1.101) if setup else None

    rows = candles(2)
    rows[1] = dict(rows[1], high=1.10097, low=1.0998)
    report = research_replay(rows, candidate_provider=sell)
    assert report["trades"][0]["exit_reason"] == "stop"  # Bid alone never touched 1.101.


def test_stop_first_and_missing_next_bar():
    free = ResearchConfig(spread_pips=0, slippage_pips=0, commission_per_lot=0)
    report = research_replay(candles(2), candidate_provider=candidate, config=free)
    assert report["trades"][0]["exit_reason"] == "stop"
    assert report["trades"][0]["r_multiple"] == pytest.approx(-1)
    rows = candles(2)
    rows[1]["ts"] += timedelta(minutes=1)
    report = research_replay(rows, candidate_provider=candidate, config=free)
    assert not report["trades"]
    assert report["reasons"]["missing_next_bar"] == 1


def test_cost_gate_and_actual_fill_geometry_gate():
    expensive = ResearchConfig(spread_pips=5, max_entry_deviation_pips=10)
    report = research_replay(candles(2), candidate_provider=candidate, config=expensive)
    assert not report["trades"] and report["reasons"]["cost_exceeds_budget"] == 1

    def blocked(rows, context):
        setup = candidate(rows, context)
        return dict(setup, opposing_level=1.101) if setup else None

    report = research_replay(candles(2), candidate_provider=blocked)
    assert not report["trades"] and report["reasons"]["insufficient_target_room"] == 1


def test_time_exit_and_future_prices_cannot_change_prefix():
    rows = candles(5)
    for row in rows:
        row.update(high=1.1002, low=1.0998)
    config = ResearchConfig(max_hold_minutes=2)
    first = research_replay(rows[:3], candidate_provider=candidate, config=config)
    changed = rows[:3] + [dict(rows[3], high=9, close=8)]
    second = research_replay(changed, candidate_provider=candidate, config=config)
    assert first["trades"] == second["trades"]
    assert first["trades"][0]["exit_reason"] == "time_exit"


def test_monthly_statistics_keeps_flat_month_and_reports_misses():
    report = monthly_statistics({"2024-01": 10000, "2024-02": 10000,
                                 "2024-03": 11000, "2024-04": 11000}, 10000)
    assert report["complete_months"] == 2
    assert report["months"][1]["return_pct"] == 0
    assert report["months_at_target"] == 1
    assert report["target_hit_rate"] == .5
    assert not report["all_complete_months_at_target"]


def test_breakout_needs_completed_m5_then_later_reclaim():
    model = BreakoutPullback()
    for i in range(23):
        close = 1.08 + i * .001
        model.update_h1(dict(ts=BASE - timedelta(hours=24 - i), open=close,
                             high=close + .0002, low=close - .0002, close=close))
    series = [dict(pair="EURUSD", ts=BASE + timedelta(minutes=i), open=1.1001,
                   high=1.1006, low=1.0998, close=1.1001) for i in range(75)]
    for row in series[70:]:
        row.update(open=1.1008, high=1.102, low=1.1007, close=1.1015)
    for i, row in enumerate(series):
        if i == 59:
            model.update_h1(dict(ts=BASE, open=1.102, high=1.1032, low=1.1018, close=1.103))
        assert model.update_m1(row) is None
    assert model.armed["formed_at"] == BASE + timedelta(minutes=75)
    setup = model.update_m1(dict(pair="EURUSD", ts=BASE + timedelta(minutes=75),
                                 open=1.1008, high=1.101, low=1.1005, close=1.1009))
    assert setup["direction"] == "buy"
    assert setup["stop_loss"] < 1.1005
    assert setup["evidence"]["level"] == pytest.approx(1.1006)


def test_failed_breakout_waits_for_completed_break_then_m1_cross_back():
    model = FailedBreakout()
    series = [dict(pair="EURUSD", ts=BASE + timedelta(minutes=i), open=1.1001,
                   high=1.1006, low=1.0998, close=1.1001) for i in range(75)]
    for row in series[70:75]:
        row.update(open=1.1007, high=1.1015, low=1.1006, close=1.1012)

    # The breakout becomes known only at the close of its fifth M1 candle.
    for row in series:
        assert model.update_m1(row) is None
    assert model.armed["formed_at"] == BASE + timedelta(minutes=75)

    failure = dict(pair="EURUSD", ts=BASE + timedelta(minutes=75), open=1.1012,
                   high=1.1014, low=1.1000, close=1.1004)
    setup = model.update_m1(failure)
    assert setup["direction"] == "sell"
    assert setup["entry"] == pytest.approx(1.1004)
    assert setup["stop_loss"] == pytest.approx(1.1016)
    assert setup["setup_id"] == (BASE + timedelta(minutes=75)).isoformat()
    assert setup["evidence"]["breakout_direction"] == "up"


def test_failed_breakout_research_is_research_only_and_labeled():
    rows = candles(3)
    report = research_replay(rows, strategy="failed_breakout")
    assert report["strategy"] == "failed_breakout"
    assert "first directional M1 close back" in report["pattern_policy"]
    assert report["research_only"] and not report["live_eligible"]


def test_stop_cascade_uses_completed_range_and_suppresses_same_range_reentry():
    model = StopCascadeContinuation()
    prior = [dict(pair="EURUSD", ts=BASE + timedelta(minutes=i), open=1.1,
                  high=1.1001, low=1.0999, close=1.1) for i in range(30)]
    for row in prior:
        assert model.update_m1(row) is None

    trigger = dict(pair="EURUSD", ts=BASE + timedelta(minutes=30), open=1.1003,
                   high=1.1005, low=1.1003, close=1.1004)
    setup = model.update_m1(trigger)
    assert setup["direction"] == "buy"
    assert setup["evidence"]["prior_range_high"] == pytest.approx(1.1001)
    assert setup["setup_id"] == trigger["ts"].isoformat()
    assert setup["max_stop_distance"] == pytest.approx(.0002)

    for i in range(31, 60):
        later_break = dict(trigger, ts=BASE + timedelta(minutes=i))
        assert model.update_m1(later_break) is None


def test_stop_cascade_research_is_labeled_research_only():
    report = research_replay(candles(3), strategy="stop_cascade_continuation")
    assert report["strategy"] == "stop_cascade_continuation"
    assert "prior 30-bar range" in report["pattern_policy"]
    assert report["research_only"] and not report["live_eligible"]


def test_research_sweep_keeps_later_data_out_and_runs_one_choice(monkeypatch):
    calls = []
    split = BASE + timedelta(minutes=3)

    def fake(rows, **kwargs):
        calls.append((rows, kwargs))
        good = kwargs["strategy"] == "breakout_pullback" and kwargs["config"].reward_r == 2
        return dict(metrics=dict(trades=200, expectancy_r=.2 if good else -.1),
                    monthly=dict(complete_months=12), max_drawdown_pct=1, halted=False, trades=[])

    monkeypatch.setattr(wf, "research_replay", fake)
    result = wf.screen_research(candles(6), split)
    assert result["chosen"] == "breakout_2r"
    assert not result["promotion_passed"] and not result["live_eligible"]
    assert len(calls) == 6
    assert all(r["ts"] + timedelta(minutes=1) < split for rows, _ in calls[:5] for r in rows)
    assert calls[-1][1]["decision_start"] == split


def test_negative_or_tiny_results_never_selected(monkeypatch):
    monkeypatch.setattr(wf, "research_replay", lambda *a, **k: dict(
        metrics=dict(trades=4, expectancy_r=-.01), monthly=dict(complete_months=12),
        max_drawdown_pct=1, halted=False))
    result = wf.screen_research(candles(6), BASE + timedelta(minutes=3))
    assert result["chosen"] is None and result["validation"] is None
    monkeypatch.setattr(wf, "replay", lambda *a, **k: dict(
        effective_threshold=k["threshold"], metrics=dict(trades=100, expectancy_r=-.01)))
    result = wf.walk_forward(candles(6), BASE + timedelta(minutes=3))
    assert result["chosen_threshold"] is None and result["validation"] is None


def test_return_goal_math_and_config_validation():
    result = wf.target_feasibility()
    assert result["required_constant_net_r"] == pytest.approx(((1.1 ** (1 / 80)) - 1) / .0025)
    for changes in ({"risk_pct": .01}, {"spread_pips": -1}, {"reward_r": float("nan")}):
        with pytest.raises(ValueError):
            replace(ResearchConfig(), **changes)


def test_gap_loss_triggers_shared_overall_halt():
    rows = candles(4)
    for row in rows:
        row.update(high=1.1002, low=1.0998)
    rows[2].update(open=1.07, high=1.0702, low=1.0698, close=1.07)
    report = research_replay(rows, candidate_provider=candidate)
    assert report["halted"]
    assert report["max_drawdown_pct"] >= 5.25
    assert len(report["trades"]) == 1
    assert report["trades"][0]["exit"] < 1.07


def test_ict_research_uses_shared_detector_with_live_bar_indices(monkeypatch):
    from brain import app

    def detect(rows, context):
        assert [r["index"] for r in rows] == list(range(len(rows)))
        return candidate(rows, context)

    monkeypatch.setattr(app, "_detect_candidate", detect)
    result = research_replay(candles(2), strategy="ict_2r")
    assert len(result["trades"]) == 1
