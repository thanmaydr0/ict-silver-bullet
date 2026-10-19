"""Export maximum available raw MT5 M1 bars, partitioned by symbol and year.

Run on the Windows host with the MetaTrader 5 terminal open and logged in:
    python -m backtest.pull_history_m1

The MT5 ``time`` field is written unchanged. The offset probe is metadata only.
"""

from __future__ import annotations

import argparse
from io import BytesIO
from datetime import date, datetime, time, timezone
import hashlib
import importlib
import json
import logging
import math
import os
from pathlib import Path
import re
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(__file__).resolve().parent / "data"
M1_DIR = DATA_DIR / "m1"
SYMBOL_INFO_DIR = DATA_DIR / "symbol_info"
MANIFEST_PATH = DATA_DIR / "manifest.json"
ENV_PATH = ROOT / "executor" / ".env"

RATE_COLUMNS = (
    "time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"
)
SYMBOL_FIELDS = (
    "digits", "point", "trade_tick_value", "trade_contract_size", "volume_min",
    "volume_step", "swap_long", "swap_short", "swap_mode", "margin_initial",
    "currency_profit",
)
WEEKDAYS_SUNDAY_FIRST = (
    "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"
)
SYMBOL_PATTERN = re.compile(r"[A-Za-z0-9_.#@+-]{1,64}\Z")
logger = logging.getLogger("pull-history-m1")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_settings(symbol_override: str | None = None) -> dict[str, Any]:
    """Load only the MT5 settings and symbol list from executor/.env."""
    from dotenv import dotenv_values

    if not ENV_PATH.is_file():
        raise RuntimeError(f"Missing {ENV_PATH}; configure MT5 credentials there.")
    file_values = dotenv_values(ENV_PATH, interpolate=False)

    def value(name: str, default: str | None = None) -> str | None:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            raw = file_values.get(name)
        if raw is None or not str(raw).strip():
            return default
        return str(raw).strip()

    login_text = value("MT5_LOGIN")
    password = value("MT5_PASSWORD")
    server = value("MT5_SERVER")
    if not login_text or not password or not server:
        raise RuntimeError("MT5_LOGIN, MT5_PASSWORD, and MT5_SERVER must be set in executor/.env.")
    try:
        login = int(login_text)
    except ValueError:
        raise RuntimeError("MT5_LOGIN must be a positive integer.") from None
    if login <= 0:
        raise RuntimeError("MT5_LOGIN must be a positive integer.")

    symbols_text = symbol_override if symbol_override is not None else value("TRADED_PAIRS", "EURUSD,GBPUSD")
    symbols = _parse_symbols(symbols_text or "")
    try:
        configured_offset_hours = float(value("BROKER_UTC_OFFSET_HOURS", "0"))
    except ValueError:
        raise RuntimeError("BROKER_UTC_OFFSET_HOURS must be numeric.") from None
    if not math.isfinite(configured_offset_hours) or not -12 <= configured_offset_hours <= 14:
        raise RuntimeError("BROKER_UTC_OFFSET_HOURS must be between -12 and +14.")

    return {
        "login": login,
        "password": password,
        "server": server,
        "terminal_path": value("MT5_TERMINAL_PATH"),
        "symbols": symbols,
        "configured_offset_hours": configured_offset_hours,
    }


def _parse_symbols(value: str) -> list[str]:
    symbols = list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if not symbols:
        raise ValueError("TRADED_PAIRS must contain at least one symbol.")
    invalid = [symbol for symbol in symbols if not SYMBOL_PATTERN.fullmatch(symbol) or symbol in {".", ".."}]
    if invalid:
        raise ValueError(f"Invalid MT5 symbol name(s): {', '.join(invalid)}")
    return symbols


def _safe_filename(symbol: str) -> str:
    # Percent encoding makes broker suffixes safe in paths without losing the name.
    from urllib.parse import quote

    return quote(symbol, safe="._-")


def _connect(mt5: Any, settings: dict[str, Any]) -> None:
    options: dict[str, Any] = {
        "login": settings["login"],
        "password": settings["password"],
        "server": settings["server"],
    }
    if settings["terminal_path"]:
        options["path"] = settings["terminal_path"]
    if not mt5.initialize(**options):
        raise RuntimeError(f"MT5 initialization failed: {mt5.last_error()}")


def _require_demo_server(settings: dict[str, Any]) -> None:
    server = str(settings.get("server", "")).casefold()
    if not any(marker in server for marker in ("demo", "paper", "trial")):
        raise RuntimeError("Refusing to initialize MT5: MT5_SERVER must identify a demo server.")


def _require_demo_account(mt5: Any) -> str:
    account = mt5.account_info()
    if account is None:
        raise RuntimeError("Cannot verify MT5 account type; refusing to export without demo confirmation.")
    fields = account._asdict() if hasattr(account, "_asdict") else {}
    trade_mode = fields.get("trade_mode", getattr(account, "trade_mode", None))
    demo_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
    if trade_mode != demo_mode:
        raise RuntimeError("Refusing to export: connected MT5 account is not confirmed as demo.")
    return "demo"


def _json_safe(value: Any) -> Any:
    if hasattr(value, "_asdict"):
        return {str(key): _json_safe(item) for key, item in value._asdict().items()}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "item") and callable(value.item):
        return _json_safe(value.item())
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _observe_server_offset(mt5: Any, symbol: str) -> dict[str, Any]:
    """Estimate the current raw-time offset from a fresh tick; never alter bars."""
    observed_at = datetime.now(timezone.utc)
    tick = mt5.symbol_info_tick(symbol)
    observation: dict[str, Any] = {
        "observed_at_utc": observed_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "method": "raw tick time compared with UTC clock; nearest 30-minute offset",
        "raw_tick_time": None,
        "raw_tick_time_msc": None,
        "estimated_offset_seconds": None,
        "estimated_offset_hours": None,
        "freshness_seconds_after_offset": None,
        "status": "no_tick",
        "applied_to_saved_timestamps": False,
    }
    if tick is None:
        return observation

    raw_seconds = int(getattr(tick, "time", 0) or 0)
    raw_milliseconds = int(getattr(tick, "time_msc", 0) or 0)
    raw_epoch = raw_milliseconds / 1000 if raw_milliseconds > 0 else float(raw_seconds)
    observation["raw_tick_time"] = raw_seconds or None
    observation["raw_tick_time_msc"] = raw_milliseconds or None
    if raw_epoch <= 0:
        observation["status"] = "tick_has_no_timestamp"
        return observation

    now_epoch = observed_at.timestamp()
    difference = raw_epoch - now_epoch
    candidate = round(difference / 1800) * 1800
    freshness = now_epoch - (raw_epoch - candidate)
    observation["freshness_seconds_after_offset"] = round(freshness, 3)
    if -12 * 3600 <= candidate <= 14 * 3600 and abs(difference - candidate) <= 90 and -5 <= freshness <= 120:
        observation["estimated_offset_seconds"] = int(candidate)
        observation["estimated_offset_hours"] = candidate / 3600
        observation["status"] = "observed_from_fresh_tick"
    else:
        observation["status"] = "no_fresh_offset_observation"
    return observation


def _trade_sessions(mt5: Any, symbol: str, max_sessions_per_day: int = 32) -> dict[str, Any]:
    function = getattr(mt5, "symbol_info_session_trade", None)
    if not callable(function):
        return {
            "api_available": False,
            "status": "unsupported_by_python_api",
            "source": "mt5.symbol_info_session_trade",
            "windows": [],
        }

    windows: list[dict[str, Any]] = []
    errors: list[str] = []
    for day_index, day_name in enumerate(WEEKDAYS_SUNDAY_FIRST):
        for session_index in range(max_sessions_per_day):
            try:
                session = function(symbol, day_index, session_index)
            except Exception as exc:  # broker/package-specific session metadata
                errors.append(f"{day_name} session {session_index}: {type(exc).__name__}: {exc}")
                break
            if session is None:
                break
            if hasattr(session, "_asdict"):
                details = _json_safe(session._asdict())
            elif isinstance(session, (tuple, list)):
                details = {"from": _json_safe(session[0]) if len(session) > 0 else None,
                           "to": _json_safe(session[1]) if len(session) > 1 else None}
            else:
                details = _json_safe(session)
            windows.append({
                "day_index_sunday_zero": day_index,
                "day_name": day_name,
                "session_index": session_index,
                "value": details,
            })
    return {
        "api_available": True,
        "status": "read_with_errors" if errors else ("windows_returned" if windows else "no_windows_returned"),
        "source": "mt5.symbol_info_session_trade",
        "windows": windows,
        "errors": errors,
    }


def _symbol_metadata(mt5: Any, symbol: str) -> dict[str, Any]:
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"MT5 symbol_info returned no data for {symbol}: {mt5.last_error()}")
    values = info._asdict() if hasattr(info, "_asdict") else {}
    selected = {field: _json_safe(values.get(field)) for field in SYMBOL_FIELDS}
    missing = [field for field in SYMBOL_FIELDS if field not in values]
    return {
        "symbol": symbol,
        "captured_at_utc": _utc_now_iso(),
        "fields": selected,
        "missing_symbol_info_fields": missing,
        "symbol_info_session_fields": {
            key: _json_safe(value) for key, value in values.items() if key.startswith("session_")
        },
        "trade_sessions": _trade_sessions(mt5, symbol),
    }


def _pull_available_rates(mt5: Any, symbol: str, chunk_bars: int):
    import pandas as pd

    chunks = []
    start_pos = 0
    previous_oldest: int | None = None
    chunk_number = 0
    while True:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, start_pos, chunk_bars)
        if rates is None:
            raise RuntimeError(f"MT5 M1 read failed for {symbol} at position {start_pos}: {mt5.last_error()}")
        count = len(rates)
        if count == 0:
            break
        names = getattr(getattr(rates, "dtype", None), "names", None)
        if names is not None:
            missing = [name for name in RATE_COLUMNS if name not in names]
            if missing:
                raise RuntimeError(f"MT5 rates for {symbol} are missing columns: {', '.join(missing)}")
        frame = pd.DataFrame.from_records(rates)
        missing = [name for name in RATE_COLUMNS if name not in frame.columns]
        if missing:
            raise RuntimeError(f"MT5 rates for {symbol} are missing columns: {', '.join(missing)}")
        frame = frame.loc[:, RATE_COLUMNS]
        frame["time"] = pd.to_numeric(frame["time"], errors="raise").astype("int64")
        oldest = int(frame["time"].min())
        newest = int(frame["time"].max())
        if previous_oldest is not None and oldest >= previous_oldest:
            raise RuntimeError(
                f"MT5 did not move backward for {symbol} at position {start_pos}; stopping to avoid a loop."
            )
        previous_oldest = oldest
        chunks.append(frame)
        chunk_number += 1
        logger.info("%s: chunk %d returned %d bars, raw time %d..%d", symbol, chunk_number, count, oldest, newest)
        if count < chunk_bars:
            break
        start_pos += count
    if not chunks:
        return pd.DataFrame(columns=RATE_COLUMNS)
    return pd.concat(chunks, ignore_index=True)


def _gap_stats(raw_times: list[int]) -> dict[str, int]:
    ordered = sorted(int(value) for value in raw_times)
    gaps = [later - earlier for earlier, later in zip(ordered, ordered[1:]) if later - earlier > 60]
    return {
        "gap_count": len(gaps),
        "missing_m1_bars_between_observed_rows": sum(max(0, gap // 60 - 1) for gap in gaps),
        "largest_gap_seconds": max(gaps, default=0),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_symbol_bars(symbol: str, frame, data_dir: Path = DATA_DIR) -> dict[str, Any]:
    import pandas as pd

    if frame.empty:
        raise RuntimeError(f"No M1 history returned for {symbol}; no yearly files were written.")
    before_deduplication = len(frame)
    frame = frame.sort_values("time", kind="stable").drop_duplicates(subset=["time"], keep="first").reset_index(drop=True)
    duplicate_rows_removed = before_deduplication - len(frame)
    all_times = [int(value) for value in frame["time"].tolist()]
    symbol_gaps = _gap_stats(all_times)
    year_values = pd.to_datetime(frame["time"], unit="s", utc=True).dt.year
    annual_files = []

    for year, annual in frame.groupby(year_values, sort=True):
        annual = annual.loc[:, RATE_COLUMNS].sort_values("time", kind="stable").reset_index(drop=True)
        year = int(year)
        path = M1_DIR / f"{_safe_filename(symbol)}_{year}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            annual.to_parquet(temporary, index=False, engine="pyarrow")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        times = [int(value) for value in annual["time"].tolist()]
        annual_files.append({
            "file": path.relative_to(data_dir).as_posix(),
            "year": year,
            "first_timestamp": min(times),
            "last_timestamp": max(times),
            "row_count": len(annual),
            **_gap_stats(times),
            "sha256": _sha256(path),
        })
        logger.info("Saved %d bars to %s", len(annual), path)

    return {
        "symbol": symbol,
        "first_timestamp": min(all_times),
        "last_timestamp": max(all_times),
        "row_count": len(frame),
        "duplicate_rows_removed": duplicate_rows_removed,
        **symbol_gaps,
        "files": annual_files,
    }


def _history_clock_metadata(
    offset_observations: list[dict[str, Any]], configured_offset_hours: float
) -> dict[str, Any]:
    return {
        "raw_time_field": "MT5 rate.time",
        "storage": "raw integer seconds preserved exactly; no offset or timezone conversion applied",
        "project_expected_server_rule": "UTC+2/UTC+3 (EET/EEST) with DST aligned to New York close",
        "configured_fallback_offset_hours": configured_offset_hours,
        "offset_observation_method": (
            "A current tick timestamp is compared with the UTC clock, rounded to a 30-minute offset, "
            "and accepted only when the tick is fresh. Observations are diagnostic and are not applied to bars."
        ),
        "offset_observations": offset_observations,
    }


def _config_hash(symbols: list[str], chunk_bars: int, configured_offset_hours: float) -> str:
    payload = {
        "symbols": symbols,
        "timeframe": "M1",
        "chunk_bars": chunk_bars,
        "configured_fallback_offset_hours": configured_offset_hours,
        "raw_time_preserved": True,
        "partition_year_basis": "raw epoch interpreted as UTC calendar year",
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", help="optional comma-separated override; default is TRADED_PAIRS in executor/.env")
    parser.add_argument("--chunk-bars", type=int, default=50_000, help="bars requested per MT5 position chunk (default: 50000)")
    args = parser.parse_args(argv)
    if not 1_000 <= args.chunk_bars <= 250_000:
        parser.error("--chunk-bars must be between 1000 and 250000")

    from executor.logging_setup import setup_logging

    global logger
    logger = setup_logging("pull-history-m1")
    settings = _load_settings(args.symbols)
    _require_demo_server(settings)
    try:
        import pandas as pd

        pd.DataFrame().to_parquet(BytesIO(), engine="pyarrow")
    except Exception as exc:
        parser.error(
            "Pandas/PyArrow Parquet support is unavailable in this environment; "
            "install backtest/requirements.txt in the active environment "
            f"({type(exc).__name__}: {exc})."
        )

    mt5 = importlib.import_module("MetaTrader5")
    initialized = False
    failures: dict[str, dict[str, str]] = {}
    completed: list[dict[str, Any]] = []
    symbol_clock_observations: dict[str, list[dict[str, Any]]] = {}
    symbol_info_files: dict[str, dict[str, str]] = {}
    run_id = f"m1-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    try:
        _connect(mt5, settings)
        initialized = True
        account_type = _require_demo_account(mt5)
        for symbol in settings["symbols"]:
            observations: list[dict[str, Any]] = []
            symbol_clock_observations[symbol] = observations
            try:
                if not mt5.symbol_select(symbol, True):
                    raise RuntimeError(f"MT5 symbol_select failed: {mt5.last_error()}")
                observations.append(_observe_server_offset(mt5, symbol))
                metadata = _symbol_metadata(mt5, symbol)
                metadata["clock_observations"] = observations
                metadata_path = SYMBOL_INFO_DIR / f"{_safe_filename(symbol)}.json"
                _atomic_json(metadata_path, metadata)
                symbol_info_files[symbol] = {
                    "file": metadata_path.relative_to(DATA_DIR).as_posix(),
                    "sha256": _sha256(metadata_path),
                }

                frame = _pull_available_rates(mt5, symbol, args.chunk_bars)
                if frame.empty:
                    raise RuntimeError(f"No M1 history returned for {symbol}; check broker history and Max bars in chart.")
                summary = _write_symbol_bars(symbol, frame)
                observations.append(_observe_server_offset(mt5, symbol))
                # Refresh metadata so the final offset sample is included and hashed.
                metadata["clock_observations"] = observations
                _atomic_json(metadata_path, metadata)
                symbol_info_files[symbol]["sha256"] = _sha256(metadata_path)
                summary["symbol_info_file"] = symbol_info_files[symbol]["file"]
                summary["symbol_info_sha256"] = symbol_info_files[symbol]["sha256"]
                completed.append(summary)
            except Exception as exc:
                logger.exception("M1 history export failed for %s", symbol)
                failures[symbol] = {"error_type": type(exc).__name__, "error": str(exc)}

        clock_metadata = _history_clock_metadata([
            {"symbol": symbol, **observation}
            for symbol, observations in symbol_clock_observations.items()
            for observation in observations
        ], settings["configured_offset_hours"])
        config_digest = _config_hash(
            settings["symbols"], args.chunk_bars, settings["configured_offset_hours"]
        )
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "generated_at_utc": _utc_now_iso(),
            "status": "complete" if not failures else ("partial" if completed else "failed"),
            "config_sha256": config_digest,
            "timeframe": "M1",
            "account_type": account_type,
            "chunk_bars": args.chunk_bars,
            "timestamp_unit": "Unix seconds, as returned in MT5 rate.time",
            "partition_year_basis": "raw epoch interpreted as UTC calendar year; stored time values are unchanged",
            "gap_count_definition": "number of adjacent observed rows separated by more than 60 seconds; includes session/weekend closures",
            "clock": clock_metadata,
            "symbols": completed,
            "failed_symbols": failures,
        }
        _atomic_json(MANIFEST_PATH, manifest)
        logger.info("Wrote %s with status %s", MANIFEST_PATH, manifest["status"])
    finally:
        if initialized:
            mt5.shutdown()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
