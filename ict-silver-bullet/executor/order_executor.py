"""Poll approved signals, execute protected legs, and manage broker positions."""
# EC2 demo-account manual checks (never begin on a funded account):
# 1. Configure MT5/DB, enable Python trading, use a HEDGING account, start all
#    executor processes; run python -m executor.order_executor. A second copy
#    must fail its singleton lock. Check correct account/server in the journal.
# 2. Submit pending buy/sell signals inside each NY window with valid 2R TP,
#    entry/SL and lots. Verify scalp+runner total never exceeds requested lots,
#    actual deals populate two trades, broker SL/TP, status and retcode logs.
# 3. Submit below-min, above-max, off-step, unsplittable, stale-window, invalid
#    price, and netting-account signals; verify rejection with no order placed.
# 4. Exercise FOK/IOC/RETURN brokers and disabled trading/invalid stops/requotes;
#    verify retcodes, partial fills, and no blind retries or duplicate exposure.
# 5. Reach scalp TP (2R): verify primary exits, runner SL becomes actual entry,
#    HTF swing TP remains, and confirmed M15 swings only tighten its stop.
# 6. End a window before 1R: verify market exits of both unactivated legs; repeat
#    after touching 1R and verify survival. Restart mid-trade and repeat.
# 7. At Friday 16:50 NY verify ALL account positions flatten, new entries are
#    blocked beforehand, and failed closes are retried on subsequent polls.
# 8. Kill process before/after order_send and interrupt DB; restart with journal
#    intact, verify deal reconciliation/no resend, trade exit/P&L reporting and
#    status repair. Ambiguous sends intentionally block new entries: investigate
#    broker history before operator repair. Preserve runtime files on upgrades.
# 9. Stop/restart MT5 and restore connectivity; verify recovery. Ctrl+C exits.

from datetime import datetime, time as wall_time, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import json
import logging
import math
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5

from executor.db_client import get_pending_signals, update_signal_status, update_trade
from executor.logging_setup import setup_logging
from executor.mt5_bridge import connect
from executor.trade_reporter import report_fill

logger = logging.getLogger("executor")
UTC = timezone.utc
NY = ZoneInfo("America/New_York")
RUNNER_FRACTION = Decimal("0.35")
DEVIATION_POINTS = 20
MAGIC = 260107
FRIDAY_FLATTEN = wall_time(16, 50)
MIN_TIME_BEFORE_FLATTEN = timedelta(hours=1)
WINDOWS = {"london": (wall_time(3), wall_time(4)),
           "ny_am": (wall_time(10), wall_time(11)),
           "ny_pm": (wall_time(14), wall_time(15))}
STATE_PATH = Path(os.environ.get("EXECUTOR_STATE_DIR", Path(__file__).parent / "runtime")) / "orders.json"


def aware(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if value.tzinfo is None:
        raise ValueError("aware datetime required")
    return value.astimezone(UTC)


def killzone_close(name, now):
    """Same NY-date window logic as scheduler, without Brain imports. Test candidate."""
    local = aware(now).astimezone(NY)
    return datetime.combine(local.date(), WINDOWS[name][1], NY).astimezone(UTC)


def friday_guard(now, opening=False):
    """Flatten Friday 16:50 through Sunday 17:00 NY. Candidate for tests."""
    local = aware(now).astimezone(NY)
    if local.weekday() == 5 or (local.weekday() == 6 and local.time() < wall_time(17)):
        return True
    if local.weekday() != 4:
        return False
    flatten = datetime.combine(local.date(), FRIDAY_FLATTEN, NY)
    return local >= flatten - (MIN_TIME_BEFORE_FLATTEN if opening else timedelta())


def validate_volume(volume, info):
    """Reject off-grid or out-of-range lots, never round them up. Test candidate."""
    amount = Decimal(str(volume))
    low, high, step = (Decimal(str(getattr(info, key))) for key in
                       ("volume_min", "volume_max", "volume_step"))
    if not amount.is_finite() or step <= 0 or not low <= amount <= high:
        raise ValueError("lot size outside broker bounds")
    if amount % step:
        raise ValueError("lot size not on broker volume grid")
    return amount


def split_volume(volume, info):
    """Floor runner allocation, retain exact total; reject undersized legs. Test candidate."""
    total = validate_volume(volume, info)
    step = Decimal(str(info.volume_step))
    runner = (total * RUNNER_FRACTION / step).to_integral_value(rounding=ROUND_FLOOR) * step
    primary = total - runner
    validate_volume(primary, info)
    validate_volume(runner, info)
    return float(primary), float(runner)


def filling_mode(info):
    # SYMBOL_FILLING_* are bit flags; ORDER_FILLING_* are enum values.
    if info.trade_exemode in (mt5.SYMBOL_TRADE_EXECUTION_INSTANT, mt5.SYMBOL_TRADE_EXECUTION_REQUEST):
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    if info.trade_exemode != mt5.SYMBOL_TRADE_EXECUTION_MARKET:
        return mt5.ORDER_FILLING_RETURN
    raise ValueError("symbol permits no supported market filling policy")


def send(request):
    result = mt5.order_send(request)
    code = getattr(result, "retcode", None)
    logger.info("MT5 action=%s symbol=%s position=%s retcode=%s deal=%s order=%s",
                request["action"], request.get("symbol"), request.get("position"),
                code, getattr(result, "deal", None), getattr(result, "order", None))
    if result is None:
        logger.error("MT5 order_send returned None; last_error=%s", mt5.last_error())
    return result


def save(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(STATE_PATH)


def positions():
    result = mt5.positions_get()
    if result is None:
        raise RuntimeError("MT5 positions unavailable")
    return result


def close_position(position):
    info = mt5.symbol_info(position.symbol)
    tick = mt5.symbol_info_tick(position.symbol)
    if info is None or tick is None:
        raise RuntimeError("missing symbol/tick for close")
    buy = position.type == mt5.POSITION_TYPE_BUY
    return send({"action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
                 "position": position.ticket, "volume": position.volume,
                 "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                 "price": tick.bid if buy else tick.ask,
                 "deviation": DEVIATION_POINTS, "magic": MAGIC,
                 "comment": "ICT exit", "type_time": mt5.ORDER_TIME_GTC,
                 "type_filling": filling_mode(info)})


def swings(rates, buy):
    """Confirmed 2-left/2-right structure extrema; rates exclude forming bar. Test candidate."""
    field = "low" if buy else "high"
    points = []
    for i in range(2, len(rates) - 2):
        price = float(rates[i][field])
        neighbours = [float(rates[j][field]) for j in (i-2, i-1, i+1, i+2)]
        if all(price < p if buy else price > p for p in neighbours):
            points.append((int(rates[i]["time"]), price))
    return points


def runner_target(symbol, buy, primary_tp):
    # NOTE: No HTF pool field exists in signals. Use nearest confirmed H1
    # swing high/low beyond scalp TP in the last 300 closed bars; reject if none.
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 300)
    if rates is None:
        raise RuntimeError("HTF candles unavailable")
    pools = [price for _, price in swings(rates, not buy)
             if (price > primary_tp if buy else price < primary_tp)]
    if not pools:
        raise ValueError("no HTF liquidity target beyond scalp TP")
    return min(pools) if buy else max(pools)


def trail_stop(current, structure, buy, info):
    """One tick outside structure, never loosen an existing stop. Test candidate."""
    step = Decimal(str(info.trade_tick_size or info.point))
    value = Decimal(str(structure)) + (-step if buy else step)
    rounding = ROUND_FLOOR if buy else ROUND_CEILING
    candidate = float((value / step).to_integral_value(rounding=rounding) * step)
    return max(current, candidate) if buy else min(current, candidate) if current else candidate


def modify_stop(position, new_stop):
    info = mt5.symbol_info(position.symbol)
    tick = mt5.symbol_info_tick(position.symbol)
    if info is None or tick is None:
        raise RuntimeError("missing stop-modification symbol/tick")
    buy = position.type == mt5.POSITION_TYPE_BUY
    distance = max(info.trade_stops_level, info.trade_freeze_level) * info.point
    if (tick.bid - new_stop if buy else new_stop - tick.ask) <= distance:
        return False
    if position.sl and (new_stop <= position.sl if buy else new_stop >= position.sl):
        return True
    result = send({"action": mt5.TRADE_ACTION_SLTP, "symbol": position.symbol,
                   "position": position.ticket, "sl": new_stop, "tp": position.tp,
                   "magic": MAGIC})
    return getattr(result, "retcode", None) == mt5.TRADE_RETCODE_DONE


def prepare_signal(signal, now, account):
    if account.margin_mode != mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
        # NOTE: Independent scalp/runner broker SL/TP require hedging. Reject
        # netting rather than silently merging legs or changing other positions.
        raise ValueError("two-leg model requires a hedging account")
    if friday_guard(now, opening=True):
        raise ValueError("insufficient time before Friday flatten")
    detected = aware(signal["detected_at"])
    name = signal["killzone"]
    local = detected.astimezone(NY)
    start = datetime.combine(local.date(), WINDOWS[name][0], NY).astimezone(UTC)
    end = killzone_close(name, detected)
    if not start <= detected <= now < end:
        raise ValueError("stale signal or outside its killzone")
    symbol = signal["pair"]
    if not mt5.symbol_select(symbol, True):
        raise ValueError("symbol unavailable")
    info, tick = mt5.symbol_info(symbol), mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        raise ValueError("missing symbol info/tick")
    direction = signal["direction"].lower()
    if direction not in ("buy", "sell", "long", "short"):
        raise ValueError("unknown direction")
    buy = direction in ("buy", "long")
    entry, stop, tp = (float(signal[k]) for k in ("entry", "stop_loss", "take_profit"))
    if not all(math.isfinite(x) and x > 0 for x in (entry, stop, tp)):
        raise ValueError("invalid signal prices")
    if not (stop < entry < tp if buy else tp < entry < stop):
        raise ValueError("inverted protective prices")
    risk = abs(entry - stop)
    # NOTE: Signal take_profit must be 2R within one broker tick; use its exact
    # price. Do not silently replace a differently configured Brain target.
    if abs(abs(tp - entry) - 2 * risk) > max(info.trade_tick_size, info.point) * 1.01:
        raise ValueError("scalp take_profit must equal 2R")
    quote = tick.ask if buy else tick.bid
    if abs(quote - entry) > DEVIATION_POINTS * info.point:
        raise ValueError("entry too far from current market")
    primary, runner = split_volume(signal["lot_size"], info)
    target = runner_target(symbol, buy, tp)
    policy = filling_mode(info)
    record = {"symbol": symbol, "buy": buy, "stop": stop, "tp": tp,
              "close": end.isoformat(), "created": now.isoformat(),
              "reached_1r": False, "runner_active": False, "legs": []}
    for leg, lots, target_price in (("scalp", primary, tp), ("runner", runner, target)):
        comment = f"ICT:{signal['id']}:{leg}"
        if len(comment) > 31:
            raise ValueError("signal ID too long for broker comment")
        record["legs"].append({"name": leg, "attempted": False, "comment": comment,
            "request": {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
                        "volume": lots, "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
                        "price": entry, "sl": stop, "tp": target_price,
                        "deviation": DEVIATION_POINTS, "magic": MAGIC,
                        "comment": comment, "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": policy}})
    return record


def execute_signal(signal, state, account):
    key = str(signal["id"])
    try:
        record = prepare_signal(signal, datetime.now(UTC), account)
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        logger.warning("Signal %s rejected before send: %s; MT5 retcode=not_sent", key, exc)
        state["signals"][key] = {"status": "rejected", "legs": []}
        save(state)
        update_signal_status(int(key), "rejected")
        return
    state["signals"][key] = record
    save(state)
    for leg in record["legs"]:
        # NOTE: Persist intent BEFORE send. An ambiguous result is reconciled
        # from broker history, never blindly resent (fixed DB contract has no CAS).
        leg["attempted"] = True
        save(state)
        try:
            result = send(leg["request"])
        except BaseException:
            record["abort"] = True
            save(state)
            raise
        leg["retcode"] = getattr(result, "retcode", None)
        leg["deal"] = getattr(result, "deal", 0)
        leg["order"] = getattr(result, "order", 0)
        save(state)
        if leg["retcode"] != mt5.TRADE_RETCODE_DONE:
            # Partial fill is real exposure; reconcile it, abort second leg,
            # and unwind all fills from this incomplete two-leg execution.
            record["abort"] = True
            save(state)
            break
    record["execution_complete"] = True
    save(state)


def reconcile_leg(record, leg, live):
    if not leg["attempted"]:
        return None, ()
    if not leg.get("identifier"):
        history = mt5.history_deals_get(aware(record["created"]) - timedelta(seconds=5), datetime.now(UTC))
        if history is None:
            raise RuntimeError("MT5 deal history unavailable")
        matching = [d for d in history if d.magic == MAGIC and d.symbol == record["symbol"]
                    and d.entry == mt5.DEAL_ENTRY_IN and
                    (d.comment == leg["comment"] or d.ticket == leg.get("deal")
                     or (leg.get("order") and d.order == leg["order"]))]
        position = next((p for p in live if p.magic == MAGIC and p.comment == leg["comment"]), None)
        if matching:
            leg["identifier"] = int(matching[0].position_id)
        elif position:
            leg["identifier"] = int(position.identifier)
        else:
            return None, ()
    deals = mt5.history_deals_get(position=leg["identifier"])
    if deals is None:
        raise RuntimeError("MT5 position history unavailable")
    position = next((p for p in live if p.identifier == leg["identifier"]), None)
    return position, deals


def report_leg(signal_id, record, leg, position, deals):
    entries = [d for d in deals if d.entry == mt5.DEAL_ENTRY_IN]
    exits = [d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
    volume = sum(d.volume for d in entries)
    if volume <= 0:
        return
    entry = sum(d.volume * d.price for d in entries) / volume
    leg.update(entry=entry, risk=abs(entry - record["stop"]),
               entry_time=datetime.fromtimestamp(min(d.time for d in entries), UTC).isoformat())
    fill = {"leg": leg["name"], "entry_fill": entry, "lots": volume,
            "opened_at": datetime.fromtimestamp(min(d.time for d in entries), UTC).isoformat(),
            "status": "open"}
    if position is None and exits and sum(d.volume for d in exits) >= volume - 1e-8:
        exit_price = sum(d.price * d.volume for d in exits) / sum(d.volume for d in exits)
        sign = 1 if record["buy"] else -1
        fill.update(exit_fill=exit_price, closed_at=datetime.fromtimestamp(max(d.time for d in exits), UTC).isoformat(),
                    realized_r=sign * (exit_price - entry) / leg["risk"] if leg["risk"] else 0,
                    realized_usd=sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0) for d in deals),
                    status="closed")
        leg["closed"] = True
        leg["tp_exit"] = any(d.reason == mt5.DEAL_REASON_TP for d in exits) or (
            sign * (exit_price - record["tp"]) >= -1e-8)
    if not leg.get("trade_id"):
        # NOTE: insert_trade lacks an idempotency key/read-back contract. A lost
        # response can duplicate a reporting row on retry, never a broker order.
        leg["trade_id"] = report_fill(int(signal_id), fill)
    elif fill["status"] == "closed":
        update_trade(leg["trade_id"], {k: v for k, v in fill.items() if k not in ("leg", "opened_at")})


def manage_signal(key, record, state, live):
    if not record["legs"]:
        update_signal_status(int(key), record["status"])
        return False
    active, uncertain = [], False
    # Restart in the gap between legs: reconcile/unwind, never complete the
    # second entry against a potentially different market or duplicate the first.
    if not record.get("execution_complete"):
        record["abort"] = True
    for leg in record["legs"]:
        position, deals = reconcile_leg(record, leg, live)
        if deals:
            report_leg(key, record, leg, position, deals)
        if position:
            active.append((leg, position))
        elif leg["attempted"] and not leg.get("identifier"):
            # A definite rejection cannot have executed; None/timeouts/PLACED
            # stay quarantined until history proves the disposition.
            code = leg.get("retcode")
            ambiguous = (None, mt5.TRADE_RETCODE_TIMEOUT, mt5.TRADE_RETCODE_CONNECTION,
                         mt5.TRADE_RETCODE_PLACED, mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL)
            uncertain |= code in ambiguous
    filled = any(leg.get("identifier") for leg in record["legs"])
    if filled:
        record["status"] = "filled"
        update_signal_status(int(key), "filled")
    elif not uncertain:
        record["status"] = "rejected"
        update_signal_status(int(key), "rejected")
    save(state)
    if uncertain:
        logger.error("Signal %s has an unresolved send; new entries quarantined", key)
    if record.get("abort"):
        orders = mt5.orders_get(symbol=record["symbol"])
        if orders is None:
            raise RuntimeError("cannot reconcile outstanding broker orders")
        for order in orders:
            if order.magic == MAGIC and any(order.comment == leg["comment"] or
                    order.ticket == leg.get("order") for leg in record["legs"]):
                result = send({"action": mt5.TRADE_ACTION_REMOVE, "order": order.ticket})
                uncertain |= getattr(result, "retcode", None) != mt5.TRADE_RETCODE_DONE
        for _, position in active:
            close_position(position)
        return uncertain
    scalp = record["legs"][0]
    if scalp.get("closed") and scalp.get("tp_exit"):
        record["runner_active"] = True
        record.setdefault("activated_at", datetime.now(UTC).isoformat())
    now = datetime.now(UTC)
    tick = mt5.symbol_info_tick(record["symbol"])
    if tick is None:
        raise RuntimeError("missing management tick")
    price = tick.bid if record["buy"] else tick.ask
    sign = 1 if record["buy"] else -1
    if now < aware(record["close"]) and scalp.get("entry") and sign * (price - scalp["entry"]) >= scalp["risk"]:
        record["reached_1r"] = True
    # Recover executable-price excursions missed between polls/restarts using
    # broker bid/ask tick history, strictly bounded by the killzone close.
    if not record["reached_1r"] and scalp.get("entry"):
        until = min(now, aware(record["close"]))
        since = aware(record.get("last_checked", scalp["entry_time"]))
        ticks = mt5.copy_ticks_range(record["symbol"], since, until, mt5.COPY_TICKS_ALL) if since < until else ()
        if ticks is None:
            raise RuntimeError("cannot determine scalp 1R excursion")
        field = "bid" if record["buy"] else "ask"
        record["reached_1r"] = any(float(tick[field]) > 0 and
            sign * (float(tick[field]) - scalp["entry"]) >= scalp["risk"] for tick in ticks)
        record["last_checked"] = until.isoformat()
    save(state)
    if now >= aware(record["close"]) and not record["reached_1r"] and not record["runner_active"]:
        for _, position in active:
            close_position(position)
        return uncertain
    for leg, position in active:
        if leg["name"] == "scalp":
            if sign * (price - record["tp"]) >= 0:
                close_position(position)
        elif record["runner_active"]:
            if not modify_stop(position, leg["entry"]):
                continue
            info = mt5.symbol_info(record["symbol"])
            rates = mt5.copy_rates_from_pos(record["symbol"], mt5.TIMEFRAME_M15, 1, 100)
            if info is None or rates is None:
                raise RuntimeError("M15 structure unavailable")
            points = [(ts, value) for ts, value in swings(rates, record["buy"])
                      if ts >= aware(record["activated_at"]).timestamp() and ts > leg.get("last_structure", 0)]
            if points:
                ts, value = points[-1]
                baseline = max(position.sl, leg["entry"]) if record["buy"] else min(
                    position.sl or leg["entry"], leg["entry"])
                new_stop = trail_stop(baseline, value, record["buy"], info)
                if modify_stop(position, new_stop):
                    leg["last_structure"] = ts
                    save(state)
    return uncertain


def poll_and_execute(interval_seconds=3):
    if interval_seconds <= 0:
        raise ValueError("poll interval must be positive")
    # Windows byte-range lock is released by OS even after process termination.
    import msvcrt
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.with_suffix(".lock").open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {"signals": {}}
        while True:
            try:
                account = mt5.account_info()
                if account is None:
                    connect()
                    account = mt5.account_info()
                if account is None:
                    raise RuntimeError("MT5 account unavailable")
                account_key = f"{account.login}:{account.server}"
                if state.get("account", account_key) != account_key:
                    raise RuntimeError("journal account mismatch; operator review required")
                state["account"] = account_key
                live = positions()
                flatten = friday_guard(datetime.now(UTC))
                if flatten:
                    for position in live:
                        try:
                            close_position(position)
                        except Exception:
                            logger.exception("Friday close failed for %s", position.ticket)
                    live = positions()
                uncertain = False
                tracked_comments = {leg["comment"] for record in state["signals"].values()
                                    for leg in record["legs"]}
                if any(p.magic == MAGIC and p.comment not in tracked_comments and not any(
                        leg.get("identifier") == p.identifier for record in state["signals"].values()
                        for leg in record["legs"]) for p in live):
                    logger.error("Untracked executor position: restore journal before opening new entries")
                    uncertain = True
                for key, record in list(state["signals"].items()):
                    try:
                        uncertain |= manage_signal(key, record, state, live)
                    except Exception:
                        uncertain = True
                        logger.exception("Management failed for signal %s", key)
                save(state)
                for signal in get_pending_signals():
                    if str(signal["id"]) in state["signals"]:
                        continue
                    if uncertain:
                        break
                    execute_signal(signal, state, account)
                    # Reconcile each send before another signal may open.
                    key = str(signal["id"])
                    uncertain |= manage_signal(key, state["signals"][key], state, positions())
            except Exception:
                logger.exception("Order executor iteration failed")
            time.sleep(interval_seconds)


def main():
    setup_logging("executor")
    try:
        while True:
            try:
                connect()
                poll_and_execute()
            except OSError:
                # Singleton/journal filesystem errors require operator action.
                logger.exception("Executor lock or journal unavailable")
                return
            except Exception:
                logger.exception("Executor startup failed; retrying")
                time.sleep(3)
    except KeyboardInterrupt:
        logger.info("Order executor stopped")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
