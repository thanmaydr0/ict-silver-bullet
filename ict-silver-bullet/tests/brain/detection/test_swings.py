import pytest

from brain.detection.swings import SwingPoint, find_swing_points, get_last_swing_against


def test_confirmed_high_and_low(candles):
    bars = candles([(5, 6, 4, 5), (5, 9, 3, 5), (5, 6, 4, 5),
                    (4, 5, 1, 4), (4, 6, 2, 4)])
    points = find_swing_points(bars, lookback=1)
    assert [(p.index, p.price, p.type) for p in points] == [
        (110, 9, "high"), (110, 3, "low"), (130, 1, "low")]
    assert points[0].ts is not None
    assert get_last_swing_against(points, "bullish") == points[0]
    assert get_last_swing_against(points, "bearish") == points[-1]


def test_default_lookback_and_edges(candles):
    bars = candles([(5, 6, 4, 5)] * 5 + [(5, 10, 1, 5)] + [(5, 6, 4, 5)] * 5)
    assert [(p.index, p.type) for p in find_swing_points(bars)] == [
        (150, "high"), (150, "low")]
    assert find_swing_points(bars[:10]) == []
    assert find_swing_points([]) == []


def test_tied_extrema_are_not_pivots(candles):
    assert find_swing_points(candles([(5, 6, 4, 5)] * 3), 1) == []


def test_last_swing_uses_chronological_order():
    points = [SwingPoint(4, 12, "high"), SwingPoint(2, 13, "high")]
    assert get_last_swing_against(points, "bullish") == points[-1]
    assert get_last_swing_against(points, "bearish") is None
    assert get_last_swing_against([], "bullish") is None
    with pytest.raises(ValueError):
        get_last_swing_against(points, "neutral")


@pytest.mark.parametrize("lookback", [0, -1, 1.5, True])
def test_invalid_lookback(lookback):
    with pytest.raises(ValueError):
        find_swing_points([], lookback)
