# This file can only be run and tested on Windows with the MT5 terminal open and logged in.
# Do not install or run MetaTrader5 here; implementation follows the documented API.
"""Feed broker candles into Supabase, keeping stored timestamps in UTC."""

from datetime import datetime, timedelta
import logging
import time
from zoneinfo import ZoneInfo

from executor.config import load_config
from executor.db_client import insert_candles
from executor.logging_setup import setup_logging

UTC = ZoneInfo("UTC")
logger = logging.getLogger("candles")
mt5 = None
_offsets: dict[str, tuple[float, timedelta]] = {}


def _terminal():
    global mt5
    if mt5 is None:
        import MetaTrader5
        mt5 = MetaTrader5
    return mt5


def connect() -> None:
    """Initialize the configured terminal, raising on a failed connection."""
    terminal = _terminal()
    config = load_config()
    options = dict(login=config.MT5_LOGIN, password=config.MT5_PASSWORD, server=config.MT5_SERVER)
    if config.MT5_TERMINAL_PATH:
        options["path"] = config.MT5_TERMINAL_PATH
    connected = terminal.initialize(**options)
    if not connected:
        raise RuntimeError(f"MT5 initialization failed: {terminal.last_error()}")
    _offsets.clear()
    logger.info("Connected to MT5 terminal")


def get_broker_utc_offset(pair: str) -> timedelta:
    """Infer a half-hour offset from a fresh tick; refresh once an hour."""
    now_monotonic = time.monotonic()
    cached = _offsets.get(pair)
    if cached and now_monotonic - cached[0] < 3600:
        return cached[1]
    now = datetime.now(UTC)
    offset = timedelta(hours=load_config().BROKER_UTC_OFFSET_HOURS)
    source = "configured fallback"
    try:
        tick = _terminal().symbol_info_tick(pair)
        # Forex weekend ticks are stale. Require alignment within 90s
        # after applying the nearest half-hour offset, bounded to real timezones.
        new_york = now.astimezone(ZoneInfo("America/New_York"))
        closed = (new_york.weekday() == 5 or
                  (new_york.weekday() == 4 and new_york.hour >= 17) or
                  (new_york.weekday() == 6 and new_york.hour < 17))
        if not closed and tick is not None and tick.time > 0:
            difference = float(tick.time) - now.timestamp()
            seconds = round(difference / 1800) * 1800
            if -12 * 3600 <= seconds <= 14 * 3600 and abs(difference - seconds) <= 90:
                offset = timedelta(seconds=seconds)
                source = "live tick"
    except Exception as exc:
        logger.warning("Tick offset unavailable for %s (%s)", pair, type(exc).__name__)
    # NOTE: MT5 docs specify UTC. Apply this heuristic only for the broker-time
    # feed requested here; a stale tick aligned to 30 minutes is indistinguishable
    # from a live tick with a different offset. Verify the broker on deployment.
    _offsets[pair] = (now_monotonic, offset)
    logger.info("Broker UTC offset for %s: %.1fh (%s)", pair, offset.total_seconds() / 3600, source)
    return offset


def _timeframe(timeframe: str):
    value = getattr(_terminal(), f"TIMEFRAME_{timeframe.upper()}", None)
    if value is None:
        raise ValueError(f"Unsupported MT5 timeframe: {timeframe}")
    return value


def pull_recent_candles(pair: str, timeframe: str, count: int) -> list[dict]:
    """Retrieve bars and convert broker timestamps to aware UTC datetimes."""
    if count <= 0:
        return []
    terminal = _terminal()
    if not terminal.symbol_select(pair, True):
        raise RuntimeError(f"MT5 symbol unavailable: {pair}; {terminal.last_error()}")
    offset = get_broker_utc_offset(pair)
    rates = terminal.copy_rates_from_pos(pair, _timeframe(timeframe), 0, count)
    if rates is None:
        raise RuntimeError(f"MT5 candle read failed for {pair}: {terminal.last_error()}")
    return [dict(pair=pair, timeframe=timeframe.upper(),
                 open=float(rate["open"]), high=float(rate["high"]),
                 low=float(rate["low"]), close=float(rate["close"]),
                 ts=datetime.fromtimestamp(int(rate["time"]), UTC) - offset)
            for rate in rates]


def push_candles_to_supabase(pair: str, timeframe: str, count: int) -> None:
    rows = pull_recent_candles(pair, timeframe, count)
    insert_candles(rows)
    logger.info("Submitted %d candles for %s %s", len(rows), pair, timeframe)


def run_loop(pairs: list[str], timeframe: str, interval_seconds: int = 3) -> None:
    """Continue through symbol errors and reconnect a disconnected terminal."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    while True:
        try:
            info = _terminal().terminal_info()
            if info is None or not info.connected:
                logger.warning("MT5 disconnected; reconnecting")
                _terminal().shutdown()
                connect()
            for pair in pairs:
                try:
                    # NOTE: Refresh the forming bar and the last two closed bars.
                    push_candles_to_supabase(pair, timeframe, 3)
                except Exception as exc:
                    logger.error("Candle feed failed for %s (%s)", pair, type(exc).__name__)
        except Exception as exc:
            logger.error("Candle loop failed (%s); retrying in %ss", type(exc).__name__, interval_seconds)
        time.sleep(interval_seconds)


def main() -> None:
    setup_logging("candles")
    try:
        config = load_config()
        while True:
            try:
                connect()
                run_loop(config.TRADED_PAIRS, "M1")
            except Exception as exc:
                logger.error("Candle feeder failed (%s); reconnecting in 3s", type(exc).__name__)
                time.sleep(3)
    except KeyboardInterrupt:
        logger.info("Candle feeder stopped")
    finally:
        if mt5 is not None:
            mt5.shutdown()


if __name__ == "__main__":
    main()
