"""Check MT5 Python calendar support and historical high-impact coverage.

Run on the Windows host with MT5 open:
    python -m backtest.pull_calendar_probe

The script feature-detects ``mt5.calendar_*``. It writes a CSV only when a
calendar history function returns events; the probe summary is always saved.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import csv
import importlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(__file__).resolve().parent / "data"
OUTPUT_PATH = DATA_DIR / "calendar_probe.json"
CSV_PATH = DATA_DIR / "calendar_events.csv"
ENV_PATH = ROOT / "executor" / ".env"
COMMON_CURRENCIES = ("USD", "EUR", "JPY", "GBP", "CHF", "CAD", "AUD", "NZD")
IMPORTANCE_NAMES = {0: "none", 1: "low", 2: "moderate", 3: "high"}
logger = logging.getLogger("pull-calendar-probe")


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


def _require_demo_server(settings: dict[str, Any]) -> None:
    server = str(settings.get("server", "")).casefold()
    if not any(marker in server for marker in ("demo", "paper", "trial")):
        raise RuntimeError("Refusing to initialize MT5: MT5_SERVER must identify a demo server.")


def _require_demo_account(mt5: Any) -> str:
    account = mt5.account_info()
    if account is None:
        raise RuntimeError("Cannot verify MT5 account type; refusing to probe without demo confirmation.")
    fields = account._asdict() if hasattr(account, "_asdict") else {}
    trade_mode = fields.get("trade_mode", getattr(account, "trade_mode", None))
    demo_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
    if trade_mode != demo_mode:
        raise RuntimeError("Refusing to probe: connected MT5 account is not confirmed as demo.")
    return "demo"


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if hasattr(value, "_asdict"):
        return {str(key): item for key, item in value._asdict().items()}
    names = getattr(getattr(value, "dtype", None), "names", None)
    if names:
        return {str(name): value[name] for name in names}
    return {}


def _rows(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, Mapping) or hasattr(result, "_asdict"):
        mapping = _as_mapping(result)
        return [mapping] if mapping else []
    names = getattr(getattr(result, "dtype", None), "names", None)
    if names:
        return [_as_mapping(row) for row in result]
    if isinstance(result, (list, tuple)):
        converted = [_as_mapping(row) for row in result]
        return [row for row in converted if row]
    try:
        return [_as_mapping(row) for row in result if _as_mapping(row)]
    except TypeError:
        return []


def _invoke_history(function: Any, start: datetime, end: datetime) -> tuple[Any, str]:
    attempts = (
        ((start, end), {}),
        ((), {"date_from": start, "date_to": end}),
        ((), {"datetime_from": start, "datetime_to": end}),
        ((start, end, "", ""), {}),
    )
    type_errors = []
    for args, kwargs in attempts:
        try:
            return function(*args, **kwargs), "positional" if args else "keyword"
        except TypeError as exc:
            type_errors.append(str(exc))
    raise TypeError("No supported calendar history call signature: " + " | ".join(type_errors[-2:]))


def _invoke_by_event(function: Any, event_id: int, start: datetime, end: datetime) -> Any:
    attempts = (
        ((event_id, start, end), {}),
        ((), {"event_id": event_id, "date_from": start, "date_to": end}),
        ((), {"event_id": event_id, "datetime_from": start, "datetime_to": end}),
    )
    errors = []
    for args, kwargs in attempts:
        try:
            return function(*args, **kwargs)
        except TypeError as exc:
            errors.append(str(exc))
    raise TypeError("No supported calendar-by-event call signature: " + " | ".join(errors[-2:]))


def _call_one(function: Any, value: Any) -> Any:
    try:
        return function(value)
    except TypeError:
        return function(**{"currency": value})


def _event_importance(value: Any) -> tuple[str | None, bool | None]:
    if value is None:
        return None, None
    if hasattr(value, "item") and callable(value.item):
        value = value.item()
    if isinstance(value, bool):
        return str(value), None
    if isinstance(value, (int, float)):
        number = int(value)
        name = IMPORTANCE_NAMES.get(number, str(value))
        return name, number == 3
    text = str(value).strip()
    normalized = text.lower()
    if "high" in normalized:
        return text, True
    if normalized in {"0", "1", "2", "3"}:
        number = int(normalized)
        return IMPORTANCE_NAMES[number], number == 3
    return text, None


def _json_safe(value: Any) -> Any:
    if hasattr(value, "_asdict"):
        return {str(key): _json_safe(item) for key, item in value._asdict().items()}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "item") and callable(value.item):
        return _json_safe(value.item())
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _get_event_details(mt5: Any, event_ids: set[int]) -> dict[int, dict[str, Any]]:
    function = getattr(mt5, "calendar_event_by_id", None)
    if not callable(function):
        return {}
    details = {}
    for event_id in sorted(event_ids):
        try:
            row = _as_mapping(function(event_id))
        except Exception as exc:
            logger.warning("Calendar event metadata unavailable for event %s (%s)", event_id, type(exc).__name__)
            continue
        if row:
            details[event_id] = row
    return details


def _discover_event_ids(mt5: Any) -> tuple[dict[int, dict[str, Any]], list[str]]:
    function = getattr(mt5, "calendar_event_by_currency", None)
    if not callable(function):
        return {}, []
    discovered: dict[int, dict[str, Any]] = {}
    errors = []
    for currency in COMMON_CURRENCIES:
        try:
            for row in _rows(_call_one(function, currency)):
                event_id = row.get("id", row.get("event_id"))
                if event_id is not None:
                    discovered[int(event_id)] = row
        except Exception as exc:
            errors.append(f"{currency}: {type(exc).__name__}: {exc}")
    return discovered, errors


def _calendar_years(now: datetime, max_years: int):
    for age in range(max_years + 1):
        year = now.year - age
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = min(datetime(year + 1, 1, 1, tzinfo=timezone.utc), now)
        yield year, start, end


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    keys = sorted({key for row in rows for key in row})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=keys, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _json_safe(value) for key, value in row.items()})
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def probe_calendar(mt5: Any, max_years: int) -> dict[str, Any]:
    functions = sorted(name for name in dir(mt5) if name.startswith("calendar_") and callable(getattr(mt5, name, None)))
    history = getattr(mt5, "calendar_value_history", None)
    by_event = getattr(mt5, "calendar_value_history_by_event", None)
    use_general_history = callable(history)
    event_catalog, catalog_errors = _discover_event_ids(mt5) if not use_general_history else ({}, [])
    event_ids = set(event_catalog)
    if callable(by_event) and not event_ids:
        event_catalog, catalog_errors = _discover_event_ids(mt5)
        event_ids = set(event_catalog)

    if not use_general_history and not (callable(by_event) and event_ids):
        return {
            "status": "calendar_api_unavailable" if not functions else "history_interface_unavailable",
            "calendar_functions_found": functions,
            "historical_values_function_available": use_general_history or callable(by_event),
            "historical_high_impact_events_confirmed": False,
            "deepest_year_with_values": None,
            "yearly_probes": [],
            "event_catalog_errors": catalog_errors,
            "csv_path": None,
            "note": "No usable Python calendar history interface was exposed; no calendar CSV was written.",
        }

    now = datetime.now(timezone.utc)
    raw_rows: list[dict[str, Any]] = []
    yearly_probes = []
    empty_streak = 0
    for year, start, end in _calendar_years(now, max_years):
        rows_for_year = []
        errors = []
        call_style = "unsupported"
        if use_general_history:
            try:
                result, call_style = _invoke_history(history, start, end)
                if result is None:
                    last_error = getattr(mt5, "last_error", lambda: None)()
                    errors.append(f"calendar_value_history returned None: {last_error}")
                else:
                    rows_for_year.extend(_rows(result))
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
        else:
            call_style = "by-event"
            for event_id in sorted(event_ids):
                try:
                    result = _invoke_by_event(by_event, event_id, start, end)
                    if result is None:
                        continue
                    for row in _rows(result):
                        row.setdefault("event_id", event_id)
                        rows_for_year.append(row)
                except Exception as exc:
                    errors.append(f"event {event_id}: {type(exc).__name__}: {exc}")
        high_count = 0
        for row in rows_for_year:
            row["query_year"] = year
            event_id = row.get("event_id", row.get("id"))
            if event_id is not None:
                try:
                    row["event_id"] = int(event_id)
                    catalog = event_catalog.get(int(event_id), {})
                    if not row.get("event_name") and catalog.get("name"):
                        row["event_name"] = catalog["name"]
                    if not row.get("currency") and catalog.get("currency"):
                        row["currency"] = catalog["currency"]
                    if row.get("importance") is None and catalog.get("importance") is not None:
                        row["importance"] = catalog["importance"]
                except (TypeError, ValueError):
                    pass
            label, is_high = _event_importance(row.get("importance", row.get("impact")))
            row["importance_name"] = label
            row["is_high_impact"] = is_high
            if is_high:
                high_count += 1
        raw_rows.extend(rows_for_year)
        yearly_probes.append({
            "year": year,
            "query_start_utc": _utc_iso(start),
            "query_end_utc": _utc_iso(end),
            "call_style": call_style,
            "returned_values": len(rows_for_year),
            "high_impact_values": high_count,
            "errors": errors,
        })
        empty_streak = 0 if rows_for_year or errors else empty_streak + 1
        if raw_rows and empty_streak >= 2:
            break

    resolvable_event_ids = set()
    for row in raw_rows:
        try:
            if row.get("event_id") is not None:
                resolvable_event_ids.add(int(row["event_id"]))
        except (TypeError, ValueError):
            continue
    event_info = _get_event_details(mt5, resolvable_event_ids)
    high_counts_by_year: dict[int, int] = {}
    for row in raw_rows:
        try:
            event_key = int(row["event_id"]) if row.get("event_id") is not None else None
        except (TypeError, ValueError):
            event_key = None
        details = event_info.get(event_key) if event_key is not None else None
        if details:
            if not row.get("event_name") and details.get("name"):
                row["event_name"] = details["name"]
            if not row.get("currency") and details.get("currency"):
                row["currency"] = details["currency"]
            if row.get("importance") is None and details.get("importance") is not None:
                row["importance"] = details.get("importance")
            label, is_high = _event_importance(row.get("importance", row.get("impact")))
            row["importance_name"] = label
            row["is_high_impact"] = is_high
        if row.get("is_high_impact") is True:
            year = int(row.get("query_year", 0))
            high_counts_by_year[year] = high_counts_by_year.get(year, 0) + 1

    for probe in yearly_probes:
        probe["high_impact_values"] = high_counts_by_year.get(probe["year"], 0)

    seen = set()
    unique_rows = []
    for row in raw_rows:
        key = (str(row.get("event_id", "")), str(row.get("time", row.get("timestamp", ""))), str(row.get("period", "")))
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(row)
    _write_csv(CSV_PATH, unique_rows)
    high_rows = [row for row in unique_rows if row.get("is_high_impact") is True]
    years_with_values = [probe["year"] for probe in yearly_probes if probe["returned_values"]]
    any_errors = any(probe["errors"] for probe in yearly_probes)
    return {
        "status": (
            "partial" if unique_rows and any_errors else
            "history_returned" if unique_rows else
            "probe_error" if any_errors else
            "history_function_returned_no_values"
        ),
        "calendar_functions_found": functions,
        "historical_values_function_available": True,
        "historical_high_impact_events_confirmed": bool(high_rows),
        "high_impact_classification_available": any(row.get("is_high_impact") is not None for row in unique_rows),
        "event_rows": len(unique_rows),
        "high_impact_event_rows": len(high_rows),
        "deepest_year_with_values": min(years_with_values) if years_with_values else None,
        "yearly_probes": yearly_probes,
        "event_catalog_errors": catalog_errors,
        "csv_path": "data/calendar_events.csv" if unique_rows else None,
        "calendar_time_note": (
            "Query bounds were UTC-aware Python datetimes. Event time fields are exported as returned. "
            "Native MQL5 calendar functions use trade-server time; verify wrapper semantics if this API is exposed."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-years", type=int, default=30, help="maximum annual history windows (default: 30)")
    args = parser.parse_args(argv)
    if not 1 <= args.max_years <= 50:
        parser.error("--max-years must be between 1 and 50")

    from executor.logging_setup import setup_logging

    global logger
    logger = setup_logging("pull-calendar-probe")
    settings = _load_settings()
    _require_demo_server(settings)
    mt5 = importlib.import_module("MetaTrader5")
    initialized = False
    try:
        _connect(mt5, settings)
        initialized = True
        account_type = _require_demo_account(mt5)
        result = probe_calendar(mt5, args.max_years)
        result.update({
            "schema_version": 1,
            "generated_at_utc": _utc_iso(datetime.now(timezone.utc)),
            "account_type": account_type,
            "max_years": args.max_years,
        })
        _write_json(OUTPUT_PATH, result)
        if result["status"] == "calendar_api_unavailable":
            logger.warning("MT5 Python package exposes no calendar_* functions; summary written to %s", OUTPUT_PATH)
        else:
            logger.info("Calendar probe status=%s; summary written to %s", result["status"], OUTPUT_PATH)
    finally:
        if initialized:
            mt5.shutdown()
    return 1 if result["status"] in {"partial", "probe_error"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
