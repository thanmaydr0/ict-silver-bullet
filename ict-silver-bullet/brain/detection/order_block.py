"""Last opposite-colour candle preceding a market structure shift."""

from .swings import _check_bias, _value


def find_order_block(candles: list, mss_index: int, bias: str):
    """Return the original candle from at most ten positions before the MSS.

    ``mss_index`` is a candle's external index, as returned by detect_mss.
    """
    _check_bias(bias)
    position = next((i for i, c in enumerate(candles)
                     if _value(c, "index") == mss_index), None)
    if position is None:
        return None
    for candle in reversed(candles[max(0, position - 10):position]):
        open_, close = _value(candle, "open"), _value(candle, "close")
        opposite = close < open_ if bias == "bullish" else close > open_
        if opposite:
            return candle
    return None
