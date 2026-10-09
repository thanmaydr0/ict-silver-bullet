"""Permutation risk-of-ruin experiment on actual closed-trade R multiples.

Usage: python -m backtest.monte_carlo backtest/results/mechanical.json
The finite sequence is shuffled without replacement; unfinished paths are
reported separately. This estimates order risk conditional on observed trades,
not a guarantee about future returns or intra-trade floating drawdown.
"""

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import random

from backtest.replay import metrics, timestamp, write_report
from brain.logging_setup import setup_logging
from brain.risk.circuit_breakers import (
    DrawdownState, check_breakers,
    SOFT_DAILY_DD_PCT, HARD_DAILY_DD_PCT, SOFT_OVERALL_DD_PCT, HARD_OVERALL_DD_PCT,
)
from brain.risk.position_sizing import DEFAULT_RISK_PCT


def _quantile(values, fraction):
    index = (len(values) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def simulate(trades, *, simulations=10000, risk_pct=DEFAULT_RISK_PCT,
             target_pct=0.10, initial_equity=10000.0, trades_per_day=4, seed=0):
    """Shuffle outcomes into original chronological trading-day slots.

    Dictionaries must supply r_multiple, optionally confluence_score and
    opened_at/closed_at. Numeric R sequences use synthetic four-trade days.
    Soft breakers halve risk once; soft overall also requires score >=9.
    Hard stop is checked after each trade before target success.
    """
    if not isinstance(simulations, int) or simulations <= 0 or not isinstance(trades_per_day, int) or trades_per_day <= 0:
        raise ValueError("simulations and trades_per_day must be positive integers")
    if not all(math.isfinite(v) for v in (risk_pct, target_pct, initial_equity)) or not 0 < risk_pct <= 1 or target_pct <= 0 or initial_equity <= 0:
        raise ValueError("invalid equity, risk or target")
    outcomes = [dict(t) if isinstance(t, dict) else dict(r_multiple=float(t), confluence_score=10) for t in trades]
    if not outcomes:
        raise ValueError("at least one closed trade required")
    for trade in outcomes:
        if not math.isfinite(trade["r_multiple"]):
            raise ValueError("R multiples must be finite")
        trade.setdefault("confluence_score", 10)
    # NOTE: Missing historical scores use 10; supplied replay scores preserve
    # the soft-overall score gate. Numeric sequences use four-trade day slots.
    dated = [t.get("closed_at") or t.get("opened_at") for t in outcomes]
    if any(dated) and not all(dated):
        raise ValueError("timestamps must be provided for all trades or none")
    if all(dated):
        slots = sorted(timestamp(d) for d in dated)
    else:
        base = datetime(2024, 1, 2, 12, tzinfo=timezone.utc)
        slots = [base + timedelta(days=i // trades_per_day, minutes=i % trades_per_day) for i in range(len(outcomes))]
    rng = random.Random(seed)
    ruined = reached = skipped = 0
    drawdowns = []
    for _ in range(simulations):
        sequence = rng.sample(outcomes, len(outcomes))
        balance = peak = initial_equity
        state = DrawdownState(balance, balance)
        maximum = 0.0
        for now, trade in zip(slots, sequence):
            state.update(balance, balance, now)
            breakers = check_breakers(state.daily_dd_pct, state.overall_dd_pct)
            if "soft_overall" in breakers and trade["confluence_score"] < 9:
                skipped += 1
                continue
            size = risk_pct * (0.5 if "soft_daily" in breakers or "soft_overall" in breakers else 1)
            prior_balance = balance
            balance += balance * size * trade["r_multiple"]
            peak = max(peak, balance)
            state.update(balance, prior_balance, now)
            maximum = max(maximum, (peak - balance) / peak)
            breakers = check_breakers(state.daily_dd_pct, state.overall_dd_pct)
            if "hard_daily" in breakers or "hard_overall" in breakers:
                ruined += 1
                break
            if balance >= initial_equity * (1 + target_pct):
                reached += 1
                break
        drawdowns.append(maximum)
    ordered = sorted(drawdowns)
    return dict(simulations=simulations, seed=seed, risk_pct=risk_pct, target_pct=target_pct,
                risk_of_ruin_pct=100 * ruined / simulations, target_reached_pct=100 * reached / simulations,
                unfinished_pct=100 * (simulations - ruined - reached) / simulations,
                soft_overall_skipped_trades=skipped,
                observed_metrics=metrics(outcomes),
                thresholds=dict(soft_daily=SOFT_DAILY_DD_PCT, hard_daily=HARD_DAILY_DD_PCT,
                                soft_overall=SOFT_OVERALL_DD_PCT, hard_overall=HARD_OVERALL_DD_PCT),
                max_drawdown=dict(min=ordered[0], median=_quantile(ordered, .5),
                                  p95=_quantile(ordered, .95), p99=_quantile(ordered, .99),
                                  max=ordered[-1], samples=drawdowns),
                assumptions="actual R permutations; compounded equity risk; original day slots; closed-trade drawdown only; no costs",
                score_policy="missing scores assume 10; soft overall skips scores below 9")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "results" / "monte_carlo.json")
    parser.add_argument("--simulations", type=int, default=10000)
    parser.add_argument("--risk-pct", type=float, default=DEFAULT_RISK_PCT)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    logger = setup_logging("monte_carlo")
    report = json.loads(args.report.read_text(encoding="utf-8"))
    result = simulate(report["trades"], simulations=args.simulations, risk_pct=args.risk_pct, seed=args.seed)
    write_report(args.output, result)
    logger.info("Hard stop before 10%% target: %.2f%%; unfinished: %.2f%%; p95 max DD: %.2f%%",
                result["risk_of_ruin_pct"], result["unfinished_pct"], 100 * result["max_drawdown"]["p95"])


if __name__ == "__main__":
    main()
