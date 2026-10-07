"""Three-candle fair value gaps, excluding fully mitigated gaps."""

from .swings import _check_bias, _value


def is_mitigated(gap: dict, candles: list) -> bool:
    """Full fill means touching bottom (bullish) or top (bearish).

    Partial fills remain active. Only candles after candle 3 can mitigate;
    ``gap['index']`` is the middle candle's list offset, not its external index.
    This full-fill definition is a judgment call supplied by the task.
    """
    _check_bias(gap["type"])
    # NOTE: Candle 3 forms the gap; mitigation begins at the following candle.
    for candle in candles[gap["index"] + 2:]:
        if gap["type"] == "bullish" and _value(candle, "low") <= gap["bottom"]:
            return True
        if gap["type"] == "bearish" and _value(candle, "high") >= gap["top"]:
            return True
    return False


def detect_fvg(candles: list, direction: str) -> list[dict]:
    """Apply strict candle-1/candle-3 separation, exactly using list offsets."""
    _check_bias(direction)
    gaps = []
    for i in range(1, len(candles) - 1):
        c1, c3 = candles[i - 1], candles[i + 1]
        if direction == "bullish" and _value(c1, "high") < _value(c3, "low"):
            gaps.append({"top": _value(c3, "low"), "bottom": _value(c1, "high"),
                         "index": i, "type": "bullish"})
        elif direction == "bearish" and _value(c1, "low") > _value(c3, "high"):
            gaps.append({"top": _value(c1, "low"), "bottom": _value(c3, "high"),
                         "index": i, "type": "bearish"})
    return [gap for gap in gaps if not is_mitigated(gap, candles)]
