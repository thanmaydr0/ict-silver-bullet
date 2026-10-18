"""Tune thresholds on earlier history, then validate one choice exactly once.

Usage: python -m backtest.walk_forward --split 2025-01-01T00:00:00Z
"""

import argparse
from dataclasses import asdict, replace
from datetime import timedelta
import math
from pathlib import Path

from backtest.replay import (CandleHistory, ResearchConfig, load_history, normalize_history,
                             replay, research_replay, timestamp, write_report)
from brain.logging_setup import setup_logging


def split_history(rows, split):
    """Disjoint split by candle availability, not candle open timestamp.

    Bars closing exactly at the boundary belong to validation. A tune decision
    must never consume a bar closing at or after the validation boundary.
    """
    boundary = timestamp(split)
    tune, validate = [], []
    for row in normalize_history(rows):
        available = row["ts"] + timedelta(minutes=1 if row["timeframe"] == "M1" else 60)
        (tune if available < boundary else validate).append(row)
    return CandleHistory(tune), CandleHistory(validate)


def walk_forward(rows, split, *, thresholds=(7, 8, 9, 10), output_dir=None,
                 llm=False, review_setup=None, min_tune_trades=30):
    """Choose highest tune expectancy (ties prefer higher threshold).

    Validation starts with a fresh account. Tune candles warm indicators only.
    LLM tuning/validation, when requested, is a separate experiment.
    """
    tune, validate = split_history(rows, split)
    thresholds = tuple(thresholds)
    if not tune or not validate or not thresholds or min_tune_trades < 1:
        raise ValueError("nonempty tune/validate periods and thresholds required")
    output = Path(output_dir) if output_dir else None
    sweep = []
    for threshold in thresholds:
        report = replay(tune, threshold=threshold, llm=llm, review_setup=review_setup, collect_decisions=False,
                        results_path=output / f"tune_{threshold}.jsonl" if output else None)
        sweep.append(dict(threshold=threshold, effective_threshold=report["effective_threshold"],
                          **report["metrics"]))
    # NOTE: A least-negative result is not an acceptable strategy. Require a
    # positive tune expectancy and a declared sample floor before validation.
    eligible = [r for r in sweep if r["trades"] >= min_tune_trades and r["expectancy_r"] > 0]
    if not eligible:
        result = dict(split=timestamp(split), sweep=sweep, chosen_threshold=None,
                      validation=None, reason="no_closed_tune_trades" if not any(r["trades"] for r in sweep)
                      else "no_profitable_threshold_with_sufficient_trades", news_policy="skipped")
    else:
        chosen = max(eligible, key=lambda r: (r["expectancy_r"], r["effective_threshold"], r["threshold"]))
        validation = replay(CandleHistory(tune + validate), threshold=chosen["threshold"],
                            decision_start=split, llm=llm, review_setup=review_setup, collect_decisions=False,
                            results_path=output / "validate.jsonl" if output else None)
        result = dict(split=timestamp(split), sweep=sweep, chosen_threshold=chosen["threshold"],
                      validation={k: v for k, v in validation.items() if k != "decisions"},
                      selection="highest tune expectancy; ties prefer higher threshold",
                      news_policy="skipped: historical news unavailable")
    if output:
        write_report(output / "walk_forward.json", result)
    return result


def target_feasibility(risk_pct=.0025, trades_per_day=4, trading_days=20, target=.10):
    """Constant-return illustration, not an expectation or guaranteed floor."""
    if not all(math.isfinite(v) and v > 0 for v in (risk_pct, trades_per_day, trading_days, target)):
        raise ValueError("target assumptions must be finite and positive")
    trades = trades_per_day * trading_days
    required = math.expm1(math.log1p(target) / trades) / risk_pct
    return dict(target_pct=target * 100, risk_pct=risk_pct, illustrative_trades=trades,
                required_constant_net_r=required,
                interpretation="constant net R at every trade; not a return forecast; fewer trades or variability change requirements")


def screen_research(rows, split, *, output_dir=None, config=None, min_tune_trades=100,
                    min_complete_months=6, candidate_providers=None):
    """Three declared hypotheses, no tuning on later candles, no live promotion.

    All previously examined history is development evidence. The later slice
    is a retrospective diagnostic, not a new untouched forward test.
    """
    config = config or ResearchConfig()
    tune, later = split_history(rows, split)
    if not tune or not later or min_tune_trades < 1 or min_complete_months < 1:
        raise ValueError("research requires nonempty periods and positive sample floors")
    output = Path(output_dir) if output_dir else None
    experiments = (("ict_2r", "ict_2r", replace(config, reward_r=2.)),
                   ("breakout_1_5r", "breakout_pullback", replace(config, reward_r=1.5)),
                   ("breakout_2r", "breakout_pullback", replace(config, reward_r=2.)),
                   ("failed_breakout_2r", "failed_breakout", replace(config, reward_r=2.)))
    specification = dict(split=timestamp(split), experiments=[dict(name=n, strategy=s, config=asdict(c)) for n, s, c in experiments],
                         min_tune_trades=min_tune_trades, min_complete_months=min_complete_months,
                         data_status="previously examined history: development only", feasibility=target_feasibility(config.risk_pct))
    if output:
        write_report(output / "frozen_specification.json", specification)
    sweep = []
    for name, strategy, settings in experiments:
        report = research_replay(tune, strategy=strategy, config=settings,
                                 candidate_provider=(candidate_providers or {}).get(strategy),
                                 results_path=output / f"{name}_tune.jsonl" if output else None)
        if output:
            write_report(output / f"{name}_tune.json", report)
        sweep.append(dict(name=name, strategy=strategy, config=asdict(settings), metrics=report["metrics"],
                          monthly=report["monthly"], max_drawdown_pct=report["max_drawdown_pct"], halted=report["halted"]))
    eligible = [r for r in sweep if r["metrics"]["trades"] >= min_tune_trades and
                r["metrics"]["expectancy_r"] > 0 and r["monthly"]["complete_months"] >= min_complete_months and not r["halted"]]
    result = dict(specification=specification, sweep=sweep, chosen=None, validation=None,
                  promotion_passed=False, live_eligible=False,
                  reason="no_positive_net_tune_candidate_with_sufficient_evidence")
    if eligible:
        # NOTE: The monthly target is assessed after selection; do not choose
        # a model merely because its most favorable month happened to hit 10%.
        chosen = max(eligible, key=lambda r: r["metrics"]["expectancy_r"])
        settings = ResearchConfig(**chosen["config"])
        validation = research_replay(CandleHistory(tune + later), strategy=chosen["strategy"], config=settings,
                                     decision_start=split,
                                     candidate_provider=(candidate_providers or {}).get(chosen["strategy"]),
                                     results_path=output / "later_development.jsonl" if output else None)
        result.update(chosen=chosen["name"], validation={k: v for k, v in validation.items() if k != "trades"},
                      reason="retrospective_screen_only; real-tick, cost-stress and fresh forward validation still required")
        if output:
            write_report(output / "later_development.json", validation)
    if output:
        write_report(output / "research_screen.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", required=True)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "results" / "walk_forward")
    parser.add_argument("--thresholds", default="7,8,9,10")
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--research", action="store_true")
    parser.add_argument("--risk-pct", type=float, default=.0025)
    parser.add_argument("--spread-pips", type=float, default=.5)
    parser.add_argument("--slippage-pips", type=float, default=.1)
    parser.add_argument("--commission-per-lot", type=float, default=7.)
    args = parser.parse_args()
    logger = setup_logging("walk_forward")
    rows = load_history(args.data_dir)
    if args.research:
        if args.llm:
            parser.error("research screening is mechanical only")
        config = ResearchConfig(risk_pct=args.risk_pct, spread_pips=args.spread_pips,
                                slippage_pips=args.slippage_pips, commission_per_lot=args.commission_per_lot)
        result = screen_research(rows, args.split, output_dir=args.output_dir / "research", config=config)
        logger.info("Research selected=%s; %s", result["chosen"], result["reason"])
        return
    for llm in ([False, True] if args.llm else [False]):
        result = walk_forward(rows, args.split, thresholds=tuple(int(t) for t in args.thresholds.split(",")),
                              output_dir=args.output_dir / ("mechanical_llm" if llm else "mechanical"), llm=llm)
        logger.info("Chosen threshold=%s; validation=%s", result["chosen_threshold"],
                    result["validation"]["metrics"] if result["validation"] else result["reason"])


if __name__ == "__main__":
    main()
