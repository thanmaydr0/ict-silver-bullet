"""Continuous equity snapshots and normalized trade fills."""
# EC2 demo-account manual checks:
# 1. Start MT5, configure executor settings, run python -m executor.trade_reporter;
#    verify UTC DB snapshots every 3s, even without trades, and compare MT5 equity.
# 2. Cross 17:00 NY (also DST dates), restart, and verify start balance includes
#    all deals/deposits/commissions since the boundary. Check losing open positions.
# 3. Establish a new equity high, draw down, restart, and verify retained HWM and
#    fractional DD values (0.025 = 2.5%) against Brain thresholds.
# 4. Disconnect DB/terminal then restore; verify loop recovery and Ctrl+C exit.
# 5. Execute scalp+runner through executor; check both trades and exit/P&L updates.

from datetime import datetime, time as wall_time, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5

from executor.db_client import insert_trade, insert_equity_snapshot, get_latest_equity_snapshot
from executor.logging_setup import setup_logging
from executor.mt5_bridge import connect

logger = logging.getLogger("executor")
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
STATE_PATH = Path(os.environ.get("EXECUTOR_STATE_DIR", Path(__file__).parent / "runtime")) / "equity.json"


def trading_day_start(now: datetime) -> datetime:
    """17:00 NY boundary in UTC. Candidate for tests (including DST)."""
    if now.tzinfo is None:
        raise ValueError("aware datetime required")
    local = now.astimezone(NY)
    day = local.date() - timedelta(days=local.time() < wall_time(17))
    return datetime.combine(day, wall_time(17), NY).astimezone(UTC)


def drawdown_pct(equity: float, baseline: float) -> float:
    """Fractional drawdown, matching Brain (may be negative daily). Test candidate."""
    if not all(math.isfinite(x) for x in (equity, baseline)) or baseline <= 0:
        raise ValueError("invalid drawdown baseline")
    return (baseline - equity) / baseline


def report_fill(signal_id, fill_result):
    """Write normalized broker leg fields and return insert_trade's row ID."""
    if float(fill_result["lots"]) <= 0:
        raise ValueError("cannot report a zero-volume fill")
    fields = ("leg", "entry_fill", "exit_fill", "lots", "realized_r",
              "realized_usd", "opened_at", "closed_at", "status")
    trade = {key: fill_result[key] for key in fields if key in fill_result}
    trade.update(signal_id=int(signal_id), status=fill_result.get("status", "open"))
    return insert_trade(trade)


def report_equity_snapshot():
    account = mt5.account_info()
    if account is None:
        raise RuntimeError("MT5 account_info unavailable")
    equity, balance = float(account.equity), float(account.balance)
    if not all(math.isfinite(x) for x in (equity, balance)):
        raise ValueError("invalid account values")
    now = datetime.now(UTC)
    start = trading_day_start(now)
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    account_key = f"{account.login}:{account.server}"
    if state.get("account") != account_key:
        state = {"account": account_key}
    latest = get_latest_equity_snapshot()
    hwm = max(float(state.get("hwm", equity)), equity)
    if latest:
        # NOTE: Schema has no HWM column; recover it from equity/overall DD,
        # and retain a local durable HWM across DB outages (one account per DB).
        dd = float(latest["overall_dd_pct"])
        if 0 <= dd < 1:
            hwm = max(hwm, float(latest["equity"]) / (1 - dd))
    if state.get("day") != start.isoformat():
        # NOTE: Reconstruct 17:00 balance after downtime using ALL cash flows,
        # including deposits, withdrawals, fees, swap and commission.
        deals = mt5.history_deals_get(start, now)
        if deals is None:
            raise RuntimeError("cannot reconstruct daily start balance")
        cash_change = sum(sum(float(getattr(d, field, 0)) for field in
                              ("profit", "commission", "swap", "fee")) for d in deals)
        state.update(day=start.isoformat(), start_balance=balance - cash_change)
    snapshot = {"equity": equity, "balance": balance,
                "daily_dd_pct": drawdown_pct(equity, float(state["start_balance"])),
                "overall_dd_pct": drawdown_pct(equity, hwm), "ts": now.isoformat()}
    state["hwm"] = hwm
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(STATE_PATH)
    insert_equity_snapshot(snapshot)
    return snapshot


def main():
    setup_logging("executor")
    try:
        while True:
            try:
                if mt5.account_info() is None:
                    connect()
                report_equity_snapshot()
            except Exception:
                logger.exception("Equity snapshot iteration failed")
            time.sleep(3)
    except KeyboardInterrupt:
        logger.info("Equity reporter stopped")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
