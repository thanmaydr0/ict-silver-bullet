"""Pure, injectable pre-trade guards. No broker or network calls."""

from datetime import time

from brain.risk.circuit_breakers import NEW_YORK, check_breakers
from brain.risk.position_sizing import DEFAULT_RISK_PCT, calculate_lot_size
from brain.scheduler.killzone_clock import current_killzone

FRIDAY_CLOSE = time(17)
FRIDAY_GAP_GUARD_START = time(16, 50)


def _exposure(pair, direction):
    pair = pair.upper().replace("/", "").replace("_", "")
    if len(pair) != 6 or not pair.isalpha():
        raise ValueError("pair must contain two three-letter currency codes")
    direction = direction.lower()
    if direction not in {"buy", "sell", "long", "short"}:
        raise ValueError("direction must be buy/sell or long/short")
    sign = 1 if direction in {"buy", "long"} else -1
    return {pair[:3]: sign, pair[3:]: -sign}


def is_correlated_exposure(open_positions, new_pair, new_direction) -> bool:
    """Detect shared currency exposure in the same direction, not covariance.

    A long buys base and sells quote; a short reverses that. Opposing shared
    currency legs do not count as correlated; they still consume a position slot.
    # NOTE: Conservatively reject a second aligned shared-currency exposure;
    # the two-position cap does not authorize doubling one currency risk unit.
    Positions use dictionaries with pair and direction keys (or symbol for pair).
    """
    proposed = _exposure(new_pair, new_direction)
    for position in open_positions:
        existing = _exposure(position.get("pair", position.get("symbol", "")), position["direction"])
        if any(existing.get(currency) == sign for currency, sign in proposed.items()):
            return True
    return False


def evaluate_signal(setup_score_result, drawdown_state, account_state, open_positions,
                    trades_today, consecutive_losses_today, now) -> dict:
    """Approve a signal or return a stable rejection reason.

    account_state is a dict with equity, pair, direction, stop_loss_pips,
    pip_value_per_lot and optional risk_pct/lot_step (defaults .0025/.01).
    drawdown_state exposes daily_dd_pct/overall_dd_pct (object or dictionary).
    Pair/direction may alternatively be supplied in setup_score_result.
    Soft breakers share one 50% reduction, floored only after reducing risk.
    """
    def reject(reason):
        return {"approved": False, "lot_size": None, "reason": reason}

    if isinstance(drawdown_state, dict):
        daily, overall = drawdown_state["daily_dd_pct"], drawdown_state["overall_dd_pct"]
    else:
        daily, overall = drawdown_state.daily_dd_pct, drawdown_state.overall_dd_pct
    breakers = check_breakers(daily, overall)
    if "hard_overall" in breakers:
        return reject("hard_overall")
    if "hard_daily" in breakers:
        return reject("hard_daily")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(NEW_YORK)
    if local.weekday() == 4 and FRIDAY_GAP_GUARD_START <= local.time():
        return reject("friday_close_guard")
    if current_killzone(now) is None:
        return reject("outside_killzone")
    if setup_score_result.get("reason"):
        return reject(setup_score_result["reason"])
    minimum = 9 if consecutive_losses_today >= 2 or "soft_overall" in breakers else 8
    score = setup_score_result.get("score", 0)
    if not isinstance(score, (int, float)) or not 0 <= score <= 10:
        return reject("invalid_setup_score")
    if score < minimum:
        return reject("score_below_minimum")
    if trades_today >= 4:
        return reject("max_trades_today")
    if len(open_positions) >= 2:
        return reject("max_open_positions")
    pair = account_state.get("pair", setup_score_result.get("pair"))
    direction = account_state.get("direction", setup_score_result.get("direction"))
    try:
        if is_correlated_exposure(open_positions, pair, direction):
            return reject("correlated_exposure")
        risk = account_state.get("risk_pct", DEFAULT_RISK_PCT)
        if "soft_daily" in breakers or "soft_overall" in breakers:
            risk *= 0.5
        size = calculate_lot_size(account_state["equity"], risk, account_state["stop_loss_pips"],
                                  account_state["pip_value_per_lot"], account_state.get("lot_step", 0.01))
    except (KeyError, ValueError, TypeError, AttributeError):
        return reject("invalid_account_state")
    if size <= 0:
        return reject("lot_size_below_step")
    return {"approved": True, "lot_size": size, "reason": "approved"}
