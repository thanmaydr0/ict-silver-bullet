from datetime import datetime


def insert_candles(rows: list[dict]) -> None:
    """Persist candle rows to the shared database.

    Args:
        rows: Candle records with timezone-aware UTC timestamps.

    Returns:
        None.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def get_latest_candles(pair: str, timeframe: str, limit: int) -> list[dict]:
    """Read the latest candles for a pair and timeframe.

    Args:
        pair: Trading symbol.
        timeframe: Candle interval.
        limit: Maximum number of rows.

    Returns:
        A list of matching candle records.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def insert_signal(signal: dict) -> int | None:
    """Persist a proposed trade signal.

    Args:
        signal: Proposed trade fields.

    Returns:
        The inserted signal ID, or None if no record is returned.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def get_pending_signals() -> list[dict]:
    """Read trade proposals awaiting execution.

    Returns:
        Pending signal records.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def get_latest_signal() -> dict | None:
    """Read the most recent trade proposal.

    Returns:
        The latest signal record, or None if absent.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def update_signal_status(signal_id: int, status: str) -> None:
    """Update the lifecycle status of a signal.

    Args:
        signal_id: Signal primary key.
        status: New lifecycle status.

    Returns:
        None.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def insert_equity_snapshot(snapshot: dict) -> None:
    """Persist an account equity snapshot.

    Args:
        snapshot: Equity fields with a timezone-aware UTC timestamp.

    Returns:
        None.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def get_latest_equity_snapshot() -> dict | None:
    """Read the most recent equity snapshot.

    Returns:
        The latest equity record, or None if absent.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def get_equity_snapshots(since: datetime) -> list[dict]:
    """Read equity snapshots from a time boundary.

    Args:
        since: Timezone-aware UTC lower time boundary.

    Returns:
        Matching equity snapshot records.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def get_max_equity() -> float | None:
    """Read the historical maximum account equity.

    Returns:
        Maximum equity, or None if no snapshots exist.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def get_trades(limit: int) -> list[dict]:
    """Read recent trades joined with their signals.

    Args:
        limit: Maximum number of trades.

    Returns:
        Trade records including associated signal fields.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def get_trades_since(since: datetime) -> list[dict]:
    """Read trades from a time boundary.

    Args:
        since: Timezone-aware UTC lower time boundary.

    Returns:
        Matching trade records.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def get_open_trades() -> list[dict]:
    """Read trades that have not been closed.

    Returns:
        Open trade records.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def insert_news_events(rows: list[dict]) -> None:
    """Persist economic news events.

    Args:
        rows: News records with timezone-aware UTC timestamps.

    Returns:
        None.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def get_upcoming_news(within_minutes: int) -> list[dict]:
    """Read news events within a forward-looking window.

    Args:
        within_minutes: Number of minutes ahead of the current time.

    Returns:
        Upcoming news records.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError
