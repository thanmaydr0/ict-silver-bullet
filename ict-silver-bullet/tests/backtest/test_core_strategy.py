from dataclasses import replace
from datetime import timedelta
import json

import pytest

from backtest import replay as replay_module
from backtest import walk_forward as wf
from backtest.replay import ResearchConfig
from brain import app
from tests.backtest.test_replay import BASE, candles, setup


def test_cost_aware_replay_calls_shared_core_and_preserves_signal_target(tmp_path):
    observations = []
    def detect(rows, context):
        assert all(r["ts"] + timedelta(minutes=1) <= context["now"] for r in rows)
        observations.append(context["now"])
        return dict(setup(rows, context), setup_id="first") if len(rows) == 1 else None
    history = candles(2)
    history[1].update(low=1.1, high=1.103)
    path = tmp_path / "audit.jsonl"
    report = replay_module.research_replay(history, strategy="core", candidate_provider=detect,
                                           results_path=path)
    assert len(observations) == 2
    trade = report["trades"][0]
    assert trade["take_profit"] == 1.102  # Original signal, never improved after the fill.
    assert trade["entry"] > 1.1 and trade["commission"] > 0
    assert report["risk_score_policy"] == "shared core confluence and risk engine"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    audit = next(r for r in records if r["type"] == "core_decision")
    assert audit["candles"] and audit["decision"]["risk_result"]["approved"]
    assert audit["news_policy"] == "skipped"


def test_core_threshold_gate_is_not_bypassed_by_research_score_ten():
    def eight(rows, context):
        result = setup(rows, context)
        result["factors"]["entry_in_ote_zone"] = False
        result["factors"]["fvg_present_unmitigated"] = False
        return result
    report = replay_module.research_replay(candles(2), strategy="core", threshold=9,
                                           candidate_provider=eight)
    assert not report["trades"]
    assert report["reasons"]["decision_score_below_sweep_threshold"] == 2


def test_core_derives_completed_h1_without_partial_or_future_candles(monkeypatch):
    captured = []
    def evaluate(rows, context):
        captured.append(context["htf_candles"])
        assert all(r["ts"]+timedelta(hours=1) <= context["now"] for r in context["htf_candles"])
        return app.PipelineDecision(False, "no_candidate")
    monkeypatch.setattr(app, "evaluate_candles", evaluate)
    history = [dict(r, ts=r["ts"]-timedelta(hours=1)) for r in candles(61)]
    replay_module.research_replay(history, strategy="core")
    assert captured and len(captured[-1]) == 1
    assert captured[-1][0]["ts"] == BASE-timedelta(hours=1)
    captured.clear()
    replay_module.research_replay(history[:20]+history[21:], strategy="core")
    assert captured and not captured[-1]


def test_core_sweep_never_sees_later_period_and_stress_cannot_select(monkeypatch, tmp_path):
    calls = []
    split = BASE+timedelta(minutes=3)
    def fake(rows, **kwargs):
        calls.append((rows, kwargs))
        return dict(metrics=dict(trades=150, expectancy_r=.3 if kwargs["threshold"] == 9 else .1),
                    monthly=dict(complete_months=12), max_drawdown_pct=1., halted=False, trades=[])
    monkeypatch.setattr(wf, "research_replay", fake)
    result = wf.core_walk_forward(candles(6), split, output_dir=tmp_path)
    assert result["chosen_threshold"] == 9
    assert len(calls) == 5
    assert all(r["ts"]+timedelta(minutes=1) < split for rows, _ in calls[:3] for r in rows)
    assert all(k["threshold"] == 9 and k["decision_start"] == split for _, k in calls[3:])
    assert calls[-1][1]["config"].spread_pips == 1.
    assert not result["live_eligible"]
    spec = json.loads((tmp_path/"frozen_specification.json").read_text())
    assert "not fresh out-of-sample" in spec["data_status"]


def test_core_negative_tune_does_not_touch_later_period(monkeypatch):
    calls = []
    def fake(rows, **kwargs):
        calls.append(kwargs)
        return dict(metrics=dict(trades=200, expectancy_r=-.1), monthly={},
                    max_drawdown_pct=1., halted=False)
    monkeypatch.setattr(wf, "research_replay", fake)
    result = wf.core_walk_forward(candles(6), BASE+timedelta(minutes=3))
    assert result["chosen_threshold"] is None
    assert len(calls) == 3
    assert all("decision_start" not in k for k in calls)
    with pytest.raises(ValueError):
        wf.core_walk_forward(candles(6), BASE+timedelta(minutes=3),
                            config=replace(ResearchConfig(), reward_r=1.5))
