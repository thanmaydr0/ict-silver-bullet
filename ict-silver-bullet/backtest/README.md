# MT5 history probes

Run these on the Windows EC2 host with the MT5 terminal open and logged in to the demo account. They read `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, optional `MT5_TERMINAL_PATH`, and `TRADED_PAIRS` from `executor/.env` (process environment values take precedence). No credentials are written to output files.

From the `ict-silver-bullet` project root, use a Python environment with the backtest dependencies and MT5 client installed:

```powershell
.\executor\.venv\Scripts\Activate.ps1
python -m pip install -r backtest/requirements.txt python-dotenv
python -m backtest.pull_history_m1
python -m backtest.pull_ticks_probe
python -m backtest.pull_calendar_probe
```

The scripts require `MT5_SERVER` to identify a demo, paper, or trial server and verify `account_info().trade_mode` is demo before querying history. They refuse any account they cannot confirm as demo.

Before the full M1 export, set MT5 **Max bars in chart** high enough and let the terminal download the symbol history. The export saves each 50,000-bar chunk to `backtest/data/m1/.staging/` and advances an atomic checkpoint. If the process or MT5 read fails, rerun the same command to resume; when positional reads hit a history boundary, it continues with `copy_rates_range` using the last saved raw timestamp. Use `--chunk-bars` to change the request size or `--symbols` to override `TRADED_PAIRS` for one run.

## Output files

- `backtest/data/m1/<SYMBOL>_<YEAR>.parquet` — one file per symbol and year, with `time`, OHLC, `tick_volume`, `spread`, and `real_volume`.
- `backtest/data/symbol_info/<SYMBOL>.json` — requested contract fields and trade-session windows when the installed Python API exposes them.
- `backtest/data/manifest.json` — raw first/last timestamps, row and gap counts, per-file SHA-256 hashes, symbol metadata hashes, and the offset observations.
- `backtest/data/m1/.staging/` — resumable chunk Parquet files and checkpoints retained for any incomplete symbol; removed after that symbol's annual files are finalized. A partial manifest lists staged-file hashes and failed symbols.
- `backtest/data/ticks_probe.json` — annual tick-history samples for EURUSD and USDJPY. Each request is one hour by default; the worst-case total request window is capped at 72 hours across both symbols. Tick rows are counted then discarded.
- `backtest/data/calendar_probe.json` — calendar API availability and annual history probe results. `backtest/data/calendar_events.csv` is written only if a calendar history function returns events.

M1 `time` values are stored unchanged as the integer values returned by MT5. The offset samples are diagnostic only and are never applied to bar timestamps. The manifest's `gap_count` counts every adjacent-row interval over 60 seconds, including normal market closures; no bars are filled. The annual partition uses the raw epoch's UTC calendar year.

Copy the complete `backtest/data/` directory from EC2 into the same path in the local `ict-silver-bullet` working copy. Keep its directory structure intact and use `manifest.json` to identify the files belonging to the export. If `manifest.json` reports `partial` and the M1 staging directory exists, retain it and rerun the M1 command on EC2; do not copy only the annual Parquet files until the export is complete. If MT5's Python API does not expose `calendar_*` functions, the calendar JSON records that result and no CSV is produced.
