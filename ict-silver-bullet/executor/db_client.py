def get_pending_signals() -> list[dict]:
    """Read trade proposals awaiting execution.

    Returns:
        Pending signal records.

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


def insert_trade(trade: dict) -> int | None:
    """Persist an executed trade.

    Args:
        trade: Execution record and associated signal fields.

    Returns:
        The inserted trade ID, or None if no record is returned.

    Raises:
        NotImplementedError: This interface is reserved for a later phase.
    """
    raise NotImplementedError


def update_trade(trade_id: int, fields: dict) -> None:
    """Update selected fields of a trade record.

    Args:
        trade_id: Trade primary key.
        fields: Fields to update.

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
