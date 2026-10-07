"""Executor settings loaded and validated on demand from executor/.env."""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values


_ENV_PATH = Path(__file__).resolve().parent / ".env"


def _value(values: dict, name: str, default: str = "") -> str:
    value = os.environ.get(name, values.get(name))
    return value.strip() if value is not None and value.strip() else default


def _required(values: dict, name: str) -> str:
    value = _value(values, name)
    if not value:
        raise RuntimeError(f"{__package__}: missing required environment variable {name}; set it in {_ENV_PATH} or the process environment.")
    return value


def _pairs(values: dict) -> list[str]:
    pairs = [pair.strip() for pair in _value(values, "TRADED_PAIRS", "EURUSD,GBPUSD").split(",") if pair.strip()]
    if not pairs:
        raise RuntimeError("TRADED_PAIRS must contain at least one pair.")
    return pairs


@dataclass(frozen=True)
class ExecutorConfig:
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str = field(repr=False)
    MT5_LOGIN: int
    MT5_PASSWORD: str = field(repr=False)
    MT5_SERVER: str
    WATCHDOG_WEBHOOK_URL: str
    MT5_TERMINAL_PATH: str | None
    HEALTHCHECK_PING_URL: str | None
    TRADED_PAIRS: list[str]
    BROKER_UTC_OFFSET_HOURS: int


@lru_cache(maxsize=1)
def load_config() -> ExecutorConfig:
    """Validate and cache executor settings; no MT5 connection is made."""
    values = dotenv_values(_ENV_PATH, interpolate=False)
    required = {name: _required(values, name) for name in (
        "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "MT5_LOGIN", "MT5_PASSWORD",
        "MT5_SERVER", "WATCHDOG_WEBHOOK_URL",
    )}
    try:
        login = int(required["MT5_LOGIN"])
        if login <= 0:
            raise ValueError
    except ValueError:
        raise RuntimeError("MT5_LOGIN must be a positive integer.") from None
    try:
        offset = int(_value(values, "BROKER_UTC_OFFSET_HOURS", "0"))
    except ValueError:
        raise RuntimeError("BROKER_UTC_OFFSET_HOURS must be an integer.") from None
    return ExecutorConfig(
        SUPABASE_URL=required["SUPABASE_URL"],
        SUPABASE_SERVICE_KEY=required["SUPABASE_SERVICE_KEY"],
        MT5_LOGIN=login,
        MT5_PASSWORD=required["MT5_PASSWORD"],
        MT5_SERVER=required["MT5_SERVER"],
        WATCHDOG_WEBHOOK_URL=required["WATCHDOG_WEBHOOK_URL"],
        MT5_TERMINAL_PATH=_value(values, "MT5_TERMINAL_PATH") or None,
        HEALTHCHECK_PING_URL=_value(values, "HEALTHCHECK_PING_URL") or None,
        TRADED_PAIRS=_pairs(values),
        BROKER_UTC_OFFSET_HOURS=offset,
    )
