"""Chronological replay of the live decision chain.

Run ``python -m backtest.replay --data-dir backtest/data --llm`` for two
independent passes. JSONL contains full decision inputs, outcomes and a report.
Historical news is unavailable: the news gate is explicitly skipped.
"""

import argparse
from collections import Counter, defaultdict, deque
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path

from brain import app
from brain.logging_setup import setup_logging
from brain.pairs import pair_spec
from brain.risk import engine
from brain.risk.circuit_breakers import DrawdownState, check_breakers, trading_day_start


def timestamp(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def normalize_history(rows):
    unique = {}
    for raw in rows:
        row = dict(raw, ts=timestamp(raw["ts"]))
        row.setdefault("timeframe", "M1")
        if row["timeframe"] not in {"M1", "H1"}:
            raise ValueError("replay supports M1 and H1 only")
        for key in ("open", "high", "low", "close"):
            row[key] = float(row[key])
        if not all(math.isfinite(row[k]) for k in ("open", "high", "low", "close")):
            raise ValueError("nonfinite candle")
        if not row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]:
            raise ValueError("invalid OHLC candle")
        key = row["pair"], row["timeframe"], row["ts"]
        if key in unique and unique[key] != row:
            raise ValueError("conflicting duplicate candle")
        unique[key] = row
    return sorted(unique.values(), key=lambda c: (c["ts"], c["pair"], c["timeframe"]))


def load_history(data_dir):
    import pandas as pd

    paths = sorted(Path(data_dir).glob("*.parquet"))
    if not paths:
        raise ValueError("no parquet history found")
    return normalize_history(row for path in paths for row in pd.read_parquet(path).to_dict("records"))


def metrics(trades):
    closed = [t for t in trades if t.get("r_multiple") is not None]
    values = [t["r_multiple"] for t in closed]
    wins = [r for r in values if r > 0]
    losses = [-r for r in values if r < 0]
    return dict(trades=len(values), win_rate=len(wins) / len(values) if values else None,
                expectancy_r=sum(values) / len(values) if values else None,
                average_rr=(sum(wins) / len(wins)) / (sum(losses) / len(losses)) if wins and losses else None,
                average_win_r=sum(wins) / len(wins) if wins else None,
                average_loss_r=sum(losses) / len(losses) if losses else None)


def _json(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def write_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, default=_json, indent=2), encoding="utf-8")


def replay(rows, *, threshold=8, initial_equity=10000.0, llm=False,
           review_setup=None, results_path=None, decision_start=None, decision_end=None,
           collect_decisions=True):
    """Replay completed bars, with fresh account state on every invocation.

    decision_start/end bound decision times [start, end); earlier rows may only
    warm detection. Injection of review_setup supports offline LLM tests.
    """
    if threshold not in range(7, 11) or not math.isfinite(initial_equity) or initial_equity <= 0:
        raise ValueError("threshold must be 7..10 and equity positive")
    # NOTE: Phase 7 and Phase 3 both enforce score >=8. A requested sweep of
    # 7 is recorded as effective 8 rather than silently changing live guards.
    minimum = max(threshold, app.MIN_SETUP_SCORE)
    start = timestamp(decision_start) if decision_start else None
    end = timestamp(decision_end) if decision_end else None
    rows = normalize_history(rows)
    m1, h1 = defaultdict(lambda: deque(maxlen=app.CANDLE_LIMIT)), defaultdict(lambda: deque(maxlen=app.CANDLE_LIMIT))
    # NOTE: Derive H1 only from all 60 distinct M1 bars; incomplete hours cannot
    # masquerade as completed higher-timeframe evidence. Explicit H1 takes precedence.
    explicit = set()
    buckets = defaultdict(list)
    events = sorted(rows, key=lambda r: (r["ts"] + timedelta(minutes=1 if r["timeframe"] == "M1" else 60), r["timeframe"], r["pair"]))
    balance = initial_equity
    state = DrawdownState(balance, balance)
    positions, pending, trades, decisions = [], {}, [], []
    reasons = Counter()
    counts = losses = 0
    halted = False
    daily_halted = None
    seen = set()
    last_prices = {}
    output = None
    if results_path:
        path = Path(results_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        output = path.open("w", encoding="utf-8")

    def emit(record):
        if output:
            output.write(json.dumps(record, default=_json) + "\n")

    def close(position, price, now, reason):
        nonlocal balance, losses
        sign = 1 if position["direction"] == "buy" else -1
        pnl = (price - position["entry"]) * sign * position["usd_per_price"]
        balance += pnl
        losses = losses + 1 if pnl < 0 else 0
        trade = dict(position, closed_at=now, exit=price, exit_reason=reason,
                     pnl=pnl, r_multiple=pnl / position["risk_usd"])
        trades.append(trade)
        emit(dict(type="trade", **trade))
        positions.remove(position)

    def equity():
        return balance + sum((last_prices[p["pair"]] - p["entry"]) *
                             (1 if p["direction"] == "buy" else -1) * p["usd_per_price"] for p in positions)

    def scorer(factors, **kwargs):
        result = app.confluence.score_setup(factors, **kwargs)
        if not result["reason"] and result["score"] < minimum:
            result = dict(result, reason="score_below_sweep_threshold")
        return result

    def risk(score, *args):
        if halted:
            return dict(approved=False, lot_size=None, reason="hard_overall")
        if daily_halted == trading_day_start(args[-1]):
            return dict(approved=False, lot_size=None, reason="hard_daily")
        if score["score"] < minimum:
            return dict(approved=False, lot_size=None, reason="score_below_sweep_threshold")
        return engine.evaluate_signal(score, *args)

    reviewer = review_setup or (app.reasoning.review_setup if llm else lambda _: dict(
        verdict="approve", conviction=1.0, reasoning="Mechanical-only pass; LLM bypassed"))
    try:
        for row in events:
            pair = row["pair"]
            now = row["ts"] + timedelta(minutes=1 if row["timeframe"] == "M1" else 60)
            if end and now >= end:
                break
            if row["timeframe"] == "H1":
                explicit.add(pair)
                h1[pair].append(row)
                continue
            m1[pair].append(row)
            if pair not in explicit:
                hour = row["ts"].replace(minute=0, second=0, microsecond=0)
                bucket = buckets[pair]
                if bucket and bucket[0]["ts"].replace(minute=0) != hour:
                    bucket.clear()
                bucket.append(row)
                if len(bucket) == 60 and now == hour + timedelta(hours=1):
                    h1[pair].append(dict(pair=pair, timeframe="H1", ts=hour, open=bucket[0]["open"],
                        high=max(c["high"] for c in bucket), low=min(c["low"] for c in bucket), close=row["close"]))
                    bucket.clear()
            if start and now < start:
                continue
            if state.day_start != trading_day_start(now):
                counts = losses = 0
            state.update(equity(), balance, now)
            last_prices[pair] = row["close"]
            signal = pending.pop(pair, None)
            # NOTE: Pending limits expire after one M1 bar. No same-decision-bar
            # fills; no spread/commission model exists in exported OHLC history.
            if signal and row["ts"] >= signal["detected_at"] and row["low"] <= signal["entry"] <= row["high"]:
                spec = pair_spec(pair, signal["entry"])
                unit = signal["lot_size"] * spec["pip_value_per_lot"] / spec["pip_size"]
                positions.append(dict(signal, opened_at=row["ts"], usd_per_price=unit,
                                      risk_usd=abs(signal["entry"] - signal["stop_loss"]) * unit))
                counts += 1
                emit(dict(type="fill", now=now, signal=signal))
            elif signal:
                emit(dict(type="expiry", now=now, signal=signal))
            for p in list(positions):
                if p["pair"] != pair:
                    continue
                buy = p["direction"] == "buy"
                stopped = row["low"] <= p["stop_loss"] if buy else row["high"] >= p["stop_loss"]
                target = row["high"] >= p["take_profit"] if buy else row["low"] <= p["take_profit"]
                # NOTE: Stop wins OHLC ambiguity; existing positions gap through
                # stops at the adverse open. Newly filled limits stop at stop price.
                if stopped:
                    price = p["stop_loss"]
                    if p["opened_at"] < row["ts"]:
                        price = min(price, row["open"]) if buy else max(price, row["open"])
                    close(p, price, now, "stop")
                elif target:
                    close(p, p["take_profit"], now, "target")
            state.update(equity(), balance, now)
            breakers = check_breakers(state.daily_dd_pct, state.overall_dd_pct)
            if "hard_daily" in breakers or "hard_overall" in breakers:
                halted = halted or "hard_overall" in breakers
                daily_halted = trading_day_start(now)
                for p in list(positions):
                    close(p, last_prices[p["pair"]], now, "circuit_breaker")
                pending.clear()
                state.update(equity(), balance, now)
            account = dict(equity=equity(), drawdown_state=asdict(state), trades_today=counts + len(pending),
                           consecutive_losses_today=losses, open_positions=[dict(p) for p in positions] + list(pending.values()))
            context = dict(now=now, pair=pair, htf_candles=list(h1[pair]), news_events=[],
                           review_setup=reviewer, score_setup=scorer, evaluate_signal=risk,
                           risk_state=dict(account, drawdown_state=state))
            decision = app.evaluate_candles(list(m1[pair]), context)
            record = dict(type="decision", now=now, pair=pair, candles=list(m1[pair]),
                          htf_candles=list(h1[pair]), news_policy="skipped", risk_state=account,
                          decision=asdict(decision), execution="discarded")
            if decision.approved:
                s = decision.signal
                # NOTE: Deduplicate repeated views of the same MSS, using its
                # absolute candle timestamp rather than shifting window indices.
                index = (decision.setup or {}).get("mss", {}).get("index")
                origin = list(m1[pair])[index]["ts"] if isinstance(index, int) and 0 <= index < len(m1[pair]) else trading_day_start(now)
                key = (pair, origin, s["direction"], s["entry"], s["stop_loss"], s["take_profit"])
                if key not in seen:
                    pending[pair] = dict(s)
                    seen.add(key)
                    record["execution"] = "pending"
                else:
                    record["execution"] = "duplicate_setup"
            # Keep compact decisions in memory; full candle context streams to disk.
            reasons[decision.reason] += 1
            if collect_decisions:
                decisions.append({k: v for k, v in record.items() if k not in {"candles", "htf_candles"}})
            # NOTE: Stream full context for every candidate; aggregate empty
            # decision reasons. CLIs avoid retaining multi-year audit data in RAM.
            if decision.setup is not None:
                emit(record)
        report = dict(mode="mechanical+LLM" if llm else "mechanical-only", threshold=threshold,
                      effective_threshold=minimum, news_policy="skipped: historical news unavailable",
                      execution_assumptions="next-bar limit, one-bar expiry, stop-first ambiguity, adverse stop gaps; no costs; floating equity sampled at M1 close; shared approximate USD pip values",
                      initial_equity=initial_equity, balance=balance, equity=equity(),
                      decision_counts=dict(reasons),
                      metrics=metrics(trades), trades=trades, open_positions=positions,
                      pending=list(pending.values()), decisions=decisions)
        # NOTE: Open positions are reported separately, never counted as wins/losses
        # or force-liquidated using prices outside this replay's data boundary.
        emit(dict(type="report", **{k: v for k, v in report.items() if k != "decisions"}))
        return report
    finally:
        if output:
            output.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "results")
    parser.add_argument("--threshold", type=int, default=8)
    parser.add_argument("--llm", action="store_true")
    args = parser.parse_args()
    logger = setup_logging("backtest")
    rows = load_history(args.data_dir)
    for llm in ([False, True] if args.llm else [False]):
        name = "mechanical_llm" if llm else "mechanical"
        report = replay(rows, threshold=args.threshold, llm=llm, collect_decisions=False,
                        results_path=args.output_dir / f"{name}.jsonl")
        write_report(args.output_dir / f"{name}.json", {k: v for k, v in report.items() if k != "decisions"})
        logger.info("%s: %s; news gate skipped", name, report["metrics"])


if __name__ == "__main__":
    main()
