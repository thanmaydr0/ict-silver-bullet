import pytest

from backtest.monte_carlo import simulate
from brain.risk.circuit_breakers import HARD_OVERALL_DD_PCT, HARD_DAILY_DD_PCT


def test_all_wins_zero_ruin():
    report = simulate([2] * 100, simulations=1000)
    assert report["risk_of_ruin_pct"] == 0
    assert report["target_reached_pct"] == 100
    assert report["max_drawdown"]["max"] == 0


def test_all_losses_ruin():
    report = simulate([-1] * 100, simulations=1000)
    assert report["risk_of_ruin_pct"] == 100
    assert report["target_reached_pct"] == 0
    assert report["max_drawdown"]["min"] >= HARD_OVERALL_DD_PCT


def test_daily_soft_reduction_and_hard_stop():
    report = simulate([-1] * 50, risk_pct=.01, trades_per_day=50, simulations=10)
    # Three full losses reach ~2.97%, followed by one half-risk loss.
    assert report["risk_of_ruin_pct"] == 100
    assert report["max_drawdown"]["max"] == pytest.approx(1 - .99 ** 3 * .995)
    assert report["thresholds"]["hard_daily"] == HARD_DAILY_DD_PCT


def test_seed_reproducibility_and_distribution():
    trades = [2] * 35 + [-1] * 65
    report = simulate(trades, simulations=200, seed=42, risk_pct=.005)
    assert report == simulate(trades, simulations=200, seed=42, risk_pct=.005)
    dd = report["max_drawdown"]
    assert dd["min"] <= dd["median"] <= dd["p95"] <= dd["p99"] <= dd["max"]
    assert len(dd["samples"]) == 200
    assert sum(report[k] for k in ("risk_of_ruin_pct", "target_reached_pct", "unfinished_pct")) == pytest.approx(100)


def test_short_sequence_unfinished():
    result = simulate([1], simulations=10)
    assert result["unfinished_pct"] == 100
    assert result["risk_of_ruin_pct"] == 0


def test_soft_overall_requires_score_nine():
    result = simulate([dict(r_multiple=-1, confluence_score=8)] * 100, simulations=10)
    assert result["soft_overall_skipped_trades"] > 0
    assert result["unfinished_pct"] == 100
    assert result["max_drawdown"]["max"] < HARD_OVERALL_DD_PCT


def test_gap_loss_beyond_balance_is_ruin():
    report = simulate([-500], simulations=10)
    assert report["risk_of_ruin_pct"] == 100
    assert report["max_drawdown"]["max"] == pytest.approx(1.25)


@pytest.mark.parametrize("trades,kwargs", [([], {}), ([float("nan")], {}), ([1], {"risk_pct": 0}), ([1], {"simulations": 0})])
def test_invalid_inputs(trades, kwargs):
    with pytest.raises(ValueError):
        simulate(trades, **kwargs)
