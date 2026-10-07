"""Shared replay decision chain and unattended live adapter."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import math
import time

from brain.config import load_config
from brain.db import supabase_client as db
from brain.detection import fvg, liquidity, mss, order_block, ote, swings
from brain.llm import reasoning
from brain.logging_setup import setup_logging
from brain.news import blackout
from brain.pairs import pair_spec
from brain.risk import engine
from brain.risk.circuit_breakers import DrawdownState, trading_day_start
from brain.scheduler import killzone_clock
from brain.scoring import confluence

PIPELINE_INTERVAL_SECONDS = 60
MIN_SETUP_SCORE = 8
CANDLE_LIMIT = 300
logger = logging.getLogger("brain")


@dataclass(frozen=True)
class PipelineDecision:
    approved: bool
    reason: str
    setup: dict | None = None
    score_result: dict | None = None
    llm: dict | None = None
    risk_result: dict | None = None
    signal: dict | None = None
    stages: tuple[str, ...] = ()


def _resolve(value):
    return value() if callable(value) else value


def _timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return result.astimezone(timezone.utc)


def _bars(candles, now, duration):
    # NOTE: Only completed M1/H1 bars enter detection, with chronological local
    # indices; live and replay share normalization to avoid look-ahead.
    rows = sorted((dict(c) for c in candles if _timestamp(c["ts"]) + duration <= now),
                  key=lambda c: _timestamp(c["ts"]))
    return [dict(c, index=i, **{k: float(c[k]) for k in ("open", "high", "low", "close")})
            for i, c in enumerate(rows)]


def _detect_candidate(candles, context):
    htf = _bars(context["htf_candles"], context["now"], timedelta(hours=1))
    if len(candles) < 16 or len(htf) < 2:
        return None
    # NOTE: Bias uses the last two H1 closes; ATR averages 14 true ranges.
    # Entry uses the latest post-MSS FVG midpoint, stop extends one pip beyond
    # the sweep, and target uses the nearest untapped opposing liquidity pool.
    if htf[-1]["close"] == htf[-2]["close"]:
        return None
    bias = "bullish" if htf[-1]["close"] > htf[-2]["close"] else "bearish"
    ranges = [max(c["high"] - c["low"], abs(c["high"] - p["close"]),
                  abs(c["low"] - p["close"])) for p, c in zip(candles[:-1], candles[1:])]
    atr = sum(ranges[-14:]) / 14
    if not math.isfinite(atr) or atr <= 0:
        return None
    pivots = swings.find_swing_points(candles)
    shift = mss.detect_mss(candles, pivots, bias, atr)
    if shift is None:
        return None
    before = candles[:shift["index"]]
    opposite = "low" if bias == "bullish" else "high"
    # The pool must predate the five-candle sweep window, otherwise the sweep
    # wick itself could become the reference pivot and hide the actual breach.
    pivot = next((p for p in reversed(pivots)
                  if p.type == opposite and p.index < shift["index"] - 5), None)
    sweep = liquidity.detect_liquidity_sweep(before, pivot.price,
        "buy_side" if bias == "bullish" else "sell_side") if pivot else None
    gaps = [g for g in fvg.detect_fvg(candles, bias) if g["index"] >= shift["index"]]
    block = order_block.find_order_block(candles, shift["index"], bias)
    target = liquidity.find_nearest_liquidity_pool(candles, bias, lookback=len(candles))
    if not sweep or not gaps or target is None:
        return None
    gap = gaps[-1]
    entry = (gap["top"] + gap["bottom"]) / 2
    spec = pair_spec(context["pair"], candles[-1]["close"])
    stop = sweep["wick_extreme"] + (-spec["pip_size"] if bias == "bullish" else spec["pip_size"])
    end = (max(c["high"] for c in candles[shift["index"]:]) if bias == "bullish"
           else min(c["low"] for c in candles[shift["index"]:]))
    zone = ote.calculate_ote_zone(sweep["wick_extreme"], end)
    if not (stop < entry < target["level"] if bias == "bullish" else target["level"] < entry < stop):
        return None
    return dict(pair=context["pair"], direction="buy" if bias == "bullish" else "sell",
        entry=entry, stop_loss=stop, take_profit=target["level"], htf_bias=bias,
        sweep=sweep, mss=shift, fvg=gap, order_block=block, atr=atr,
        factors=dict(htf_bias_aligned=True, inside_killzone=True,
            liquidity_sweep_before_mss=sweep["index"] < shift["index"],
            displacement_ge_1_5x_atr=shift["displacement"] >= 1.5 * atr,
            fvg_present_unmitigated=True, entry_in_ote_zone=ote.price_in_ote(entry, zone)))


def evaluate_candles(candles, context) -> PipelineDecision:
    """Pure orchestration: no clock, writes or implicit network access.

    Context is a dict containing aware now, pair, htf_candles (H1), news_events
    and risk_state. candles/news_events/risk_state may be injected lazy readers.
    risk_state contains equity, drawdown_state, trades_today,
    consecutive_losses_today, open_positions. Inject review_setup for live or
    replay; omission fails closed. Other replaceable callables are
    current_killzone, detect_candidate, score_setup, is_in_news_blackout and
    evaluate_signal. on_stage is an optional audit sink; stages are also returned.
    Inputs are never mutated. Replay should supply deterministic callables.
    """
    now = _timestamp(context["now"])
    stages = []
    setup = score = llm = risk = None

    def stage(message):
        stages.append(message)
        if context.get("on_stage"):
            context["on_stage"](message)

    def finish(reason, signal=None):
        stage(f"decision: {reason}")
        return PipelineDecision(signal is not None, reason, setup, score, llm, risk,
                                signal, tuple(stages))

    zone = context.get("current_killzone", killzone_clock.current_killzone)(now)
    stage(f"killzone: {zone}")
    if zone is None:
        return finish("outside_killzone")
    candles = _bars(_resolve(candles), now, timedelta(minutes=1))
    stage(f"candles: {len(candles)} completed M1 bars")
    events = _resolve(context.get("news_events", []))
    blocked = context.get("is_in_news_blackout", blackout.is_in_news_blackout)(now, context["pair"], events)
    stage(f"news: blackout={blocked}")
    setup = context.get("detect_candidate", _detect_candidate)(candles, dict(context, now=now))
    stage(f"detection: {setup}")
    if setup is None:
        return finish("no_candidate")
    factors = dict(setup["factors"], inside_killzone=True)
    score = context.get("score_setup", confluence.score_setup)(
        factors, no_high_impact_news_next_15min=not blocked)
    stage(f"score: {score}")
    if score.get("reason") or score["score"] < MIN_SETUP_SCORE:
        return finish(score.get("reason") or "score_below_minimum")
    review = context.get("review_setup")
    if review is None:
        return finish("llm_review_unavailable")
    spec = pair_spec(context["pair"], candles[-1]["close"])
    upcoming = [e for e in events if str(e.get("impact", "")).lower() == "high"
                and e.get("currency") in (context["pair"][:3], context["pair"][3:])
                and _timestamp(e["scheduled_at"]) >= now]
    next_event = min(upcoming, key=lambda e: _timestamp(e["scheduled_at"]), default=None)
    # NOTE: Account performance remains explicitly unknown to the LLM until
    # its verdict passes, preserving the required veto-before-account-read gate.
    llm_context = dict(pair=context["pair"], htf_bias=setup["htf_bias"], killzone_name=zone,
        sweep_description=str(setup["sweep"]), displacement_pips=setup["mss"]["displacement"] / spec["pip_size"],
        displacement_atr_ratio=setup["mss"]["displacement"] / setup["atr"],
        fvg_top=setup["fvg"]["top"], fvg_bottom=setup["fvg"]["bottom"],
        ote_overlap_bool=factors["entry_in_ote_zone"], mechanical_score=score["score"],
        candles_summary=str(candles[-20:]), recent_headlines=context.get("recent_headlines", []),
        next_event_name=next_event.get("title", "unnamed") if next_event else "none in news window",
        next_event_minutes_until=(_timestamp(next_event["scheduled_at"]) - now).total_seconds() / 60 if next_event else None,
        realized_r_today="unknown until risk stage", daily_dd_used="unknown until risk stage",
        daily_dd_limit=0.0325, trades_taken_today="unknown until risk stage")
    stage("llm: reviewing setup")
    verdict = review(llm_context)
    llm = verdict.model_dump() if hasattr(verdict, "model_dump") else dict(verdict)
    stage(f"llm: {llm}")
    if llm["verdict"] == "veto":
        return finish("llm_veto")
    if llm["verdict"] not in {"approve", "downweight"}:
        return finish("invalid_llm_verdict")
    score = dict(score, score=score["score"] - (llm["verdict"] == "downweight"))
    stage(f"effective score: {score['score']}")
    stage("risk: loading account state")
    state = _resolve(context["risk_state"])
    account = dict(equity=state["equity"], pair=context["pair"], direction=setup["direction"],
        stop_loss_pips=abs(setup["entry"] - setup["stop_loss"]) / spec["pip_size"],
        pip_value_per_lot=spec["pip_value_per_lot"])
    stage(f"risk inputs: account={account} trades={state['trades_today']} "
          f"loss_streak={state['consecutive_losses_today']} positions={state['open_positions']} "
          f"drawdown={state['drawdown_state']}")
    risk = context.get("evaluate_signal", engine.evaluate_signal)(score, state["drawdown_state"], account,
        state["open_positions"], state["trades_today"], state["consecutive_losses_today"], now)
    stage(f"risk: {risk}")
    if not risk["approved"]:
        return finish(risk["reason"])
    signal = {key: setup[key] for key in ("pair", "direction", "entry", "stop_loss", "take_profit")}
    signal.update(status="pending", lot_size=risk["lot_size"], confluence_score=score["score"],
        killzone=zone, detected_at=now, llm_verdict=llm["verdict"],
        llm_conviction=llm["conviction"], llm_reasoning=llm["reasoning"])
    return finish("approved", signal)


def _news_events(now):
    # NOTE: get_upcoming_news omits past events and uses its own clock. This
    # bounded, paginated read uses the existing client and preserves ±15min.
    query = db._get_client().table("news_events").select("*").gte(
        "scheduled_at", (now - timedelta(minutes=15)).isoformat()).lte(
        "scheduled_at", (now + timedelta(minutes=15)).isoformat()).order("scheduled_at").order("id")
    rows = []
    while True:
        page = query.range(len(rows), len(rows) + 999).execute().data
        if page is None:
            raise RuntimeError("news query returned no data")
        rows.extend(page)
        if len(page) < 1000:
            return rows


def _risk_state(now):
    start = trading_day_start(now)
    latest = db.get_latest_equity_snapshot()
    snapshots = db.get_equity_snapshots(start)
    peak = db.get_max_equity()
    trades = db.get_trades_since(start)
    positions = db.get_open_trades()
    if not latest or not snapshots or peak is None:
        logger.error("risk rejected: missing persisted equity baseline/high-water mark")
        raise RuntimeError("missing persisted equity baseline/high-water mark")
    # NOTE: Earliest snapshot since rollover approximates the daily baseline;
    # snapshots at rollover are needed for the exact reset balance.
    grouped = {}
    for trade in trades:
        grouped.setdefault(trade["signal_id"], []).append(trade)
    # NOTE: Executor legs count as one setup; only fully closed setups enter
    # the loss streak, ordered by their last close and summed realized USD.
    closed = [(max(_timestamp(t["closed_at"]) for t in legs),
               sum(float(t["realized_usd"]) for t in legs))
              for legs in grouped.values() if all(t.get("closed_at") for t in legs)]
    losses = 0
    for _, pnl in sorted(closed, reverse=True):
        if pnl >= 0:
            break
        losses += 1
    return dict(equity=float(latest["equity"]), drawdown_state=DrawdownState(
        float(snapshots[0]["balance"]), float(latest["equity"]), float(peak), start),
        trades_today=len(grouped), consecutive_losses_today=losses,
        open_positions=list({t["signal_id"]: t for t in positions}.values()))


def run_pipeline_once(now=None):
    """Gather live inputs, evaluate, audit and persist approved signals."""
    now = _timestamp(now if now is not None else datetime.now(timezone.utc))
    if killzone_clock.current_killzone(now) is None:
        logger.info("killzone: outside_killzone; skipping all reads and detection")
        return [PipelineDecision(False, "outside_killzone")]
    decisions = []
    for pair in load_config().TRADED_PAIRS:
        try:
            logger.info("%s pipeline started at %s; fetching M1/H1 candles", pair, now)
            candles = db.get_latest_candles(pair, "M1", CANDLE_LIMIT)
            htf = db.get_latest_candles(pair, "H1", CANDLE_LIMIT)
            decision = evaluate_candles(candles, dict(now=now, pair=pair, htf_candles=htf,
                news_events=lambda: _news_events(now), review_setup=reasoning.review_setup,
                risk_state=lambda: _risk_state(now),
                on_stage=lambda message: logger.info("%s %s", pair, message)))
            decisions.append(decision)
            if decision.approved:
                signal_id = db.insert_signal(decision.signal)
                if signal_id is None:
                    logger.error("%s pending signal insert failed", pair)
                else:
                    logger.info("%s pending signal inserted id=%s lot_size=%s", pair, signal_id, decision.signal["lot_size"])
        except Exception as exc:
            logger.error("%s pipeline iteration failed (%s)", pair, type(exc).__name__)
    return decisions


def main() -> None:
    setup_logging("brain")
    try:
        while True:
            try:
                run_pipeline_once()
            except Exception as exc:
                logger.error("Pipeline iteration failed (%s); backing off", type(exc).__name__)
            time.sleep(PIPELINE_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Brain pipeline stopped")


if __name__ == "__main__":
    main()
