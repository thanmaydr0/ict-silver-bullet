from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from brain.risk.circuit_breakers import (
    DrawdownState, check_breakers, trading_day_start, SOFT_DAILY_DD_PCT,
    HARD_DAILY_DD_PCT, SOFT_OVERALL_DD_PCT, HARD_OVERALL_DD_PCT,
)
from brain.risk.engine import evaluate_signal, is_correlated_exposure
from brain.risk.position_sizing import calculate_lot_size

NY = ZoneInfo("America/New_York")


@pytest.mark.parametrize("key,threshold,axis,action", [
    ("soft_daily", SOFT_DAILY_DD_PCT, 0, "reduce_size_50pct"),
    ("hard_daily", HARD_DAILY_DD_PCT, 0, "close_all_disable_new"),
    ("soft_overall", SOFT_OVERALL_DD_PCT, 1, "reduce_size_50pct_and_raise_min_score_to_9"),
    ("hard_overall", HARD_OVERALL_DD_PCT, 1, "halt_require_manual_review"),
])
def test_breaker_thresholds(key, threshold, axis, action):
    values = [0, 0]
    values[axis] = threshold - 1e-8
    assert key not in check_breakers(*values)
    values[axis] = threshold
    assert check_breakers(*values)[key] == action
    values[axis] += 1e-8
    assert check_breakers(*values)[key] == action


def test_all_breakers_and_profits():
    assert set(check_breakers(.04, .06)) == {"soft_daily", "hard_daily", "soft_overall", "hard_overall"}
    assert check_breakers(-.01, 0) == {}


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_invalid_drawdowns(value):
    with pytest.raises(ValueError):
        check_breakers(value, 0)


def test_floating_equity_and_trailing_peak():
    now = datetime(2026, 7, 1, 10, tzinfo=NY)
    state = DrawdownState(10000, 10000, day_start=now)
    state.update(11000, 10000, now)
    state.update(10450, 10000, now)
    assert state.daily_dd_pct == pytest.approx(-.045)
    assert state.overall_dd_pct == pytest.approx(.05)
    state.update(9700, 10000, now)
    assert state.daily_dd_pct == pytest.approx(.03)
    assert state.high_water_mark_equity == 11000
    state.update(9750, 9800, now.replace(hour=17))
    assert state.day_start_balance == 9800
    assert state.daily_dd_pct == pytest.approx(50 / 9800)
    assert state.high_water_mark_equity == 11000


def test_restored_peak_and_initial_update():
    state = DrawdownState(10000, 10500, 11000)
    state.update(10400, 10000, datetime(2026, 7, 1, tzinfo=timezone.utc))
    assert state.high_water_mark_equity == 11000
    assert state.day_start_balance == 10000
    with pytest.raises(ValueError):
        state.update(10000, 10000, datetime(2026, 6, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize("values", [(0, 100), (100, float("nan")), (100, 100, 0)])
def test_invalid_state(values):
    with pytest.raises(ValueError):
        DrawdownState(*values)


@pytest.mark.parametrize("day,previous_hour,current_hour", [
    ((2026, 3, 8), 22, 21), ((2026, 11, 1), 21, 22),
])
def test_rollover_dst_and_boundary(day, previous_hour, current_hour):
    at = datetime(*day, 17, tzinfo=NY)
    previous = trading_day_start(at - timedelta(microseconds=1))
    current = trading_day_start(at)
    assert previous.hour == previous_hour
    assert current.hour == current_hour
    assert current == at.astimezone(timezone.utc)
    assert previous.date() == at.date() - timedelta(days=1)


def test_naive_rollover():
    with pytest.raises(ValueError):
        trading_day_start(datetime(2026, 1, 1))


@pytest.mark.parametrize("equity,risk,stop,pip,step,expected", [
    (10000, .0025, 10, 10, .01, .25), (10000, .005, 10, 10, .01, .5),
    (10000, .0025, 12, 10, .01, .2), (10000, .0025, 12, 10, .1, .2),
    (100, .0025, 50, 10, .01, 0), (10000, 0, 10, 10, .01, 0),
    (12000, .0025, 10, 10, .01, .3),
])
def test_sizing(equity, risk, stop, pip, step, expected):
    result = calculate_lot_size(equity, risk, stop, pip, step)
    assert result == expected
    assert result * stop * pip <= equity * risk + 1e-10


def test_default_risk():
    assert calculate_lot_size(10000, stop_loss_pips=10, pip_value_per_lot=10) == .25


@pytest.mark.parametrize("field,value", [
    ("account_equity", 0), ("risk_pct", -.01), ("risk_pct", 2),
    ("stop_loss_pips", 0), ("pip_value_per_lot", -1), ("lot_step", 0),
    ("account_equity", float("inf")), ("risk_pct", float("nan")), ("stop_loss_pips", None),
])
def test_invalid_sizing(field, value):
    args = dict(account_equity=10000, risk_pct=.0025, stop_loss_pips=10, pip_value_per_lot=10, lot_step=.01)
    args[field] = value
    with pytest.raises(ValueError):
        calculate_lot_size(**args)


def evaluate(**changes):
    args = dict(setup_score_result={"score": 8, "reason": None},
                drawdown_state={"daily_dd_pct": 0, "overall_dd_pct": 0},
                account_state={"equity": 10000, "pair": "EURUSD", "direction": "buy",
                               "stop_loss_pips": 10, "pip_value_per_lot": 10},
                open_positions=[], trades_today=0, consecutive_losses_today=0,
                now=datetime(2026, 7, 1, 10, 30, tzinfo=NY))
    args.update(changes)
    return evaluate_signal(**args)


def test_approval_and_state_object():
    assert evaluate(drawdown_state=DrawdownState(10000, 10000)) == {
        "approved": True, "lot_size": .25, "reason": "approved"}


@pytest.mark.parametrize("score,losses,approved", [(7, 0, False), (8, 1, True),
                                                   (8, 2, False), (9, 2, True), (9, 3, True)])
def test_loss_streak(score, losses, approved):
    result = evaluate(setup_score_result={"score": score}, consecutive_losses_today=losses)
    assert result["approved"] is approved
    if not approved:
        assert result["reason"] == "score_below_minimum"


@pytest.mark.parametrize("daily,overall,score,approved,size", [
    (.025, 0, 8, True, .12), (0, .045, 8, False, None),
    (0, .045, 9, True, .12), (.025, .045, 9, True, .12),
])
def test_soft_interactions(daily, overall, score, approved, size):
    result = evaluate(drawdown_state={"daily_dd_pct": daily, "overall_dd_pct": overall},
                      setup_score_result={"score": score})
    assert result["approved"] is approved
    assert result["lot_size"] == size


@pytest.mark.parametrize("daily,overall,reason", [(.0325, 0, "hard_daily"),
                                                (0, .0525, "hard_overall"), (.04, .06, "hard_overall")])
def test_hard_breaker_overrides_everything(daily, overall, reason):
    result = evaluate(drawdown_state={"daily_dd_pct": daily, "overall_dd_pct": overall},
                      setup_score_result={"score": 0, "reason": "news_blackout"},
                      trades_today=4, open_positions=[{}, {}], account_state={},
                      now=datetime(2026, 7, 3, 16, 55, tzinfo=NY))
    assert result == {"approved": False, "lot_size": None, "reason": reason}


@pytest.mark.parametrize("count,approved", [(3, True), (4, False), (5, False)])
def test_daily_trade_cap(count, approved):
    assert evaluate(trades_today=count)["approved"] is approved


def test_position_cap():
    assert evaluate(open_positions=[{}, {}])["reason"] == "max_open_positions"


@pytest.mark.parametrize("pair,direction,correlated", [
    ("GBPUSD", "buy", True), ("GBPUSD", "sell", False),
    ("EURJPY", "long", True), ("USDCAD", "sell", True),
    ("USDCAD", "buy", False), ("AUDJPY", "buy", False),
    ("EUR/USD", "short", False),
])
def test_currency_direction_heuristic(pair, direction, correlated):
    assert is_correlated_exposure([{"pair": "EURUSD", "direction": "long"}], pair, direction) is correlated


def test_correlated_rejection_and_unrelated_approval():
    assert evaluate(open_positions=[{"pair": "GBPUSD", "direction": "buy"}])["reason"] == "correlated_exposure"
    assert evaluate(open_positions=[{"symbol": "AUDJPY", "direction": "sell"}])["approved"]


@pytest.mark.parametrize("month,utc_hour", [(1, 21), (7, 20)])
def test_friday_guard_in_winter_and_summer(month, utc_hour):
    # Jan 2 and Jul 3 2026 are Fridays.
    day = 2 if month == 1 else 3
    for minute in (50, 59):
        result = evaluate(now=datetime(2026, month, day, utc_hour, minute, tzinfo=timezone.utc))
        assert result["reason"] == "friday_close_guard"
    assert evaluate(now=datetime(2026, month, day, utc_hour, 49, 59, tzinfo=timezone.utc))["reason"] == "outside_killzone"
    assert evaluate(now=datetime(2026, month, day, 17, tzinfo=NY))["reason"] == "friday_close_guard"


def test_news_outside_and_naive():
    assert evaluate(setup_score_result={"score": 10, "reason": "news_blackout"})["reason"] == "news_blackout"
    assert evaluate(now=datetime(2026, 7, 1, 11, tzinfo=NY))["reason"] == "outside_killzone"
    with pytest.raises(ValueError):
        evaluate(now=datetime(2026, 7, 1, 10))


@pytest.mark.parametrize("score", [-1, 11, float("nan"), "9"])
def test_invalid_scores(score):
    assert evaluate(setup_score_result={"score": score})["reason"] == "invalid_setup_score"


@pytest.mark.parametrize("account", [{}, {"equity": 100},
    {"equity": 10000, "pair": "BAD", "direction": "buy"},
    {"equity": 10000, "pair": "EURUSD", "direction": "invalid"}])
def test_invalid_account(account):
    assert evaluate(account_state=account)["reason"] == "invalid_account_state"


def test_too_small_lot_and_custom_step():
    account = dict(equity=100, pair="EURUSD", direction="buy", stop_loss_pips=100, pip_value_per_lot=10)
    assert evaluate(account_state=account)["reason"] == "lot_size_below_step"
    account.update(equity=10000, stop_loss_pips=10, risk_pct=.005, lot_step=.1)
    assert evaluate(account_state=account)["lot_size"] == .5


def test_setup_pair_fallback():
    account = dict(equity=10000, stop_loss_pips=10, pip_value_per_lot=10)
    assert evaluate(account_state=account, setup_score_result={"score": 8, "pair": "EURUSD", "direction": "buy"})["approved"]
