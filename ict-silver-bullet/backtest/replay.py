"""Chronological replay of the live decision chain.

Run ``python -m backtest.replay --data-dir backtest/data --llm`` for two
independent passes. JSONL contains full decision inputs, outcomes and a report.
Historical news is unavailable: the news gate is explicitly skipped.
"""

import argparse
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path

from brain import app
from brain.logging_setup import setup_logging
from brain.pairs import pair_spec
from brain.risk import engine
from brain.risk.position_sizing import DEFAULT_RISK_PCT
from brain.risk.circuit_breakers import DrawdownState, check_breakers, trading_day_start
from brain.scheduler.killzone_clock import current_killzone, killzone_close


@dataclass(frozen=True)
class ResearchConfig:
    """Offline screening assumptions, never production settings or quotations."""

    reward_r: float = 2.0
    risk_pct: float = DEFAULT_RISK_PCT
    spread_pips: float = 0.5
    slippage_pips: float = 0.1
    commission_per_lot: float = 7.0  # round trip, USD-account assumption
    max_cost_r: float = 0.2
    max_hold_minutes: int = 20
    max_entry_deviation_pips: float = 2.0
    monthly_target: float = 0.10

    def __post_init__(self):
        values = asdict(self)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values.values()):
            raise ValueError("research assumptions must be finite numbers")
        if not (0 < self.risk_pct <= .005 and 0 < self.reward_r <= 5 and
                0 < self.max_cost_r <= 1 and self.monthly_target > 0 and
                isinstance(self.max_hold_minutes, int) and self.max_hold_minutes > 0):
            raise ValueError("invalid research risk, reward, cost cap or holding time")
        if min(self.spread_pips, self.slippage_pips, self.commission_per_lot,
               self.max_entry_deviation_pips) < 0:
            raise ValueError("execution costs and deviation must be nonnegative")


class BreakoutPullback:
    """Causal M5 first-pullback hypothesis. Research-only; not a live detector.

    H1 EMA20 trend, prior twelve M5 highs/lows, one reclaim within fifteen
    minutes. Indicators consume completed bars only, including outside windows.
    """

    def __init__(self):
        self.h1 = deque(maxlen=300)
        self.ema = deque(maxlen=4)
        self.h1_count = 0
        self.m5 = deque(maxlen=40)
        self.bucket = []
        self.armed = None

    def update_h1(self, row):
        close = row["close"]
        self.ema.append(close if not self.ema else self.ema[-1] + 2 / 21 * (close - self.ema[-1]))
        self.h1.append(row)
        self.h1_count += 1

    def opposing_level(self, entry, buy):
        field = "high" if buy else "low"
        bars = list(self.h1)
        pools = []
        for i in range(2, len(bars) - 2):
            level = bars[i][field]
            neighbours = [bars[j][field] for j in (i - 2, i - 1, i + 1, i + 2)]
            pivot = all(level > x if buy else level < x for x in neighbours)
            untapped = all(b[field] < level if buy else b[field] > level for b in bars[i + 3:])
            if pivot and untapped and (level > entry if buy else level < entry):
                pools.append(level)
        return (min(pools) if buy else max(pools)) if pools else None

    def update_m1(self, row):
        now = row["ts"] + timedelta(minutes=1)
        setup = None
        armed = self.armed
        if armed and self.h1:
            buy = armed["buy"]
            aligned = (self.h1[-1]["close"] > self.ema[-1] > self.ema[0] if buy
                       else self.h1[-1]["close"] < self.ema[-1] < self.ema[0])
            fresh = self.h1[-1]["ts"] + timedelta(hours=2) >= now
            if not aligned or not fresh:
                self.armed = armed = None
        if armed and now > armed["expires"]:
            self.armed = armed = None
        if armed and row["ts"] >= armed["formed_at"]:
            buy, level = armed["buy"], armed["level"]
            armed["extreme"] = (min(armed["extreme"], row["low"]) if buy
                                else max(armed["extreme"], row["high"]))
            touched = row["low"] <= level <= row["high"]
            armed["touched"] = armed["touched"] or touched
            reclaim = (row["close"] > level and row["close"] > row["open"] if buy
                       else row["close"] < level and row["close"] < row["open"])
            if armed["touched"] and reclaim:
                pip = pair_spec(row["pair"], row["close"])["pip_size"]
                buffer = max(pip, .1 * armed["atr"])
                setup = dict(pair=row["pair"], direction="buy" if buy else "sell",
                             entry=row["close"], stop_loss=armed["extreme"] + (-buffer if buy else buffer),
                             setup_id=armed["formed_at"].isoformat(),
                             opposing_level=self.opposing_level(row["close"], buy),
                             evidence=dict(level=level, atr_m5=armed["atr"],
                                           h1_ema20=self.ema[-1], h1_ema20_three_bars_ago=self.ema[0]))
                self.armed = None
        minute = row["ts"].replace(minute=row["ts"].minute // 5 * 5, second=0, microsecond=0)
        if self.bucket and self.bucket[0]["ts"].replace(minute=self.bucket[0]["ts"].minute // 5 * 5) != minute:
            self.bucket.clear()
        self.bucket.append(row)
        if len(self.bucket) == 5 and self.bucket[0]["ts"] == minute and now == minute + timedelta(minutes=5):
            bar = dict(ts=minute, open=self.bucket[0]["open"], close=row["close"],
                       high=max(b["high"] for b in self.bucket), low=min(b["low"] for b in self.bucket))
            previous = list(self.m5)
            self.m5.append(bar)
            self.bucket.clear()
            if len(previous) >= 14 and self.h1_count >= 23:
                ranges = [max(b["high"] - b["low"], abs(b["high"] - p["close"]),
                              abs(b["low"] - p["close"])) for p, b in zip(previous[-14:], (previous + [bar])[-14:])]
                atr = sum(ranges) / len(ranges)
                top, bottom = max(b["high"] for b in previous[-12:]), min(b["low"] for b in previous[-12:])
                buy = bar["close"] > top and self.h1[-1]["close"] > self.ema[-1] > self.ema[0]
                sell = bar["close"] < bottom and self.h1[-1]["close"] < self.ema[-1] < self.ema[0]
                if (buy or sell) and atr > 0:
                    self.armed = dict(buy=buy, level=top if buy else bottom, atr=atr,
                                      formed_at=now, expires=now + timedelta(minutes=15), touched=False,
                                      extreme=top if buy else bottom)
        return setup


class FailedBreakout:
    """Research-only fade after a completed M5 breakout fails on M1.

    A close outside the prior twelve completed M5 bars arms one event. Within
    fifteen minutes, the first directional M1 close back across the level is
    the entry; the stop sits beyond the full observed breakout excursion.
    Every input is completed before the decision timestamp.
    """

    def __init__(self):
        self.m5 = deque(maxlen=40)
        self.bucket = []
        self.armed = None
        self.previous_close = None

    def update_h1(self, row):
        # research_replay streams explicit H1 rows to every model type.
        return None

    def update_m1(self, row):
        now = row["ts"] + timedelta(minutes=1)
        setup = None
        armed = self.armed
        if armed and now > armed["expires"]:
            self.armed = armed = None
        if armed and now > armed["formed_at"]:
            upward = armed["breakout_direction"] == "up"
            armed["extreme"] = (max(armed["extreme"], row["high"]) if upward
                                else min(armed["extreme"], row["low"]))
            crossed_back = (self.previous_close is not None and
                            ((self.previous_close >= armed["level"] and row["close"] < armed["level"]
                              and row["close"] < row["open"]) if upward else
                             (self.previous_close <= armed["level"] and row["close"] > armed["level"]
                              and row["close"] > row["open"])))
            if crossed_back:
                pip = pair_spec(row["pair"], row["close"])["pip_size"]
                buffer = max(pip, .1 * armed["atr"])
                direction = "sell" if upward else "buy"
                stop = armed["extreme"] + buffer if upward else armed["extreme"] - buffer
                if (stop > row["close"] if upward else stop < row["close"]):
                    setup = dict(pair=row["pair"], direction=direction, entry=row["close"],
                                 stop_loss=stop, setup_id=armed["formed_at"].isoformat(),
                                 opposing_level=None,
                                 evidence=dict(level=armed["level"], atr_m5=armed["atr"],
                                               breakout_direction=armed["breakout_direction"],
                                               breakout_extreme=armed["extreme"]))
                    self.armed = None

        minute = row["ts"].replace(minute=row["ts"].minute // 5 * 5, second=0, microsecond=0)
        if self.bucket and self.bucket[0]["ts"].replace(
                minute=self.bucket[0]["ts"].minute // 5 * 5, second=0, microsecond=0) != minute:
            self.bucket.clear()
        self.bucket.append(row)
        if (len(self.bucket) == 5 and self.bucket[0]["ts"] == minute and
                [b["ts"] for b in self.bucket] == [minute + timedelta(minutes=i) for i in range(5)] and
                now == minute + timedelta(minutes=5)):
            bar = dict(ts=minute, open=self.bucket[0]["open"], close=row["close"],
                       high=max(b["high"] for b in self.bucket), low=min(b["low"] for b in self.bucket))
            previous = list(self.m5)
            self.m5.append(bar)
            self.bucket.clear()
            if len(previous) >= 14:
                ranges = [max(current["high"] - current["low"],
                              abs(current["high"] - prior["close"]),
                              abs(current["low"] - prior["close"]))
                          for prior, current in zip(previous[-14:], (previous + [bar])[-14:])]
                atr = sum(ranges) / len(ranges)
                top = max(b["high"] for b in previous[-12:])
                bottom = min(b["low"] for b in previous[-12:])
                if atr > 0 and (bar["close"] > top or bar["close"] < bottom):
                    upward = bar["close"] > top
                    self.armed = dict(breakout_direction="up" if upward else "down",
                                      level=top if upward else bottom, atr=atr,
                                      formed_at=now, expires=now + timedelta(minutes=15),
                                      extreme=bar["high"] if upward else bar["low"])
        self.previous_close = row["close"]
        return setup


def timestamp(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    # Parquet readers return pandas Timestamp subclasses. Convert once at the
    # boundary so the shared live evaluator uses ordinary datetime arithmetic.
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


class CandleHistory(tuple):
    """Already normalized candles; replay reads rows without mutating them.

    Internal callers may share this sequence across passes without copying
    millions of row dictionaries. Construct via normalize_history externally.
    """


def normalize_history(rows):
    if isinstance(rows, CandleHistory):
        return rows
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
    return CandleHistory(sorted(unique.values(), key=lambda c: (c["ts"], c["pair"], c["timeframe"])))


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


def monthly_statistics(equity_by_month, initial_equity, target=.10):
    """Include zero-trade months; conservatively exclude boundary months.

    Values are end-of-month marked equity. Boundary months are reported but
    not counted as complete months because historical coverage can be partial.
    """
    items = sorted(equity_by_month.items())
    rows = []
    previous = initial_equity
    for i, (month, equity) in enumerate(items):
        value = equity / previous - 1
        rows.append(dict(month=month, return_pct=100 * value, target_met=value >= target,
                         complete=0 < i < len(items) - 1))
        previous = equity
    complete = [r for r in rows if r["complete"]]
    return dict(months=rows, complete_months=len(complete), target_pct=100 * target,
                months_at_target=sum(r["target_met"] for r in complete),
                target_hit_rate=sum(r["target_met"] for r in complete) / len(complete) if complete else None,
                minimum_month_pct=min((r["return_pct"] for r in complete), default=None),
                mean_month_pct=sum(r["return_pct"] for r in complete) / len(complete) if complete else None,
                losing_months=sum(r["return_pct"] < 0 for r in complete),
                all_complete_months_at_target=bool(complete) and all(r["target_met"] for r in complete))


def research_replay(rows, *, strategy="breakout_pullback", config=None, initial_equity=10000.,
                    decision_start=None, decision_end=None, results_path=None, candidate_provider=None):
    """Offline hypothesis screening with explicit friction and market-at-next-open fills.

    This intentionally does NOT claim execution parity with the two-leg live
    executor. ICT comparisons use the shared detector; experimental breakout
    hypotheses are research-only. No broker, database, LLM or live-setting writes occur.
    Bars are assumed Bid OHLC. Real Bid/Ask ticks remain required for validation.
    candidate_provider is an injectable causal research detector for offline tests.
    """
    config = config or ResearchConfig()
    if strategy not in {"breakout_pullback", "failed_breakout", "ict_2r"}:
        raise ValueError("unknown research strategy")
    if not math.isfinite(initial_equity) or initial_equity <= 0:
        raise ValueError("initial equity must be positive")
    start = timestamp(decision_start) if decision_start else None
    end = timestamp(decision_end) if decision_end else None
    if start and end and end <= start:
        raise ValueError("decision_end must follow decision_start")
    history = normalize_history(rows)
    events = sorted(history, key=lambda r: (r["ts"] + timedelta(minutes=1 if r["timeframe"] == "M1" else 60),
                                           r["timeframe"], r["pair"]))
    model_factory = FailedBreakout if strategy == "failed_breakout" else BreakoutPullback
    models = defaultdict(model_factory)
    m1 = defaultdict(lambda: deque(maxlen=app.CANDLE_LIMIT))
    balance = initial_equity
    state = DrawdownState(balance, balance)
    position = pending = None
    trades, monthly = [], {}
    counts = losses = 0
    halted = False
    daily_halt = None
    max_dd = 0.
    prices, seen = {}, set()
    reasons = Counter()
    output = None
    if results_path:
        path = Path(results_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        output = path.open("w", encoding="utf-8")

    def emit(record):
        if output:
            output.write(json.dumps(record, default=_json) + "\n")

    def marked(price=None):
        if position is None:
            return balance
        quote = prices[position["pair"]] if price is None else price
        exit_quote = quote if position["direction"] == "buy" else quote + position["spread"]
        return balance + (exit_quote - position["entry"]) * position["sign"] * position["usd_per_price"]

    def close(price, now, reason):
        nonlocal balance, position, losses
        slip = config.slippage_pips * position["pip"]
        execution = price - position["sign"] * slip
        gross = (execution - position["entry"]) * position["sign"] * position["usd_per_price"]
        fee = position["lot_size"] * config.commission_per_lot / 2
        balance += gross - fee
        net = gross - fee - position["entry_fee"]
        trade = dict(position, closed_at=now, exit=execution, exit_reason=reason,
                     gross_pnl=gross, commission=fee + position["entry_fee"], pnl=net,
                     r_multiple=net / position["risk_usd"])
        trades.append(trade)
        losses = losses + 1 if net < 0 else 0
        emit(dict(type="trade", **trade))
        position = None

    try:
        for row in events:
            pair = row["pair"]
            now = row["ts"] + timedelta(minutes=1 if row["timeframe"] == "M1" else 60)
            if end and now >= end:
                break
            model = models[pair]
            if row["timeframe"] == "H1":
                model.update_h1(row)
                continue
            m1[pair].append(row)
            candidate = model.update_m1(row) if strategy in {"breakout_pullback", "failed_breakout"} else None
            if start and now < start:
                continue
            if state.day_start != trading_day_start(now):
                counts = losses = 0
            state.update(marked(), balance, now)
            prices[pair] = row["close"]

            if pending and pending["pair"] == pair:
                request, pending = pending, None
                sign = 1 if request["direction"] == "buy" else -1
                spec = pair_spec(pair, row["open"])
                pip = spec["pip_size"]
                spread = config.spread_pips * pip
                entry = row["open"] + (spread if sign == 1 else 0) + sign * config.slippage_pips * pip
                stop = request["stop_loss"]
                distance = sign * (entry - stop)
                cost_pips = config.spread_pips + 2 * config.slippage_pips + config.commission_per_lot / spec["pip_value_per_lot"]
                cost_r = cost_pips * pip / distance if distance > 0 else math.inf
                reason = None
                if row["ts"] != request["detected_at"]:
                    reason = "missing_next_bar"
                elif position is not None or halted or daily_halt == trading_day_start(now):
                    reason = "account_unavailable"
                elif distance <= 0:
                    reason = "invalid_stop_after_fill"
                elif abs(entry - request["entry"]) / pip > config.max_entry_deviation_pips:
                    reason = "entry_moved"
                elif cost_r > config.max_cost_r:
                    reason = "cost_exceeds_budget"
                target = entry + sign * config.reward_r * distance
                obstacle = request.get("opposing_level")
                if not reason and obstacle is not None and sign * (obstacle - target) < 0:
                    reason = "insufficient_target_room"
                # NOTE: Eligibility=10 here means every research rule passed;
                # it is neither an ICT confluence score nor a win probability.
                # Shared risk limits remain intact; this model is not live-ready.
                if not reason:
                    risk = engine.evaluate_signal(dict(score=10, reason=None), state,
                        dict(equity=marked(), pair=pair, direction=request["direction"],
                             stop_loss_pips=distance / pip + config.slippage_pips +
                                 config.commission_per_lot / spec["pip_value_per_lot"],
                             pip_value_per_lot=spec["pip_value_per_lot"], risk_pct=config.risk_pct),
                        [], counts, losses, row["ts"])
                    if not risk["approved"]:
                        reason = risk["reason"]
                if reason:
                    reasons[reason] += 1
                    emit(dict(type="rejection", now=now, reason=reason, setup=request))
                else:
                    lots = risk["lot_size"]
                    unit = lots * spec["pip_value_per_lot"] / pip
                    fee = lots * config.commission_per_lot / 2
                    balance -= fee
                    position = dict(request, entry=entry, stop_loss=stop, take_profit=target,
                                    opened_at=row["ts"], deadline=row["ts"] + timedelta(minutes=config.max_hold_minutes),
                                    sign=sign, lot_size=lots, risk_usd=distance * unit,
                                    usd_per_price=unit, pip=pip, spread=spread, entry_fee=fee, estimated_cost_r=cost_r)
                    counts += 1
                    reasons["filled"] += 1
                    emit(dict(type="fill", **position))

            if position and position["pair"] == pair:
                buy = position["sign"] == 1
                stop, target = position["stop_loss"], position["take_profit"]
                low = row["low"] + (0 if buy else position["spread"])
                high = row["high"] + (0 if buy else position["spread"])
                if (low <= stop if buy else high >= stop):
                    opening = row["open"] + (0 if buy else position["spread"])
                    close(min(stop, opening) if buy else max(stop, opening), now, "stop")
                else:
                    # NOTE: OHLC cannot establish tick order. Test adverse
                    # floating equity before target; classify as bar screening.
                    adverse = row["low"] if buy else row["high"]
                    state.update(marked(adverse), balance, now)
                    max_dd = max(max_dd, state.overall_dd_pct)
                    breakers = check_breakers(state.daily_dd_pct, state.overall_dd_pct)
                    if "hard_daily" in breakers or "hard_overall" in breakers:
                        halted = halted or "hard_overall" in breakers
                        daily_halt = trading_day_start(now)
                        close(low if buy else high, now, "circuit_breaker_bar_bound")
                    elif (high >= target if buy else low <= target):
                        close(target, now, "target")
                    elif now >= position["deadline"]:
                        close(row["close"] + (0 if buy else position["spread"]), now, "time_exit")
            state.update(marked(), balance, now)
            max_dd = max(max_dd, state.overall_dd_pct)
            breakers = check_breakers(state.daily_dd_pct, state.overall_dd_pct)
            if "hard_daily" in breakers or "hard_overall" in breakers:
                halted = halted or "hard_overall" in breakers
                daily_halt = trading_day_start(now)
                pending = None
            monthly[now.strftime("%Y-%m")] = marked()
            zone = current_killzone(now)
            if zone is None:
                continue
            if strategy == "ict_2r" or candidate_provider:
                context = dict(pair=pair, now=now, htf_candles=list(model.h1))
                detector = candidate_provider or app._detect_candidate
                bars = app._bars(list(m1[pair]), now, timedelta(minutes=1)) if strategy == "ict_2r" else list(m1[pair])
                candidate = detector(bars, context)
                if candidate and strategy == "ict_2r" and "factors" in candidate:
                    score = app.confluence.score_setup(candidate["factors"], no_high_impact_news_next_15min=True)
                    if score["reason"] or score["score"] < app.MIN_SETUP_SCORE:
                        candidate = None
                    else:
                        index = candidate.get("mss", {}).get("index")
                        if isinstance(index, int) and 0 <= index < len(m1[pair]):
                            candidate = dict(candidate, setup_id=list(m1[pair])[index]["ts"].isoformat())
            if candidate is None:
                continue
            reasons["candidates"] += 1
            candidate = dict(candidate, detected_at=now)
            key = (pair, candidate.get("setup_id", now), candidate["direction"])
            reason = None
            if key in seen:
                reason = "duplicate_setup"
            elif position or pending:
                reason = "position_or_reservation_exists"
            elif halted or daily_halt == trading_day_start(now):
                reason = "account_halted"
            elif now + timedelta(minutes=config.max_hold_minutes) >= killzone_close(zone, now):
                reason = "too_late_in_window"
            if reason:
                reasons[reason] += 1
            else:
                pending = candidate
                seen.add(key)
            emit(dict(type="candidate", now=now, setup=candidate, reason=reason or "pending_next_open"))
        report = dict(strategy=strategy, research_only=True, live_eligible=False,
                      evidence_level="M1 Bid-OHLC screening; real-tick and forward validation required",
                      execution_model="one position; next-open market; fixed R; time exit; stop-first ambiguity",
                      news_policy="skipped: historical news unavailable", cost_source="explicit assumptions, not verified broker costs",
                      risk_score_policy="binary research eligibility; not calibrated ICT confluence",
                      session_policy="unchanged shared live killzones and risk guards",
                      pattern_policy=("M5 close beyond prior 12-bar range; first directional M1 close back across within 15min; stop beyond observed breakout extreme"
                                      if strategy == "failed_breakout" else
                                      "H1 EMA20/3-bar slope; M5 prior-12 breakout; first reclaim in 15min; ATR14/one-pip stop buffer"),
                      config=asdict(config), initial_equity=initial_equity, balance=balance, equity=marked(),
                      metrics=metrics(trades), trades=trades, reasons=dict(reasons), max_drawdown_pct=100 * max_dd,
                      halted=halted, open_position=position, pending=pending,
                      monthly=monthly_statistics(monthly, initial_equity, config.monthly_target))
        emit(dict(type="report", **{k: v for k, v in report.items() if k != "trades"}))
        return report
    finally:
        if output:
            output.close()


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
    parser.add_argument("--research", choices=("ict_2r", "breakout_pullback", "failed_breakout"))
    parser.add_argument("--reward-r", type=float, default=2.)
    parser.add_argument("--risk-pct", type=float, default=DEFAULT_RISK_PCT)
    parser.add_argument("--spread-pips", type=float, default=.5)
    parser.add_argument("--slippage-pips", type=float, default=.1)
    parser.add_argument("--commission-per-lot", type=float, default=7.)
    args = parser.parse_args()
    logger = setup_logging("backtest")
    rows = load_history(args.data_dir)
    if args.research:
        if args.llm:
            parser.error("research screening does not run the LLM")
        config = ResearchConfig(reward_r=args.reward_r, risk_pct=args.risk_pct,
                                spread_pips=args.spread_pips, slippage_pips=args.slippage_pips,
                                commission_per_lot=args.commission_per_lot)
        report = research_replay(rows, strategy=args.research, config=config,
                                 results_path=args.output_dir / "research.jsonl")
        write_report(args.output_dir / "research.json", report)
        logger.info("Research screening only: metrics=%s monthly=%s", report["metrics"], report["monthly"])
        return
    for llm in ([False, True] if args.llm else [False]):
        name = "mechanical_llm" if llm else "mechanical"
        report = replay(rows, threshold=args.threshold, llm=llm, collect_decisions=False,
                        results_path=args.output_dir / f"{name}.jsonl")
        write_report(args.output_dir / f"{name}.json", {k: v for k, v in report.items() if k != "decisions"})
        logger.info("%s: %s; news gate skipped", name, report["metrics"])


if __name__ == "__main__":
    main()
