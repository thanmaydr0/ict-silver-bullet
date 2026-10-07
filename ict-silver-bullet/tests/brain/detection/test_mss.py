import pytest

from brain.detection.mss import detect_mss
from brain.detection.swings import SwingPoint


@pytest.mark.parametrize("bias,kind,level,rows", [
    ("bullish", "high", 10, [(9, 10, 8, 9), (9, 10, 8, 10), (9, 13, 8, 12)]),
    ("bearish", "low", 10, [(11, 12, 10, 11), (11, 12, 10, 10), (11, 12, 7, 8)]),
])
def test_strong_first_closing_break(candles, bias, kind, level, rows):
    assert detect_mss(candles(rows), [SwingPoint(100, level, kind)], bias, 2) == {
        "index": 120, "level": level, "displacement": 3}


@pytest.mark.parametrize("bias,kind,rows", [
    ("bullish", "high", [(9, 10, 8, 9), (10, 12, 9, 11), (9, 15, 8, 14)]),
    ("bearish", "low", [(11, 12, 10, 11), (10, 11, 8, 9), (11, 12, 5, 6)]),
])
def test_weak_first_break_filters_later_strong_break(candles, bias, kind, rows):
    assert detect_mss(candles(rows), [SwingPoint(100, 10, kind)], bias, 2) is None


def test_no_swing_missing_swing_or_wick_only(candles):
    bars = candles([(9, 10, 8, 9), (9, 12, 8, 10)])
    assert detect_mss(bars, [], "bullish", 1) is None
    assert detect_mss(bars, [SwingPoint(90, 10, "high")], "bullish", 1) is None
    assert detect_mss(bars, [SwingPoint(100, 10, "high")], "bullish", 1) is None


@pytest.mark.parametrize("atr", [0, -1, float("nan"), float("inf")])
def test_invalid_atr(atr):
    with pytest.raises(ValueError):
        detect_mss([], [], "bullish", atr)
