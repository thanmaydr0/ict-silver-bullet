"""Wick sweeps and nearest untapped UTC-day/equal-extrema liquidity."""

from datetime import datetime
from zoneinfo import ZoneInfo

from .swings import _check_bias, _value

UTC = ZoneInfo("UTC")


def detect_liquidity_sweep(candles: list, pool_level: float, direction: str) -> dict | None:
    """Return the most recent sweep in the last five candles.

    Task nomenclature: sell_side sweeps above a high; buy_side below a low.
    """
    if direction not in ("sell_side", "buy_side"):
        raise ValueError("direction must be 'sell_side' or 'buy_side'")
    for candle in reversed(candles[-5:]):
        open_, close = _value(candle, "open"), _value(candle, "close")
        # NOTE: Require the entire body inside the pool so the breach is a wick.
        if direction == "sell_side":
            extreme = _value(candle, "high")
            sweep = extreme > pool_level and max(open_, close) < pool_level
        else:
            extreme = _value(candle, "low")
            sweep = extreme < pool_level and min(open_, close) > pool_level
        if sweep:
            return {"index": _value(candle, "index"), "wick_extreme": extreme}
    return None


def _utc_day(ts):
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    elif isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts, UTC)
    if not isinstance(ts, datetime) or ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("ts must be an aware datetime, ISO timestamp, or Unix seconds")
    return ts.astimezone(UTC).date()


def find_nearest_liquidity_pool(candles: list, bias: str, lookback: int = 50) -> dict | None:
    """Find the closest active level beyond the latest close in the bias direction.

    Returns level, type ('high'/'low'), index (last forming candle), and source
    ('prior_day'/'equal_highs'/'equal_lows'). Only supplied history is used.
    Equal extrema differ by at most 0.01% (one basis point) of their price;
    the outermost cluster extreme is the pool level. Day boundaries are UTC.
    """
    _check_bias(bias)
    if not isinstance(lookback, int) or isinstance(lookback, bool) or lookback < 1:
        raise ValueError("lookback must be a positive integer")
    history = candles[-lookback:]
    if not history:
        return None
    high_side = bias == "bullish"
    field = "high" if high_side else "low"
    current = _value(history[-1], "close")
    days = [_utc_day(_value(c, "ts")) for c in history]
    candidates = []

    def add(level, formed_at, source):
        if not (level > current if high_side else level < current):
            return
        # NOTE: A later touch counts as tapped, even without a breakout.
        if any((_value(c, field) >= level if high_side else _value(c, field) <= level)
               for c in history[formed_at + 1:]):
            return
        candidates.append({"level": level, "type": field,
                           "index": _value(history[formed_at], "index"), "source": source})

    # NOTE: Session hours are unspecified; use completed UTC days in available history.
    for day in sorted(set(days[:-1])):
        if day >= days[-1]:
            continue
        positions = [i for i, value in enumerate(days) if value == day]
        values = [_value(history[i], field) for i in positions]
        level = max(values) if high_side else min(values)
        add(level, positions[-1], "prior_day")

    # NOTE: Require a tight cluster (whole range within tolerance), not chained matches.
    for i in range(len(history) - 1):
        values = [_value(history[i], field)]
        for j in range(i + 1, len(history)):
            value = _value(history[j], field)
            trial = values + [value]
            tolerance = max(abs(v) for v in trial) * 0.0001
            if max(trial) - min(trial) <= tolerance:
                values = trial
                level = max(values) if high_side else min(values)
                add(level, j, "equal_highs" if high_side else "equal_lows")
            elif (value > max(values) if high_side else value < min(values)):
                # An intervening breach invalidates the earlier cluster anchor.
                break
    return min(candidates, key=lambda pool: abs(pool["level"] - current), default=None)
