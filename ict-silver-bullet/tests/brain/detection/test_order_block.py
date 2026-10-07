import pytest

from brain.detection.order_block import find_order_block


@pytest.mark.parametrize("bias,rows", [
    ("bullish", [(5, 6, 3, 4), (4, 6, 3, 5), (5, 6, 2, 3), (3, 4, 2, 3), (3, 9, 2, 8)]),
    ("bearish", [(4, 6, 3, 5), (5, 6, 3, 4), (3, 6, 2, 5), (3, 4, 2, 3), (8, 9, 2, 3)]),
])
def test_last_opposite_candle_and_doji_exclusion(candles, bias, rows):
    bars = candles(rows)
    assert find_order_block(bars, 140, bias) is bars[2]
    assert find_order_block(bars, 999, bias) is None
    assert find_order_block(bars, 100, bias) is None


def test_ten_candle_window(candles):
    bars = candles([(5, 6, 3, 4)] + [(4, 6, 3, 5)] * 11)
    assert find_order_block(bars, 200, "bullish") is bars[0]
    assert find_order_block(bars, 210, "bullish") is None
    with pytest.raises(ValueError):
        find_order_block(bars, 210, "neutral")
