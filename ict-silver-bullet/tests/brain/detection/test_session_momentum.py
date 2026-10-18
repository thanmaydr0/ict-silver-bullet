from datetime import datetime, timedelta, timezone

import pytest

from brain import app
from brain.detection import session_momentum as model
from brain.risk.engine import evaluate_signal


def sessions():
    days = []
    day = datetime(2024, 1, 2, 8, tzinfo=timezone.utc)
    while len(days) < 15:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    rows = []
    for n, day in enumerate(days):
        previous = 1.1
        for i in range(240):
            if n < 14:
                close = 1.1+.0004*min(i+1, 30)/30
            elif i < 30:
                close = 1.1+.002*(i+1)/30
            elif i < 60:
                close = 1.102+.001*(i-29)/30
            elif i < 90:
                close = 1.103-.003*(i-59)/30
            else:
                close = 1.1
            rows.append(dict(pair="EURUSD", timeframe="M1", ts=day+timedelta(minutes=i),
                open=previous, close=close, high=max(previous, close)+.00002,
                low=min(previous, close)-.00002))
            previous = close
    return rows, days[-1]


def test_profile_requires_fourteen_full_prior_sessions_and_current_is_not_training():
    rows, opening = sessions()
    state = model.SessionHistory()
    for row in rows[:14*240]:
        state.observe(row)
    assert len(state.past["momentum_london"]) == 14
    for row in rows[14*240:14*240+30]:
        state.observe(row)
    now = opening+timedelta(minutes=30)
    profile = state.snapshot(now)
    assert len(profile["training_sessions"]) == 14
    assert all(day < opening for day in profile["training_sessions"])
    assert profile["sigma"] == pytest.approx(.0004/1.1)
    result = app.evaluate_candles(rows[14*240:14*240+30], dict(now=now, pair="EURUSD",
        strategy=model.STRATEGY, session_profile=profile, htf_candles=[], news_events=[],
        review_setup=lambda _: dict(verdict="approve", conviction=1., reasoning="test"),
        risk_state=dict(equity=10000, drawdown_state=dict(daily_dd_pct=0, overall_dd_pct=0),
                        trades_today=0, consecutive_losses_today=0, open_positions=[])))
    assert result.approved
    assert result.signal["take_profit"] is None
    assert result.setup["entry"] > result.setup["stop_loss"]
    assert result.signal["killzone"] == "momentum_london"


def test_missing_bars_do_not_masquerade_as_complete_sessions_and_duplicates_rejected():
    rows, opening = sessions()
    state = model.SessionHistory()
    for i, row in enumerate(rows[:14*240+30]):
        if i != 75:
            state.observe(row)
    assert len(state.past["momentum_london"]) == 13
    assert state.snapshot(opening+timedelta(minutes=30)) is None
    with pytest.raises(ValueError, match="chronological"):
        state.observe(rows[14*240+29])


def test_extended_session_policy_is_explicit_and_preserves_hard_risk_guards():
    now = datetime(2024, 1, 23, 10, tzinfo=timezone.utc)
    account = dict(equity=10000, pair="EURUSD", direction="buy", stop_loss_pips=20,
                   pip_value_per_lot=10, session_policy=model.STRATEGY)
    score, dd = dict(score=10, reason=None), dict(daily_dd_pct=0, overall_dd_pct=0)
    assert evaluate_signal(score, dd, account, [], 0, 0, now)["approved"]
    assert evaluate_signal(score, dd, {k:v for k,v in account.items() if k != "session_policy"},
                           [], 0, 0, now)["reason"] == "outside_killzone"
    assert evaluate_signal(score, dict(daily_dd_pct=.0325, overall_dd_pct=0),
                           account, [], 0, 0, now)["reason"] == "hard_daily"
    assert evaluate_signal(score, dd, account, [], 4, 0, now)["reason"] == "max_trades_today"


def test_sessions_follow_local_dst_and_do_not_open_on_weekends():
    winter = datetime(2026, 1, 12, 8, tzinfo=timezone.utc)
    summer = datetime(2026, 7, 13, 7, tzinfo=timezone.utc)
    assert model.session_at(winter) == model.session_at(summer) == "momentum_london"
    assert model.bounds("momentum_london", winter)[0].hour == 8
    assert model.bounds("momentum_london", summer)[0].hour == 7
    assert model.session_at(datetime(2026, 10, 18, 8, tzinfo=timezone.utc)) is None
    with pytest.raises(ValueError):
        model.session_at(datetime(2026, 1, 12, 8))
