"""Independent UTC Bid/Ask M1 export from Dukascopy's public historical feed.

Does not modify or depend on Phase 1's MT5 exporter. Native daily BI5 records
use UTC-day seconds, four big-endian integer prices and float volume. Format
reference: dukascopy-node and its historical-data clients. No bars are filled.
"""

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import lzma
import math
from pathlib import Path
import struct
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from brain.logging_setup import setup_logging

PAIRS = {"EURUSD": 100000, "GBPUSD": 100000, "USDJPY": 1000}
RECORD = struct.Struct(">5If")


def daily_url(pair, day, side):
    if pair not in PAIRS or side not in {"BID", "ASK"}:
        raise ValueError("unsupported reference pair or price side")
    return (f"https://datafeed.dukascopy.com/datafeed/{pair}/{day.year}/"
            f"{day.month-1:02d}/{day.day:02d}/{side}_candles_min_1.bi5")


def decode(data, pair, day):
    """Validate and decode actual records; zero-activity flat bars are excluded."""
    if pair not in PAIRS:
        raise ValueError("unsupported price scale")
    if not data:
        return []
    raw = lzma.decompress(data)
    if len(raw) % RECORD.size:
        raise ValueError("truncated BI5 candle record")
    base = datetime.combine(day, datetime.min.time(), timezone.utc)
    result, previous = [], -1
    for seconds, opening, closing, low, high, volume in RECORD.iter_unpack(raw):
        if seconds <= previous or seconds >= 86400 or seconds % 60:
            raise ValueError("invalid or duplicate UTC minute offset")
        previous = seconds
        if not math.isfinite(volume) or volume < 0:
            raise ValueError("invalid activity value")
        if opening == closing == low == high and volume == 0:
            continue
        if not 0 < low <= min(opening, closing) <= max(opening, closing) <= high:
            raise ValueError("invalid reference OHLC")
        scale = PAIRS[pair]
        result.append(dict(pair=pair, timeframe="M1", ts=base+timedelta(seconds=seconds),
            open=opening/scale, high=high/scale, low=low/scale, close=closing/scale))
    return result


def merge_sides(bids, asks):
    """Match real Bid/Ask minutes, without inventing a quote across a gap."""
    ask_by_time = {r["ts"]: r for r in asks}
    result = []
    for row in bids:
        ask = ask_by_time.get(row["ts"])
        if ask is None:
            continue
        if any(ask[field]+1e-10 < row[field] for field in ("open", "high", "low", "close")):
            raise ValueError("Ask below Bid in matched reference candles")
        result.append(dict(row, **{"ask_"+field: ask[field] for field in ("open", "high", "low", "close")}))
    return result


def fetch(url, *, attempts=3, opener=urlopen, sleeper=time.sleep):
    """Bounded retries; respect rate limits and never substitute empty on error."""
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": "forex-ict-reference-research/1.0"})
            with opener(request, timeout=25) as response:
                if response.status != 200:
                    raise RuntimeError(f"Reference HTTP {response.status}; no data accepted")
                return response.read()
        except HTTPError as error:
            if error.code == 404:
                return b""
            if error.code not in {429, 500, 502, 503, 504} or attempt == attempts-1:
                raise
            retry = error.headers.get("Retry-After", "60")
            delay = int(retry) if retry.isdigit() else 60
            remaining = max(delay, 60 if error.code == 429 else 2**attempt)
            while remaining > 0:
                step = min(remaining, 60)
                sleeper(step)
                remaining -= step
        except (URLError, TimeoutError):
            if attempt == attempts-1:
                raise
            sleeper(2**attempt)
    raise RuntimeError("reference download exhausted")


def export(pairs, start, end, output_dir, *, fetcher=fetch, on_progress=None, pause=.5):
    """UTC dates [start,end), resumable raw cache and explicit source manifest."""
    import pandas as pd
    pairs = tuple(pairs)
    if not pairs or any(p not in PAIRS for p in pairs) or start >= end or pause < 0:
        raise ValueError("supported pairs and increasing UTC dates required")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manifest = dict(source="Dukascopy public daily BI5", clock="UTC day + seconds; no MT5 offset",
                    quote_basis="matched historical Bid and Ask M1 OHLC", start=str(start), end=str(end), pairs={})
    for pair in pairs:
        all_rows, receipts, missing = [], [], []

        def read_side(day, side):
            url = daily_url(pair, day, side)
            cache = output/"raw"/pair/f"{day}_{side}.bi5"
            if cache.exists():
                data = cache.read_bytes()
            else:
                data = fetcher(url)
                decode(data, pair, day)
                if data:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    temporary = cache.with_suffix(".bi5.tmp")
                    temporary.write_bytes(data)
                    temporary.replace(cache)
                if pause:
                    time.sleep(pause)
            return data

        # Reject unavailable end coverage before downloading years of history.
        probe = end-timedelta(days=1)
        while probe.weekday() >= 5:
            probe -= timedelta(days=1)
        if not all(decode(read_side(probe, side), pair, probe) for side in ("BID", "ASK")):
            raise RuntimeError(f"Reference feed has no end coverage for {pair}")
        day = start
        while day < end:
            if day.weekday() == 5:
                day += timedelta(days=1)
                continue
            sides = {}
            for side in ("BID", "ASK"):
                url = daily_url(pair, day, side)
                data = read_side(day, side)
                sides[side] = decode(data, pair, day)
                receipts.append(dict(day=str(day), side=side, url=url, bytes=len(data),
                                     sha256=hashlib.sha256(data).hexdigest(), bars=len(sides[side])))
            matched = merge_sides(sides["BID"], sides["ASK"])
            all_rows.extend(matched)
            if not matched and day.weekday() < 5:
                missing.append(str(day))
                if len(missing) > 10 and not all_rows:
                    raise RuntimeError("No reference coverage across requested weekdays")
            if on_progress:
                on_progress(pair, day, len(matched), len(all_rows))
            day += timedelta(days=1)
        weekdays = sum((start+timedelta(days=i)).weekday() < 5 for i in range((end-start).days))
        if (not all_rows or len(missing) > max(5, .05*weekdays) or
                all_rows[0]["ts"].date() > start+timedelta(days=7) or
                all_rows[-1]["ts"].date() < end-timedelta(days=7)):
            raise RuntimeError(f"Insufficient reference coverage for {pair}; no success manifest written")
        frame = pd.DataFrame(all_rows)
        target = output/f"{pair}_M1.parquet"
        temporary = target.with_suffix(".parquet.tmp")
        frame.to_parquet(temporary, index=False)
        temporary.replace(target)
        manifest["pairs"][pair] = dict(rows=len(all_rows), first=all_rows[0]["ts"].isoformat(),
            last=all_rows[-1]["ts"].isoformat(), missing_weekdays=missing, files=receipts)
        (output/"provenance.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", default="EURUSD,GBPUSD,USDJPY")
    parser.add_argument("--from-date", type=date.fromisoformat, required=True)
    parser.add_argument("--to-date", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    logger = setup_logging("reference_history")
    export(args.pairs.split(","), args.from_date, args.to_date, args.output_dir,
           on_progress=lambda pair, day, bars, total: logger.info("%s %s: %d matched bars; %d total", pair, day, bars, total))


if __name__ == "__main__":
    main()
