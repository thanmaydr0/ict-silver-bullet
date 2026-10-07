from datetime import datetime, timedelta, timezone

import pytest

from brain.detection.liquidity import detect_liquidity_sweep, find_nearest_liquidity_pool


@pytest.mark.parametrize("direction,row,extreme", [
    ("sell_side", (9, 12, 8, 9.5), 12), ("buy_side", (11, 12, 8, 10.5), 8),
])
def test_genuine_sweep(candles, direction, row, extreme):
    assert detect_liquidity_sweep(candles([row]), 10, direction) == {
        "index": 100, "wick_extreme": extreme}


@pytest.mark.parametrize("direction,row", [
    ("sell_side", (9, 12, 8, 11)), ("buy_side", (11, 12, 8, 9)),
    ("sell_side", (11, 12, 8, 9)), ("buy_side", (9, 12, 8, 11)),
    ("sell_side", (9, 12, 8, 10)), ("buy_side", (11, 12, 8, 10)),
    ("sell_side", (9, 10, 8, 9)), ("buy_side", (11, 12, 10, 11)),
])
def test_breakout_body_breach_boundary_and_touch_are_not_sweeps(candles, direction, row):
    assert detect_liquidity_sweep(candles([row]), 10, direction) is None


def test_last_five_and_most_recent(candles):
    bars = candles([(9, 12, 8, 9)] + [(9, 9.5, 8, 9)] * 5)
    assert detect_liquidity_sweep(bars, 10, "sell_side") is None
    bars = candles([(9, 12, 8, 9), (9, 13, 8, 9)])
    assert detect_liquidity_sweep(bars, 10, "sell_side")["index"] == 110
    assert detect_liquidity_sweep([], 10, "sell_side") is None
    with pytest.raises(ValueError):
        detect_liquidity_sweep(bars, 10, "bullish")


@pytest.mark.parametrize("bias,rows,level,kind", [
    ("bullish", [(100, 110, 90, 100), (100, 108, 92, 100), (100, 105, 95, 100)], 108, "high"),
    ("bearish", [(100, 110, 90, 100), (100, 108, 92, 100), (100, 105, 95, 100)], 92, "low"),
])
def test_nearest_prior_day(candles, bias, rows, level, kind):
    assert find_nearest_liquidity_pool(candles(rows, [0, 1, 2]), bias) == {
        "level": level, "type": kind, "index": 110, "source": "prior_day"}


@pytest.mark.parametrize("bias,rows,level,kind,source", [
    ("bullish", [(100, 110, 94, 100), (100, 105, 95, 100),
                 (100, 110.005, 96, 100), (100, 104, 97, 100)],
     110.005, "high", "equal_highs"),
    ("bearish", [(100, 106, 90, 100), (100, 105, 95, 100),
                 (100, 104, 89.995, 100), (100, 103, 97, 100)],
     89.995, "low", "equal_lows"),
])
def test_equal_extrema_cluster(candles, bias, rows, level, kind, source):
    assert find_nearest_liquidity_pool(candles(rows), bias) == {
        "level": level, "type": kind, "index": 120, "source": source}


@pytest.mark.parametrize("bias,rows", [
    ("bullish", [(100, 110, 90, 100), (100, 110, 91, 100), (100, 111, 92, 100)]),
    ("bearish", [(100, 110, 90, 100), (100, 109, 90, 100), (100, 108, 89, 100)]),
])
def test_tapped_day_and_cluster_are_excluded(candles, bias, rows):
    assert find_nearest_liquidity_pool(candles(rows, [0, 0, 1]), bias) is None


def test_tolerance_lookback_and_empty(candles):
    bars = candles([(100, 110, 94, 100), (100, 110.02, 95, 100), (100, 104, 96, 100)])
    assert find_nearest_liquidity_pool(bars, "bullish") is None
    bars = candles([(100, 110, 94, 100), (100, 110, 95, 100), (100, 104, 96, 100)])
    assert find_nearest_liquidity_pool(bars, "bullish", 2) is None
    assert find_nearest_liquidity_pool([], "bullish") is None
    with pytest.raises(ValueError):
        find_nearest_liquidity_pool([], "neutral")
    with pytest.raises(ValueError):
        find_nearest_liquidity_pool([], "bullish", 0)


def test_utc_boundaries_and_timestamp_forms():
    offset = timezone(timedelta(hours=5, minutes=30))
    bars = [dict(index=100, open=100, high=110, low=90, close=100,
                 ts=datetime(2026, 1, 2, 1, tzinfo=offset)),
            dict(index=110, open=100, high=105, low=95, close=100,
                 ts="2026-01-02T00:30:00Z")]
    assert find_nearest_liquidity_pool(bars, "bullish")["level"] == 110
    bars[-1]["ts"] = datetime(2026, 1, 2, 0, 30, tzinfo=timezone.utc).timestamp()
    assert find_nearest_liquidity_pool(bars, "bullish")["level"] == 110
    bars[-1]["ts"] = datetime(2026, 1, 2)
    with pytest.raises(ValueError, match="aware"):
        find_nearest_liquidity_pool(bars, "bullish")
