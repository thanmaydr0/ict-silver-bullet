"""Market structure shifts confirmed by body displacement."""

import math

from .swings import _value, get_last_swing_against


def detect_mss(candles: list, swings: list, bias: str, atr: float) -> dict | None:
    """Return the first closing break after the opposing pivot, if strong."""
    if not math.isfinite(atr) or atr <= 0:
        raise ValueError("atr must be finite and positive")
    swing = get_last_swing_against(swings, bias)
    if swing is None:
        return None
    position = next((i for i, c in enumerate(candles)
                     if _value(c, "index") == swing.index), None)
    # NOTE: Without the pivot candle we cannot establish a later break safely.
    if position is None:
        return None
    for candle in candles[position + 1:]:
        close = _value(candle, "close")
        breaks = close > swing.price if bias == "bullish" else close < swing.price
        if breaks:
            displacement = abs(close - _value(candle, "open"))
            # NOTE: A weak first break filters the setup; do not accept a later one.
            if displacement < 1.5 * atr:
                return None
            return {"index": _value(candle, "index"), "level": swing.price,
                    "displacement": displacement}
    return None
