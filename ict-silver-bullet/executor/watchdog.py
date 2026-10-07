"""On-instance watchdog with an external dead-man ping."""
# EC2 demo-account manual checks:
# 1. Run python -m executor.watchdog; verify executor and terminal64.exe detection.
# 2. Stop each process; verify one POST per outage, no repeat after delivery,
#    and a fresh alert after recovery followed by a second outage.
# 3. Hold a position, stop reporter >30s; verify stale/missing heartbeat alert.
# 4. Set HEALTHCHECK_PING_URL; verify one GET/minute. Stop the whole EC2 instance
#    and verify the EXTERNAL service alerts (configure its grace period there).
# 5. Break DB/MT5/webhook access; verify bounded timeouts, alert retries with
#    backoff, continued checks, and clean Ctrl+C exit.

from datetime import datetime, timezone
import logging
import time

import MetaTrader5 as mt5
import psutil
import requests

from executor.config import load_config
from executor.db_client import get_latest_equity_snapshot
from executor.logging_setup import setup_logging
from executor.mt5_bridge import connect

logger = logging.getLogger("executor")
HEARTBEAT_MAX_SECONDS = 30


def process_health():
    executor_alive = terminal_alive = False
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (process.info["name"] or "").lower()
            args = process.info["cmdline"] or []
            terminal_alive |= name == "terminal64.exe"
            executor_alive |= any(args[i:i + 2] == ["-m", "executor.order_executor"]
                                  for i in range(len(args) - 1))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return executor_alive, terminal_alive


def health_issues():
    issues = {}
    executor_alive, terminal_alive = process_health()
    if not executor_alive:
        issues["executor"] = "Order executor process is absent"
    if not terminal_alive:
        issues["terminal"] = "MT5 terminal64.exe process is absent"
    if terminal_alive:
        if mt5.account_info() is None:
            connect()
        positions = mt5.positions_get()
        if positions is None:
            issues["mt5"] = "MT5 positions cannot be read"
        elif positions:
            snapshot = get_latest_equity_snapshot()
            try:
                ts = datetime.fromisoformat(snapshot["ts"].replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    raise ValueError("naive heartbeat")
                age = (datetime.now(timezone.utc) - ts).total_seconds()
                stale = age > HEARTBEAT_MAX_SECONDS or age < -5
            except (TypeError, KeyError, ValueError):
                stale = True
            if stale:
                issues["equity"] = "Open MT5 position without a recent equity snapshot"
    return issues


def main():
    setup_logging("executor")
    delivered, retry_at = set(), {}
    next_ping = 0.0
    try:
        while True:
            try:
                config = load_config()
                break
            except Exception as exc:
                logger.error("Watchdog configuration unavailable (%s)", type(exc).__name__)
                time.sleep(5)
        while True:
            now = time.monotonic()
            if config.HEALTHCHECK_PING_URL and now >= next_ping:
                next_ping = now + 60
                try:
                    requests.get(config.HEALTHCHECK_PING_URL, timeout=10).raise_for_status()
                except requests.RequestException as exc:
                    logger.error("Dead-man ping failed (%s)", type(exc).__name__)
            try:
                issues = health_issues()
                checked = True
            except Exception as exc:
                logger.error("Health check failed (%s)", type(exc).__name__)
                issues = {"check": "Watchdog cannot query local health"}
                checked = False
            if checked:
                for recovered in delivered - issues.keys():
                    logger.info("Watchdog recovered: %s", recovered)
                delivered.intersection_update(issues)
            for key, message in issues.items():
                if key in delivered or now < retry_at.get(key, 0):
                    continue
                try:
                    # NOTE: UTF-8 text POST supports ntfy/text webhooks;
                    # JSON-only endpoints require an adapter.
                    requests.post(config.WATCHDOG_WEBHOOK_URL,
                                  data=message.encode("utf-8"),
                                  headers={"Content-Type": "text/plain; charset=utf-8"},
                                  timeout=10).raise_for_status()
                    delivered.add(key)
                    retry_at.pop(key, None)
                    logger.warning("Watchdog alert delivered: %s", key)
                except requests.RequestException as exc:
                    retry_at[key] = now + 60
                    logger.error("Alert failed (%s)", type(exc).__name__)
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Watchdog stopped")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
