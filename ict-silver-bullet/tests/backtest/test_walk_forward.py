from datetime import timedelta

import pytest

from backtest import walk_forward as module
from tests.backtest.test_replay import BASE, candles


def test_split_disjoint_and_completed_bar_boundary():
    boundary = BASE + timedelta(minutes=3)
    history = candles(6) + [dict(candles(1)[0], timeframe="H1", ts=BASE - timedelta(minutes=30))]
    tune, validate = module.split_history(reversed(history), boundary)
    assert len(tune) == 2
    assert len(validate) == 5
    assert {r["ts"] for r in tune}.isdisjoint(r["ts"] for r in validate)
    assert all(r["ts"] + timedelta(minutes=1) < boundary for r in tune)
    assert validate[0]["timeframe"] == "H1"  # still incomplete at split


def test_sweep_never_sees_validate_and_runs_chosen_once(monkeypatch):
    calls = []
    boundary = BASE + timedelta(minutes=3)

    def replay(rows, **kwargs):
        calls.append((rows, kwargs))
        threshold = kwargs["threshold"]
        return dict(effective_threshold=max(8, threshold), metrics=dict(trades=2, expectancy_r=1 if threshold == 9 else 0),
                    decisions=[], trades=[])

    monkeypatch.setattr(module, "replay", replay)
    result = module.walk_forward(candles(6), boundary)
    assert result["chosen_threshold"] == 9
    assert len(calls) == 5
    for rows, kwargs in calls[:-1]:
        assert all(r["ts"] + timedelta(minutes=1) < boundary for r in rows)
        assert "decision_start" not in kwargs
    assert calls[-1][1]["decision_start"] == boundary
    assert calls[-1][1]["threshold"] == 9


def test_no_tune_trades_does_not_choose_using_validation(monkeypatch):
    calls = []

    def replay(rows, **kwargs):
        calls.append(kwargs)
        return dict(effective_threshold=kwargs["threshold"], metrics=dict(trades=0, expectancy_r=None))

    monkeypatch.setattr(module, "replay", replay)
    result = module.walk_forward(candles(6), BASE + timedelta(minutes=3))
    assert result["chosen_threshold"] is None and result["validation"] is None
    assert len(calls) == 4


def test_empty_split_rejected():
    with pytest.raises(ValueError, match="nonempty"):
        module.walk_forward(candles(), BASE)
