"""Deterministic confluence scoring with an audit record for every candidate."""

from brain.logging_setup import setup_logging

FACTOR_POINTS = {
    "htf_bias_aligned": 2,
    "inside_killzone": 2,
    "liquidity_sweep_before_mss": 2,
    "displacement_ge_1_5x_atr": 2,
    "fvg_present_unmitigated": 1,
    "entry_in_ote_zone": 1,
}


def score_setup(factors: dict, no_high_impact_news_next_15min: bool | None = None) -> dict:
    """Score truth-valued flags (including numeric 0/1).

    The news gate may be supplied separately or as a factors key. Missing gates
    fail closed. Outside the killzone no other factors are inspected or summed.
    """
    breakdown = dict.fromkeys(FACTOR_POINTS, 0)
    reason = None
    if not factors.get("inside_killzone", False):
        reason = "outside_killzone"
    else:
        breakdown = {name: points if factors.get(name, False) else 0
                     for name, points in FACTOR_POINTS.items()}
        if no_high_impact_news_next_15min is None:
            no_high_impact_news_next_15min = factors.get("no_high_impact_news_next_15min", False)
        if not no_high_impact_news_next_15min:
            reason = "news_blackout"
    result = {"score": 0 if reason else sum(breakdown.values()),
              "max_score": 10, "reason": reason, "breakdown": breakdown}
    setup_logging("confluence").info("Setup score=%s reason=%s breakdown=%s",
                                    result["score"], reason, breakdown)
    return result
