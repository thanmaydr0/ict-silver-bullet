"""Fractal pivots for chronological object or dictionary candles."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


def _value(candle: Any, field: str) -> Any:
    return candle[field] if isinstance(candle, Mapping) else getattr(candle, field)


def _check_bias(bias: str) -> None:
    if bias not in ("bullish", "bearish"):
        raise ValueError("bias must be 'bullish' or 'bearish'")


@dataclass(frozen=True)
class SwingPoint:
    """A pivot using the candle's external index and timestamp."""

    index: int
    price: float
    type: str
    ts: Any = None


def find_swing_points(candles: list, lookback: int = 5) -> list[SwingPoint]:
    """Return confirmed pivots, in candle order, with complete flanking windows."""
    if not isinstance(lookback, int) or isinstance(lookback, bool) or lookback < 1:
        raise ValueError("lookback must be a positive integer")
    swings = []
    for i in range(lookback, len(candles) - lookback):
        candle = candles[i]
        neighbors = candles[i - lookback:i] + candles[i + 1:i + lookback + 1]
        # NOTE: Require unique extrema; tied highs/lows are liquidity, not pivots.
        for field, kind, comparator in (("high", "high", lambda a, b: a > b),
                                        ("low", "low", lambda a, b: a < b)):
            price = _value(candle, field)
            if all(comparator(price, _value(other, field)) for other in neighbors):
                swings.append(SwingPoint(_value(candle, "index"), price, kind,
                                         _value(candle, "ts")))
    return swings


def get_last_swing_against(swings: list[SwingPoint], bias: str) -> SwingPoint | None:
    """Bullish bias must break a high; bearish bias must break a low."""
    _check_bias(bias)
    kind = "high" if bias == "bullish" else "low"
    return next((swing for swing in reversed(swings) if swing.type == kind), None)
