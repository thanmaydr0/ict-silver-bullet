def connect() -> None:
    """Connect to the local MetaTrader 5 terminal.

    Returns:
        None.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def pull_recent_candles(pair: str, timeframe: str, count: int) -> list[dict]:
    """Retrieve recent candles from the local MT5 terminal.

    Args:
        pair: Trading symbol.
        timeframe: Candle interval.
        count: Number of candles to retrieve.

    Returns:
        Candle records with timezone-aware UTC timestamps.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def push_candles_to_supabase(pair: str, timeframe: str, count: int) -> None:
    """Feed recent terminal candles to the shared database.

    Args:
        pair: Trading symbol.
        timeframe: Candle interval.
        count: Number of candles to feed.

    Returns:
        None.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def run_loop(pairs: list[str], timeframe: str, interval_seconds: int = 3) -> None:
    """Run the continuous candle feeder.

    Args:
        pairs: Trading symbols.
        timeframe: Candle interval.
        interval_seconds: Seconds between iterations; defaults to 3.

    Returns:
        None; runs until interrupted.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def main() -> None:
    """Start the MT5 candle feeder process.

    Returns:
        None; runs until interrupted.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError
