"""Brain settings loaded and validated on demand from brain/.env."""

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
class BrainConfig:
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str = field(repr=False)
    LLM_PROVIDER: str
    LLM_API_KEY: str = field(repr=False)
    LLM_MODEL: str
    FIRECRAWL_API_KEY: str = field(repr=False)
    FRED_API_KEY: str | None = field(repr=False)
    FINNHUB_API_KEY: str | None = field(repr=False)
    MARKETAUX_API_KEY: str | None = field(repr=False)
    ALPHAVANTAGE_API_KEY: str | None = field(repr=False)
    NEWSDATA_API_KEY: str | None = field(repr=False)
    TRADED_PAIRS: list[str]
    SENTIMENT_MODEL: str


@dataclass(frozen=True)
class DashboardConfig:
    DASHBOARD_HOST: str
    DASHBOARD_PORT: int
    DASHBOARD_USERNAME: str
    DASHBOARD_PASSWORD: str = field(repr=False)


@lru_cache(maxsize=1)
def load_config() -> BrainConfig:
    """Validate and cache brain settings without requiring optional news keys."""
    values = dotenv_values(_ENV_PATH, interpolate=False)
    return BrainConfig(
        SUPABASE_URL=_required(values, "SUPABASE_URL"),
        SUPABASE_SERVICE_KEY=_required(values, "SUPABASE_SERVICE_KEY"),
        LLM_PROVIDER=_required(values, "LLM_PROVIDER"),
        LLM_API_KEY=_required(values, "LLM_API_KEY"),
        LLM_MODEL=_required(values, "LLM_MODEL"),
        FIRECRAWL_API_KEY=_required(values, "FIRECRAWL_API_KEY"),
        FRED_API_KEY=_value(values, "FRED_API_KEY") or None,
        FINNHUB_API_KEY=_value(values, "FINNHUB_API_KEY") or None,
        MARKETAUX_API_KEY=_value(values, "MARKETAUX_API_KEY") or None,
        ALPHAVANTAGE_API_KEY=_value(values, "ALPHAVANTAGE_API_KEY") or None,
        NEWSDATA_API_KEY=_value(values, "NEWSDATA_API_KEY") or None,
        TRADED_PAIRS=_pairs(values),
        SENTIMENT_MODEL=_value(values, "SENTIMENT_MODEL", "ProsusAI/finbert"),
    )


@lru_cache(maxsize=1)
def load_dashboard_config() -> DashboardConfig:
    """Validate and cache dashboard settings independently of pipeline settings."""
    values = dotenv_values(_ENV_PATH, interpolate=False)
    username = _required(values, "DASHBOARD_USERNAME")
    password = _required(values, "DASHBOARD_PASSWORD")
    try:
        port = int(_value(values, "DASHBOARD_PORT", "7860"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise RuntimeError("DASHBOARD_PORT must be an integer between 1 and 65535.") from None
    return DashboardConfig(_value(values, "DASHBOARD_HOST", "0.0.0.0"), port, username, password)
