from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from brain import app
from brain.detection import trend_reclaim as model
from brain.scoring import confluence

NOW = datetime(2026, 10, 7, 14, 30, tzinfo=timezone.utc)


def history(buy=True):
    bars = []
    for i in range(90):
        bar = dict(ts=NOW - timedelta(minutes=90-i), open=1.1015,
                   high=1.103, low=1.100, close=1.1015)
        if 80 <= i < 85:
            bar.update(open=1.1002, high=1.1005, low=1.0998, close=1.1003)
        if i >= 85:
            bar.update(open=1.1003, high=1.1007, low=1.1001, close=1.1006)
        bars.append(bar)
    htf = [dict(ts=NOW - timedelta(hours=53-i), open=1.08 + .0004*i,
                high=1.081 + .0004*i, low=1.079 + .0004*i, close=1.08 + .0004*i)
           for i in range(53)]
    htf[40]["high"] = 1.12  # Completed, confirmed runner pool above the scalp.
    if not buy:
        for row in bars + htf:
            row.update(open=2.2-row["open"], close=2.2-row["close"],
                       high=2.2-row["low"], low=2.2-row["high"])
    return bars, htf


@pytest.mark.parametrize("buy", [True, False])
def test_real_core_sweep_reclaim_geometry_and_explicit_score_profile(buy):
    bars, htf = history(buy)
    originals = deepcopy((bars, htf))
    result = app._detect_candidate(bars, dict(now=NOW, pair="EURUSD", htf_candles=htf))
    assert result["strategy"] == "trend_reclaim"
    assert result["direction"] == ("buy" if buy else "sell")
    assert result["entry"] == pytest.approx(bars[-1]["close"])
    assert abs(result["take_profit"]-result["entry"]) == pytest.approx(
        2*abs(result["entry"]-result["stop_loss"]))
    score = confluence.score_setup(result["factors"], True)
    assert score["score"] >= 8
    assert set(score["breakdown"]) == set(confluence.RECLAIM_FACTOR_POINTS)
    assert "fvg" not in result and "mss" not in result
    assert (bars, htf) == originals


def test_no_signal_until_confirmation_is_complete_and_no_future_bar_effect():
    bars, htf = history()
    assert app._detect_candidate(bars[:-1], dict(now=NOW-timedelta(minutes=1),
        pair="EURUSD", htf_candles=htf)) is None
    original = app._detect_candidate(bars, dict(now=NOW, pair="EURUSD", htf_candles=htf))
    future = dict(bars[-1], ts=NOW, high=9., close=8.)
    filtered = app._bars(bars+[future], NOW, timedelta(minutes=1))
    assert app._detect_candidate(filtered, dict(now=NOW, pair="EURUSD", htf_candles=htf)) == original
    assert app._detect_candidate(bars, dict(now=NOW+timedelta(minutes=1),
        pair="EURUSD", htf_candles=htf)) is None


def test_missing_minutes_flat_htf_and_absent_runner_pool_fail_closed():
    bars, htf = history()
    assert app._detect_candidate(bars[1:], dict(now=NOW, pair="EURUSD", htf_candles=htf)) is None
    flat = [dict(row, open=1.1, close=1.1) for row in htf]
    assert app._detect_candidate(bars, dict(now=NOW, pair="EURUSD", htf_candles=flat)) is None
    no_pool = [dict(row, high=max(row["open"], row["close"])+.0001) for row in htf]
    assert app._detect_candidate(bars, dict(now=NOW, pair="EURUSD", htf_candles=no_pool)) is None


def test_shared_pipeline_reviews_new_strategy_without_inventing_fvg_or_ote():
    bars, htf = history()
    contexts = []
    def review(context):
        contexts.append(context)
        return dict(verdict="approve", conviction=.5, reasoning="test")
    decision = app.evaluate_candles(bars, dict(now=NOW, pair="EURUSD", htf_candles=htf,
        news_events=[], review_setup=review, risk_state=dict(equity=10000,
            drawdown_state=dict(daily_dd_pct=0, overall_dd_pct=0),
            trades_today=0, consecutive_losses_today=0, open_positions=[])))
    assert decision.approved
    assert contexts[0]["fvg_top"] == "not used by this strategy"
    assert contexts[0]["ote_overlap_bool"] == "not used by this strategy"
    assert "trend_reclaim" in contexts[0]["candles_summary"]
    assert confluence.score_setup(decision.setup["factors"], False)["reason"] == "news_blackout"
