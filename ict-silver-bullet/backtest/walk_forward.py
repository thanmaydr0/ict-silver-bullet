"""Tune thresholds on earlier history, then validate one choice exactly once.

Usage: python -m backtest.walk_forward --split 2025-01-01T00:00:00Z
"""

import argparse
from datetime import timedelta
from pathlib import Path

from backtest.replay import load_history, normalize_history, replay, timestamp, write_report
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
    return tune, validate


def walk_forward(rows, split, *, thresholds=(7, 8, 9, 10), output_dir=None,
                 llm=False, review_setup=None):
    """Choose highest tune expectancy (ties prefer higher threshold).

    Validation starts with a fresh account. Tune candles warm indicators only.
    LLM tuning/validation, when requested, is a separate experiment.
    """
    tune, validate = split_history(rows, split)
    thresholds = tuple(thresholds)
    if not tune or not validate or not thresholds:
        raise ValueError("nonempty tune/validate periods and thresholds required")
    output = Path(output_dir) if output_dir else None
    sweep = []
    for threshold in thresholds:
        report = replay(tune, threshold=threshold, llm=llm, review_setup=review_setup, collect_decisions=False,
                        results_path=output / f"tune_{threshold}.jsonl" if output else None)
        sweep.append(dict(threshold=threshold, effective_threshold=report["effective_threshold"],
                          **report["metrics"]))
    # NOTE: Select by tune expectancy, break ties toward stricter thresholds;
    # no closed tune trades means no defensible choice and no validation run.
    eligible = [r for r in sweep if r["trades"]]
    if not eligible:
        result = dict(split=timestamp(split), sweep=sweep, chosen_threshold=None,
                      validation=None, reason="no_closed_tune_trades", news_policy="skipped")
    else:
        chosen = max(eligible, key=lambda r: (r["expectancy_r"], r["effective_threshold"], r["threshold"]))
        validation = replay(tune + validate, threshold=chosen["threshold"],
                            decision_start=split, llm=llm, review_setup=review_setup, collect_decisions=False,
                            results_path=output / "validate.jsonl" if output else None)
        result = dict(split=timestamp(split), sweep=sweep, chosen_threshold=chosen["threshold"],
                      validation={k: v for k, v in validation.items() if k != "decisions"},
                      selection="highest tune expectancy; ties prefer higher threshold",
                      news_policy="skipped: historical news unavailable")
    if output:
        write_report(output / "walk_forward.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", required=True)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "results" / "walk_forward")
    parser.add_argument("--thresholds", default="7,8,9,10")
    parser.add_argument("--llm", action="store_true")
    args = parser.parse_args()
    logger = setup_logging("walk_forward")
    rows = load_history(args.data_dir)
    for llm in ([False, True] if args.llm else [False]):
        result = walk_forward(rows, args.split, thresholds=tuple(int(t) for t in args.thresholds.split(",")),
                              output_dir=args.output_dir / ("mechanical_llm" if llm else "mechanical"), llm=llm)
        logger.info("Chosen threshold=%s; validation=%s", result["chosen_threshold"],
                    result["validation"]["metrics"] if result["validation"] else result["reason"])


if __name__ == "__main__":
    main()
