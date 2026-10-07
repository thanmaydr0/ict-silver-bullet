import pytest

from brain.detection.fvg import detect_fvg, is_mitigated


@pytest.mark.parametrize("direction,rows,top,bottom", [
    ("bullish", [(9, 10, 8, 9), (9, 14, 9, 13), (13, 15, 12, 14)], 12, 10),
    ("bearish", [(14, 15, 12, 13), (13, 14, 8, 9), (9, 10, 7, 8)], 12, 10),
])
def test_clear_gap(candles, direction, rows, top, bottom):
    assert detect_fvg(candles(rows), direction) == [
        {"top": top, "bottom": bottom, "index": 1, "type": direction}]


@pytest.mark.parametrize("direction,rows,partial,full", [
    ("bullish", [(9, 10, 8, 9), (9, 14, 9, 13), (13, 15, 12, 14)],
     (13, 14, 11, 12), (12, 13, 10, 11)),
    ("bearish", [(14, 15, 12, 13), (13, 14, 8, 9), (9, 10, 7, 8)],
     (9, 11, 8, 10), (10, 12, 9, 11)),
])
def test_only_full_fill_mitigates(candles, direction, rows, partial, full):
    gap = detect_fvg(candles(rows), direction)[0]
    assert not is_mitigated(gap, candles(rows + [partial]))
    assert gap in detect_fvg(candles(rows + [partial]), direction)
    assert is_mitigated(gap, candles(rows + [partial, full]))
    assert gap not in detect_fvg(candles(rows + [partial, full]), direction)


def test_touching_formation_is_not_gap(candles):
    bars = candles([(9, 10, 8, 9), (10, 12, 9, 11), (11, 13, 10, 12)])
    assert detect_fvg(bars, "bullish") == []
    assert detect_fvg(bars, "bearish") == []
    assert detect_fvg(bars[:2], "bullish") == []
    with pytest.raises(ValueError):
        detect_fvg(bars, "neutral")
