"""Tune thresholds on earlier history, then validate one choice exactly once.

Usage: python -m backtest.walk_forward --split 2025-01-01T00:00:00Z
"""

import argparse
from dataclasses import asdict, replace
from datetime import timedelta
import math
import random
from pathlib import Path

from backtest.replay import (CandleHistory, ResearchConfig, load_history, load_spread_profile, normalize_history,
                             replay, research_replay, session_momentum_replay, timestamp, write_report)
from brain.risk.circuit_breakers import trading_day_start
from brain.logging_setup import setup_logging


def _stress_config(config, multiplier=2):
    spread = config.spread_pips
    spread = ({pair: value * multiplier for pair, value in spread.items()}
              if isinstance(spread, dict) else spread * multiplier)
    return replace(config, spread_pips=spread,
                   slippage_pips=multiplier * config.slippage_pips,
                   commission_per_lot=multiplier * config.commission_per_lot,
                   historical_spread_multiplier=multiplier * config.historical_spread_multiplier)


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


def core_walk_forward(rows, split, *, output_dir=None, config=None,
                      thresholds=(8, 9, 10), min_tune_trades=100, min_tune_days=60,
                      bootstrap_samples=2000, simulations=10000):
    """Freeze the real core rules, sweep only early scores, diagnose one winner.

    Previously examined history remains development data. The later diagnostic
    and doubled-friction run cannot select or retune a model. No live promotion.
    """
    config = config or ResearchConfig(max_hold_minutes=30)
    tune, later = split_history(rows, split)
    thresholds = tuple(thresholds)
    if (not tune or not later or not thresholds or any(t not in (8, 9, 10) for t in thresholds)
            or min(min_tune_trades, min_tune_days, bootstrap_samples, simulations) < 1
            or config.reward_r != 2.):
        raise ValueError("nonempty periods, score thresholds 8..10, positive evidence floors and 2R required")
    output = Path(output_dir) if output_dir else None
    specification = dict(strategy="trend_reclaim", split=timestamp(split), thresholds=thresholds,
        config=asdict(config), min_tune_trades=min_tune_trades, min_tune_days=min_tune_days,
        bootstrap_samples=bootstrap_samples, simulations=simulations,
        data_status="previously examined history; later period is retrospective, not fresh out-of-sample",
        decision_model="shared brain.app.evaluate_candles with mechanical review bypass",
        execution_limits="M1 Bid OHLC; one scalp position; no live runner; next-open fill; time stop at session close",
        news_policy="skipped: historical calendar unavailable", live_eligible=False)
    if output:
        write_report(output / "frozen_specification.json", specification)
    sweep = []
    for threshold in thresholds:
        report = research_replay(tune, strategy="core", config=config, threshold=threshold,
            results_path=output / f"tune_{threshold}.jsonl" if output else None)
        if output:
            write_report(output / f"tune_{threshold}.json", report)
        interval = day_cluster_expectancy(report.get("trades", []), samples=bootstrap_samples,
                                          seed=20261018 + threshold)
        sweep.append(dict(threshold=threshold, metrics=report["metrics"], tune_interval=interval,
                          monthly=report["monthly"],
                          max_drawdown_pct=report["max_drawdown_pct"], halted=report["halted"]))
    eligible = [r for r in sweep if r["metrics"]["trades"] >= min_tune_trades
                and r["metrics"]["expectancy_r"] > 0 and not r["halted"]
                and r["tune_interval"]["trading_days"] >= min_tune_days
                and r["tune_interval"]["lower_95_r"] is not None
                and r["tune_interval"]["lower_95_r"] > 0]
    result = dict(specification=specification, sweep=sweep, chosen_threshold=None,
                  validation=None, stress=None, monte_carlo=None, live_eligible=False,
                  reason="no_positive_core_tune_threshold_with_sufficient_trades")
    if eligible:
        chosen = max(eligible, key=lambda r: (r["tune_interval"]["lower_95_r"],
            r["metrics"]["expectancy_r"], r["threshold"]))["threshold"]
        result["chosen_threshold"] = chosen
        for name, settings in (("validation", config), ("stress", _stress_config(config))):
            report = research_replay(CandleHistory(tune+later), strategy="core", config=settings,
                threshold=chosen, decision_start=split,
                results_path=output / f"{name}.jsonl" if output else None)
            interval = day_cluster_expectancy(report.get("trades", []), samples=bootstrap_samples,
                                              seed=20261018)
            if output:
                write_report(output / f"{name}.json", report)
            result[name] = {k: v for k, v in report.items() if k != "trades"}
            result[name]["day_block_expectancy"] = interval
            if name == "validation" and report["trades"]:
                from backtest.monte_carlo import simulate
                result["monte_carlo"] = simulate(report["trades"], simulations=simulations,
                                                risk_pct=config.risk_pct, seed=20261018)
                if output:
                    write_report(output / "monte_carlo.json", result["monte_carlo"])
        result["reason"] = "retrospective_diagnostic_only; real-tick and fresh forward evidence still required"
    if output:
        write_report(output / "summary.json", result)
    return result


def day_cluster_expectancy(trades, *, samples=2000, seed=20261018):
    """Exploratory day-block interval, preserving same-day outcome dependence.

    This is not a multiple-testing correction and cannot make reused history a
    fresh holdout. Whole observed trading days are sampled with replacement.
    """
    if samples < 1:
        raise ValueError("positive bootstrap count required")
    groups = {}
    for trade in trades:
        day = trading_day_start(timestamp(trade["opened_at"]))
        total, count = groups.get(day, (0., 0))
        groups[day] = (total+trade["r_multiple"], count+1)
    if not groups:
        return dict(trading_days=0, lower_95_r=None, upper_95_r=None)
    blocks = list(groups.values())
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        draw = rng.choices(blocks, k=len(blocks))
        means.append(sum(r for r, _ in draw)/sum(n for _, n in draw))
    means.sort()
    return dict(trading_days=len(groups), lower_95_r=means[int(.025*(samples-1))],
                upper_95_r=means[int(.975*(samples-1))], samples=samples, seed=seed)


def momentum_walk_forward(rows, split, *, output_dir=None, config=None,
                          min_tune_trades=100, min_tune_days=60, simulations=10000, data_provenance=None):
    """One fixed session-momentum hypothesis: no parameter search or live promotion."""
    config = config or ResearchConfig(spread_pips=.8)
    tune, later = split_history(rows, split)
    if not tune or not later or min(min_tune_trades, min_tune_days, simulations) < 1:
        raise ValueError("nonempty periods and positive evidence floors required")
    output = Path(output_dir) if output_dir else None
    spec = dict(strategy="session_momentum", split=timestamp(split), lookback_sessions=14,
        decision_frequency_minutes=30, sessions="08:00..12:00 London and New York local; weekdays",
        exit_policy="no fixed TP; current band/TWAP loss at half-hour checkpoints or session close",
        protection="fixed opposite-band hard stop; unchanged account breakers and four-trade/day cap",
        config=asdict(config), min_tune_trades=min_tune_trades, min_tune_days=min_tune_days,
        source="Zarattini/Aziz/Barbon 2025 revision, SPY: unverified FX adaptation, TWAP substitutes for VWAP",
        data_status="retrospective research; inspect data provenance for clock and quote basis",
        data_provenance=data_provenance or dict(clock="not independently verified"),
        prior_trials="ICT thresholds, fixed-2R, breakout pullback, failed breakout, stop cascade and trend reclaim",
        news_policy="skipped; historical news unavailable", llm_policy="mechanical bypass",
        live_eligible=False, execution_parity="not compatible with current two-leg live executor; no live adapter activated")
    if output:
        write_report(output/"frozen_specification.json", spec)
    tune_report = session_momentum_replay(tune, config=config,
        results_path=output/"tune.jsonl" if output else None)
    interval = day_cluster_expectancy(tune_report["trades"])
    if output:
        write_report(output/"tune.json", tune_report)
    eligible = (tune_report["metrics"]["trades"] >= min_tune_trades
        and interval["trading_days"] >= min_tune_days and interval["lower_95_r"] > 0
        and not tune_report["halted"])
    result = dict(specification=spec, tune={k:v for k,v in tune_report.items() if k != "trades"},
        tune_interval=interval, validation=None, stress=None, monte_carlo=None,
        qualified_for_later_period=eligible, live_eligible=False,
        reason="insufficient_positive_cost_adjusted_tune_evidence")
    if eligible:
        for name, settings in (("validation", config), ("stress", _stress_config(config))):
            report = session_momentum_replay(CandleHistory(tune+later), decision_start=split,
                config=settings, results_path=output/f"{name}.jsonl" if output else None)
            interval = day_cluster_expectancy(report.get("trades", []), seed=20261018)
            if output:
                write_report(output/f"{name}.json", report)
            result[name] = {k:v for k,v in report.items() if k != "trades"}
            result[name]["day_block_expectancy"] = interval
            if name == "validation" and report["trades"]:
                from backtest.monte_carlo import simulate
                result["monte_carlo"] = simulate(report["trades"], simulations=simulations,
                                                 risk_pct=config.risk_pct, seed=20261018)
                if output:
                    write_report(output/"monte_carlo.json", result["monte_carlo"])
        result["reason"] = "retrospective_diagnostics_only; verified ticks, execution adapter and fresh forward test required"
    if output:
        write_report(output/"summary.json", result)
    return result


def screen_research(rows, split, *, output_dir=None, config=None, min_tune_trades=100,
                    min_complete_months=6, min_tune_days=60, bootstrap_samples=2000,
                    candidate_providers=None):
    """Five declared hypotheses, no tuning on later candles, no live promotion.

    All previously examined history is development evidence. The later slice
    is a retrospective diagnostic, not a new untouched forward test.
    """
    config = config or ResearchConfig()
    tune, later = split_history(rows, split)
    if (not tune or not later or min(min_tune_trades, min_complete_months,
                                     min_tune_days, bootstrap_samples) < 1):
        raise ValueError("research requires nonempty periods and positive sample floors")
    output = Path(output_dir) if output_dir else None
    experiments = (("ict_2r", "ict_2r", replace(config, reward_r=2.)),
                   ("breakout_1_5r", "breakout_pullback", replace(config, reward_r=1.5)),
                   ("breakout_2r", "breakout_pullback", replace(config, reward_r=2.)),
                   ("failed_breakout_2r", "failed_breakout", replace(config, reward_r=2.)),
                   ("stop_cascade_1_25r", "stop_cascade_continuation",
                    replace(config, reward_r=1.25, max_hold_minutes=30)))
    specification = dict(split=timestamp(split), experiments=[dict(name=n, strategy=s, config=asdict(c)) for n, s, c in experiments],
                         min_tune_trades=min_tune_trades, min_complete_months=min_complete_months,
                         min_tune_days=min_tune_days, bootstrap_samples=bootstrap_samples,
                         data_status="previously examined history: development only", feasibility=target_feasibility(config.risk_pct))
    if output:
        write_report(output / "frozen_specification.json", specification)
    sweep = []
    for index, (name, strategy, settings) in enumerate(experiments):
        report = research_replay(tune, strategy=strategy, config=settings,
                                 candidate_provider=(candidate_providers or {}).get(strategy),
                                 results_path=output / f"{name}_tune.jsonl" if output else None)
        interval = day_cluster_expectancy(report.get("trades", []), samples=bootstrap_samples,
                                         seed=20261018 + index)
        if output:
            write_report(output / f"{name}_tune.json", report)
        sweep.append(dict(name=name, strategy=strategy, config=asdict(settings), metrics=report["metrics"],
                          tune_interval=interval,
                          monthly=report["monthly"], max_drawdown_pct=report["max_drawdown_pct"], halted=report["halted"]))
    eligible = [r for r in sweep if r["metrics"]["trades"] >= min_tune_trades and
                r["metrics"]["expectancy_r"] > 0 and r["monthly"]["complete_months"] >= min_complete_months and
                r["tune_interval"]["trading_days"] >= min_tune_days and
                r["tune_interval"]["lower_95_r"] is not None and
                r["tune_interval"]["lower_95_r"] > 0 and not r["halted"]]
    result = dict(specification=specification, sweep=sweep, chosen=None, validation=None,
                  promotion_passed=False, live_eligible=False,
                  reason="no_positive_tune_candidate_with_sufficient_day_block_evidence",
                  selection="highest tune day-block lower 95% expectancy; expectancy/trade-count tie-breakers")
    if eligible:
        chosen = max(eligible, key=lambda r: (r["tune_interval"]["lower_95_r"],
                                              r["metrics"]["expectancy_r"], r["metrics"]["trades"]))
        settings = ResearchConfig(**chosen["config"])
        validation = research_replay(CandleHistory(tune + later), strategy=chosen["strategy"], config=settings,
                                     decision_start=split,
                                     candidate_provider=(candidate_providers or {}).get(chosen["strategy"]),
                                     results_path=output / "later_development.jsonl" if output else None)
        validation_interval = day_cluster_expectancy(validation.get("trades", []), samples=bootstrap_samples,
                                                      seed=20261018)
        validation["day_block_expectancy"] = validation_interval
        result.update(chosen=chosen["name"],
                      validation={k: v for k, v in validation.items() if k != "trades"},
                      validation_interval=validation_interval,
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
    parser.add_argument("--core", action="store_true", help="cost-aware shared-core experiment")
    parser.add_argument("--session-momentum", action="store_true", help="single frozen session/noise-band candidate")
    parser.add_argument("--risk-pct", type=float, default=.0025)
    parser.add_argument("--spread-pips", type=float, default=.5)
    parser.add_argument("--spread-profile", type=Path,
                        help="JSON pair-to-pips profile, such as a tick-audit report")
    parser.add_argument("--slippage-pips", type=float, default=.1)
    parser.add_argument("--commission-per-lot", type=float, default=7.)
    args = parser.parse_args()
    logger = setup_logging("walk_forward")
    rows = load_history(args.data_dir)
    spread_pips = load_spread_profile(args.spread_profile) if args.spread_profile else args.spread_pips
    if args.session_momentum:
        if args.core or args.research or args.llm:
            parser.error("--session-momentum is a separate mechanical experiment")
        result = momentum_walk_forward(rows, args.split, output_dir=args.output_dir/"session_momentum",
            config=ResearchConfig(risk_pct=args.risk_pct, spread_pips=spread_pips,
                slippage_pips=args.slippage_pips, commission_per_lot=args.commission_per_lot))
        logger.info("Session momentum qualified=%s; %s", result["qualified_for_later_period"], result["reason"])
        return
    if args.core:
        if args.llm or args.research:
            parser.error("--core is a separate mechanical-only experiment")
        result = core_walk_forward(rows, args.split, output_dir=args.output_dir / "core",
            thresholds=tuple(int(t) for t in args.thresholds.split(",") if int(t) >= 8),
            config=ResearchConfig(risk_pct=args.risk_pct, spread_pips=spread_pips,
                slippage_pips=args.slippage_pips, commission_per_lot=args.commission_per_lot,
                max_hold_minutes=30))
        logger.info("Core chosen=%s; %s", result["chosen_threshold"], result["reason"])
        return
    if args.research:
        if args.llm:
            parser.error("research screening is mechanical only")
        config = ResearchConfig(risk_pct=args.risk_pct, spread_pips=spread_pips,
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
