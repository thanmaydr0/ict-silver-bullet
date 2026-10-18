from datetime import timedelta
import json

import pytest

from backtest.replay import session_momentum_replay
from brain import app
from tests.brain.detection.test_session_momentum import sessions


def test_actual_core_is_used_with_costs_no_fixed_target_and_causal_audit(monkeypatch, tmp_path):
    rows, opening = sessions()
    calls = []
    original = app.evaluate_candles
    def evaluate(bars, context):
        assert all(r["ts"]+timedelta(minutes=1) <= context["now"] for r in bars)
        assert all(t < opening for t in context["session_profile"]["training_sessions"])
        calls.append(context["now"])
        return original(bars, context)
    monkeypatch.setattr(app, "evaluate_candles", evaluate)
    path = tmp_path/"audit.jsonl"
    report = session_momentum_replay(rows, decision_start=opening, results_path=path)
    assert calls
    assert len(report["trades"]) == 1
    trade = report["trades"][0]
    assert trade["exit_reason"] == "momentum_lost"
    assert trade["take_profit"] is None
    assert trade["commission"] > 0
    assert trade["entry"] > rows[14*240+30]["open"]
    assert trade["exit"] < rows[14*240+89]["close"]
    assert not report["live_eligible"]
    record = next(json.loads(line) for line in path.read_text().splitlines()
                  if json.loads(line)["type"] == "decision")
    assert len(record["profile"]["training_moves"]) == 14


def test_future_prices_cannot_change_closed_prefix():
    rows, opening = sessions()
    prefix = rows[:14*240+90]
    first = session_momentum_replay(prefix, decision_start=opening)
    altered = prefix+[dict(rows[14*240+90], high=9., close=8.)]
    second = session_momentum_replay(altered, decision_start=opening)
    assert first["trades"] == second["trades"]


def test_protective_stop_takes_priority_over_checkpoint_exit():
    rows, opening = sessions()
    rows[14*240+31].update(low=1.0995)
    report = session_momentum_replay(rows, decision_start=opening)
    assert report["trades"][0]["exit_reason"] == "hard_stop"
    assert report["trades"][0]["r_multiple"] < -1


def test_day_block_interval_preserves_same_day_clustering():
    from backtest.walk_forward import day_cluster_expectancy
    rows, opening = sessions()
    trades = [dict(opened_at=opening, r_multiple=1.), dict(opened_at=opening, r_multiple=-1.)]
    result = day_cluster_expectancy(trades, samples=100)
    assert result["trading_days"] == 1
    assert result["lower_95_r"] == result["upper_95_r"] == 0
    assert day_cluster_expectancy([])["lower_95_r"] is None


def test_single_frozen_candidate_does_not_select_on_later_data(monkeypatch):
    from backtest import walk_forward as wf
    from backtest import monte_carlo
    from tests.backtest.test_replay import BASE, candles
    calls = []
    def replay(rows, **kwargs):
        calls.append((rows, kwargs))
        return dict(metrics=dict(trades=120, expectancy_r=1.), halted=False,
            trades=[dict(opened_at=BASE+timedelta(days=i), r_multiple=1.) for i in range(120)])
    monkeypatch.setattr(wf, "session_momentum_replay", replay)
    monkeypatch.setattr(monte_carlo, "simulate", lambda *a, **k: dict(simulations=k["simulations"]))
    split = BASE+timedelta(minutes=3)
    result = wf.momentum_walk_forward(candles(6), split)
    assert result["qualified_for_later_period"]
    assert len(calls) == 3
    assert all(r["ts"]+timedelta(minutes=1) < split for r in calls[0][0])
    assert all(k["decision_start"] == split for _, k in calls[1:])
    assert calls[2][1]["config"].spread_pips == 2*calls[1][1]["config"].spread_pips
    assert not result["live_eligible"]


def test_uncertain_tune_result_never_runs_later_period(monkeypatch):
    from backtest import walk_forward as wf
    from tests.backtest.test_replay import BASE, candles
    calls = []
    def replay(rows, **kwargs):
        calls.append(kwargs)
        return dict(metrics=dict(trades=120, expectancy_r=.01), halted=False,
            trades=[dict(opened_at=BASE+timedelta(days=i),
                         r_multiple=1.02 if i % 2 else -1.) for i in range(120)])
    monkeypatch.setattr(wf, "session_momentum_replay", replay)
    result = wf.momentum_walk_forward(candles(6), BASE+timedelta(minutes=3))
    assert not result["qualified_for_later_period"]
    assert result["tune_interval"]["lower_95_r"] < 0
    assert len(calls) == 1
