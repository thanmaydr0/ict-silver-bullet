"""Causal M5 countertrend-sweep reclaim inside a confirmed H1 trend.

This is a testable trading hypothesis, not a calibrated return forecast. The
same detector is used by the live decision chain and the cost-aware replay.
"""

from datetime import timedelta

from brain.detection.swings import find_swing_points
from brain.pairs import pair_spec

STRATEGY = "trend_reclaim"
MIN_STOP_PIPS = 4.0
MAX_STOP_ATR = 3.0


def ema(values, period):
    result = sum(values[:period]) / period
    for value in values[period:]:
        result += 2 / (period + 1) * (value - result)
    return result


def completed_m5(candles):
    """Aggregate only five distinct, consecutive, UTC-aligned M1 bars."""
    buckets = {}
    for row in candles:
        start = row["ts"].replace(minute=row["ts"].minute // 5 * 5, second=0, microsecond=0)
        buckets.setdefault(start, {})[row["ts"]] = row
    bars = []
    for start, rows in sorted(buckets.items()):
        expected = [start + timedelta(minutes=i) for i in range(5)]
        if sorted(rows) != expected:
            continue
        part = [rows[ts] for ts in expected]
        bars.append(dict(ts=start, open=part[0]["open"], close=part[-1]["close"],
                         high=max(r["high"] for r in part), low=min(r["low"] for r in part)))
    return bars


def detect(candles, htf, pair, now):
    """Return a completed-M5 confirmation once, without pending limit entries."""
    if len(htf) < 53 or len(candles) < 90:
        return None
    if htf[-1]["ts"] + timedelta(hours=2) < now:
        return None
    bars = completed_m5(candles[-90:])
    if len(bars) < 18:
        return None
    window = bars[-18:]
    if any(b["ts"] - a["ts"] != timedelta(minutes=5) for a, b in zip(window, window[1:])):
        return None
    available = bars[-1]["ts"] + timedelta(minutes=5)
    if candles[-1]["ts"] + timedelta(minutes=1) != available or not (
            available <= now < available + timedelta(minutes=1)):
        return None
    closes = [r["close"] for r in htf]
    fast, previous_fast, slow = ema(closes, 20), ema(closes[:-3], 20), ema(closes, 50)
    buy = closes[-1] > fast > slow and fast > previous_fast
    sell = closes[-1] < fast < slow and fast < previous_fast
    if not buy and not sell:
        return None
    prior, sweep, confirmation = bars[-18:-2], bars[-2], bars[-1]
    ranges = [max(b["high"] - b["low"], abs(b["high"] - a["close"]),
                  abs(b["low"] - a["close"])) for a, b in zip(prior, prior[1:])]
    atr = sum(ranges[-14:]) / 14
    if atr <= 0:
        return None
    level = min(b["low"] for b in prior[-12:]) if buy else max(b["high"] for b in prior[-12:])
    swept = (sweep["low"] < level - .05 * atr and sweep["close"] > level if buy else
             sweep["high"] > level + .05 * atr and sweep["close"] < level)
    reclaimed = (confirmation["close"] > sweep["high"] and
                 confirmation["close"] > confirmation["open"] and
                 confirmation["low"] > sweep["low"] if buy else
                 confirmation["close"] < sweep["low"] and
                 confirmation["close"] < confirmation["open"] and
                 confirmation["high"] < sweep["high"])
    if not swept or not reclaimed:
        return None
    pip = pair_spec(pair, confirmation["close"])["pip_size"]
    sign = 1 if buy else -1
    entry = confirmation["close"]
    extreme = sweep["low"] if buy else sweep["high"]
    stop = extreme - sign * max(pip, .1 * atr)
    distance = sign * (entry - stop)
    if not MIN_STOP_PIPS * pip <= distance <= MAX_STOP_ATR * atr:
        return None
    target = entry + sign * 2 * distance
    # NOTE: The tested scalp is exactly 2R. Require space back to the prior M5
    # range boundary plus a confirmed H1 runner target, matching live contracts.
    boundary = max(b["high"] for b in prior[-12:]) if buy else min(b["low"] for b in prior[-12:])
    pools = [p.price for p in find_swing_points(htf, lookback=2)
             if p.type == ("high" if buy else "low") and sign * (p.price - target) > 0]
    if sign * (boundary - target) < 0 or not pools:
        return None
    travel = sum(abs(b - a) for a, b in zip(closes[-13:], closes[-12:]))
    efficiency = abs(closes[-1] - closes[-13]) / travel if travel else 0
    displacement = sign * (confirmation["close"] - confirmation["open"])
    # NOTE: These fixed rules are frozen hypotheses, not optimized settings:
    # 12-M5 range, 20/50 H1 EMA with three-hour slope, ATR14, 4pip..3ATR stop.
    # Optional efficiency/body factors permit an honest 8/9/10 tune sweep.
    return dict(strategy=STRATEGY, pair=pair, direction="buy" if buy else "sell",
        entry=entry, stop_loss=stop, take_profit=target, atr=atr,
        setup_id=sweep["ts"].isoformat(), htf_bias="bullish" if buy else "bearish",
        sweep=dict(ts=sweep["ts"], wick_extreme=extreme, level=level),
        confirmation=dict(ts=confirmation["ts"], displacement=displacement),
        opposing_level=boundary,
        evidence=dict(h1_fast_ema=fast, h1_slow_ema=slow, h1_previous_fast_ema=previous_fast,
                      h1_efficiency=efficiency, atr_m5=atr, range_boundary=boundary,
                      runner_target=min(pools) if buy else max(pools)),
        factors=dict(strategy=STRATEGY, inside_killzone=True, htf_trend_aligned=True,
                     countertrend_sweep_reclaimed=True, executable_2r_geometry=True,
                     efficient_htf_trend=efficiency >= .35,
                     strong_confirmation_body=displacement >= .5 * atr))
