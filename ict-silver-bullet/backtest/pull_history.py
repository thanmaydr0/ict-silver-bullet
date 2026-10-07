"""Monthly MT5 history export; run on the EC2 Windows box with MT5 open.

Usage: python -m backtest.pull_history --pairs EURUSD,GBPUSD --timeframe M1 --years 3
MetaTrader5 and pandas are imported only when the CLI runs.
"""

import argparse
from datetime import datetime, timedelta
import logging
from pathlib import Path
import re
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")
logger = logging.getLogger("history")
DATA_DIR = Path(__file__).resolve().parent / "data"


def _month_end(start: datetime) -> datetime:
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return start.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def _years_ago(now: datetime, years: int) -> datetime:
    try:
        return now.replace(year=now.year - years)
    except ValueError:
        return now.replace(year=now.year - years, day=28)


def _pull_pair(terminal, pair: str, timeframe: str, start: datetime,
               end: datetime, offset: timedelta) -> list[dict]:
    if not terminal.symbol_select(pair, True):
        raise RuntimeError(f"MT5 symbol unavailable: {pair}; {terminal.last_error()}")
    interval = getattr(terminal, f"TIMEFRAME_{timeframe}", None)
    if interval is None:
        raise ValueError(f"Unsupported MT5 timeframe: {timeframe}")
    rows = {}
    cursor = start
    # NOTE: Historical bars use the same sampled/configured offset as the feeder.
    # Historical DST offsets cannot be reconstructed from the current tick alone;
    # brokers with historical offset changes need their historical timezone rule.
    while cursor < end:
        chunk_end = min(_month_end(cursor), end)
        rates = terminal.copy_rates_range(pair, interval, cursor + offset, chunk_end + offset)
        if rates is None:
            raise RuntimeError(f"MT5 history failed for {pair} at {cursor.isoformat()}: {terminal.last_error()}")
        added = 0
        for rate in rates:
            ts = datetime.fromtimestamp(int(rate["time"]), UTC) - offset
            # MT5 includes both boundaries; retain a half-open chunk and deduplicate.
            if cursor <= ts < chunk_end:
                rows[ts] = dict(pair=pair, timeframe=timeframe, ts=ts,
                                open=float(rate["open"]), high=float(rate["high"]),
                                low=float(rate["low"]), close=float(rate["close"]))
                added += 1
        logger.info("%s %s: %s to %s, %d bars", pair, timeframe,
                    cursor.isoformat(), chunk_end.isoformat(), added)
        if len(rates) == 0:
            logger.warning("No history for %s in this chunk; check broker history and MT5 Max bars setting", pair)
        cursor = chunk_end
    return [rows[ts] for ts in sorted(rows)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", default="EURUSD,GBPUSD")
    parser.add_argument("--timeframe", default="M1")
    parser.add_argument("--years", type=int, default=3)
    args = parser.parse_args()
    pairs = list(dict.fromkeys(pair.strip() for pair in args.pairs.split(",") if pair.strip()))
    if not pairs or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", pair) or pair in {".", ".."} for pair in pairs):
        parser.error("--pairs must contain comma-separated trading symbols")
    if not 1 <= args.years <= 100:
        parser.error("--years must be between 1 and 100")
    timeframe = args.timeframe.upper()

    import MetaTrader5 as mt5
    import pandas as pd
    from executor import mt5_bridge
    from executor.logging_setup import setup_logging

    setup_logging("history")
    now = datetime.now(UTC)
    start = _years_ago(now, args.years)
    try:
        mt5_bridge.connect()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for pair in pairs:
            offset = mt5_bridge.get_broker_utc_offset(pair)
            rows = _pull_pair(mt5, pair, timeframe, start, now, offset)
            if not rows:
                raise RuntimeError(f"No history returned for {pair}; export aborted")
            frame = pd.DataFrame(rows)
            frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
            target = DATA_DIR / f"{pair}_{timeframe}.parquet"
            temporary = target.with_suffix(".parquet.tmp")
            try:
                frame.to_parquet(temporary, index=False)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            logger.info("Saved %d unique bars to %s", len(frame), target)
    except KeyboardInterrupt:
        logger.info("History export stopped")
    except Exception:
        logger.exception("History export failed")
        raise
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
