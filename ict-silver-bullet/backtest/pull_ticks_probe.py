"""Probe tick-history depth at 12-month intervals without exporting ticks.

Run on the Windows host with MT5 open:
    python -m backtest.pull_ticks_probe

Each annual request is limited to one hour by default. The script caps the
worst-case total request window to 72 hours across both symbols. Returned ticks
are counted and discarded; only the probe summary is saved.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(__file__).resolve().parent / "data"
OUTPUT_PATH = DATA_DIR / "ticks_probe.json"
ENV_PATH = ROOT / "executor" / ".env"
SYMBOLS = ("EURUSD", "USDJPY")
logger = logging.getLogger("pull-ticks-probe")


def _load_settings() -> dict[str, Any]:
    from dotenv import dotenv_values

    if not ENV_PATH.is_file():
        raise RuntimeError(f"Missing {ENV_PATH}; configure MT5 credentials there.")
    file_values = dotenv_values(ENV_PATH, interpolate=False)

    def value(name: str) -> str | None:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            raw = file_values.get(name)
        return str(raw).strip() if raw is not None and str(raw).strip() else None

    login_text, password, server = value("MT5_LOGIN"), value("MT5_PASSWORD"), value("MT5_SERVER")
    if not login_text or not password or not server:
        raise RuntimeError("MT5_LOGIN, MT5_PASSWORD, and MT5_SERVER must be set in executor/.env.")
    try:
        login = int(login_text)
    except ValueError:
        raise RuntimeError("MT5_LOGIN must be a positive integer.") from None
    if login <= 0:
        raise RuntimeError("MT5_LOGIN must be a positive integer.")
    return {"login": login, "password": password, "server": server, "terminal_path": value("MT5_TERMINAL_PATH")}


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


def _last_completed_wednesday(now: datetime) -> date:
    days_back = (now.weekday() - 2) % 7
    if days_back == 0 and now.time() < time(12, 0):
        days_back = 7
    return (now - timedelta(days=days_back)).date()


def _sample_day(anchor: date, lookback_years: int) -> date:
    target_year = anchor.year - lookback_years
    try:
        target = anchor.replace(year=target_year)
    except ValueError:  # February 29 in a non-leap year
        target = anchor.replace(year=target_year, day=28)
    candidates = [target + timedelta(days=offset) for offset in range(-3, 4)]
    return min(candidates, key=lambda day: (abs((day - target).days), day))


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _field(row: Any, name: str) -> Any:
    if hasattr(row, "dtype") and getattr(row.dtype, "names", None) and name in row.dtype.names:
        return row[name]
    if hasattr(row, name):
        return getattr(row, name)
    try:
        return row[name]
    except (KeyError, TypeError, IndexError):
        return None


def _probe_symbol(mt5: Any, symbol: str, anchor: date, max_years: int, probe_hours: int) -> dict[str, Any]:
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"MT5 symbol_select failed for {symbol}: {mt5.last_error()}")
    flag = getattr(mt5, "COPY_TICKS_ALL", None)
    if flag is None:
        raise RuntimeError("MetaTrader5 package has no COPY_TICKS_ALL flag.")

    samples = []
    successful_ages = []
    empty_streak = 0
    total_requested_hours = 0
    for age in range(max_years + 1):
        sample_day = _sample_day(anchor, age)
        start = datetime.combine(sample_day, time.min, tzinfo=timezone.utc)
        end = start + timedelta(hours=probe_hours)
        total_requested_hours += probe_hours
        rates = mt5.copy_ticks_range(symbol, start, end, flag)
        if rates is None:
            error = mt5.last_error()
            samples.append({
                "lookback_years": age,
                "start_utc": _utc_iso(start),
                "end_utc": _utc_iso(end),
                "tick_count": None,
                "error": _safe_error(error),
            })
            logger.warning("%s %dy probe failed: %s", symbol, age, error)
            # A failed request is not evidence that history ends here.
        else:
            count = len(rates)
            sample: dict[str, Any] = {
                "lookback_years": age,
                "start_utc": _utc_iso(start),
                "end_utc": _utc_iso(end),
                "tick_count": count,
                "raw_tick_timestamps_saved": False,
            }
            if count:
                successful_ages.append(age)
                empty_streak = 0
                names = getattr(getattr(rates, "dtype", None), "names", None) or ()
                times = [int(value) for value in rates["time"]] if "time" in names else []
                if times:
                    sample["first_tick_time_raw"] = min(times)
                    sample["last_tick_time_raw"] = max(times)
                if "time_msc" in names:
                    sample["first_tick_time_msc_raw"] = int(min(rates["time_msc"]))
                    sample["last_tick_time_msc_raw"] = int(max(rates["time_msc"]))
            else:
                empty_streak += 1
                sample["empty_result_last_error"] = _safe_error(mt5.last_error())
            samples.append(sample)
            logger.info("%s %dy probe returned %d ticks", symbol, age, count)

        if successful_ages and empty_streak >= 2:
            break

    deepest = max(successful_ages) if successful_ages else None
    return {
        "symbol": symbol,
        "status": "ticks_found" if deepest is not None else "no_ticks_in_sampled_windows",
        "probe_interval_years": 1,
        "probe_window_hours": probe_hours,
        "deepest_successful_sample_lookback_years": deepest,
        "history_extent_note": (
            "Sampled lower bound only: the oldest successful annual sample is not necessarily the oldest tick; "
            "coverage between annual samples was not downloaded or verified."
        ),
        "total_requested_sample_hours": total_requested_hours,
        "samples": samples,
    }


def _safe_error(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-years", type=int, default=30, help="annual sample depth (default: 30 years)")
    parser.add_argument("--probe-hours", type=int, default=1, help="hours retrieved per annual sample (default: 1)")
    args = parser.parse_args(argv)
    if not 1 <= args.max_years <= 35:
        parser.error("--max-years must be between 1 and 35")
    if not 1 <= args.probe_hours <= 24:
        parser.error("--probe-hours must be between 1 and 24")
    total_hours_ceiling = (args.max_years + 1) * len(SYMBOLS) * args.probe_hours
    if total_hours_ceiling > 72:
        parser.error("annual probes may request at most 72 hours total across both symbols")

    from executor.logging_setup import setup_logging

    global logger
    logger = setup_logging("pull-ticks-probe")
    settings = _load_settings()
    mt5 = importlib.import_module("MetaTrader5")
    initialized = False
    results = []
    failures = {}
    run_started = datetime.now(timezone.utc)
    anchor = _last_completed_wednesday(run_started)
    try:
        _connect(mt5, settings)
        initialized = True
        for symbol in SYMBOLS:
            try:
                results.append(_probe_symbol(mt5, symbol, anchor, args.max_years, args.probe_hours))
            except Exception as exc:
                logger.exception("Tick-history probe failed for %s", symbol)
                failures[symbol] = {"error_type": type(exc).__name__, "error": str(exc)}
        config = {
            "symbols": list(SYMBOLS),
            "probe_interval_years": 1,
            "probe_window_hours": args.probe_hours,
            "max_years": args.max_years,
        }
        payload = {
            "schema_version": 1,
            "generated_at_utc": _utc_iso(datetime.now(timezone.utc)),
            "status": "complete" if not failures else ("partial" if results else "failed"),
            "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest(),
            "probe_anchor_wednesday": anchor.isoformat(),
            "tick_data_written": False,
            "maximum_total_requested_hours": total_hours_ceiling,
            "results": results,
            "failed_symbols": failures,
        }
        _write_json(OUTPUT_PATH, payload)
        logger.info("Wrote tick probe summary to %s", OUTPUT_PATH)
    finally:
        if initialized:
            mt5.shutdown()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
