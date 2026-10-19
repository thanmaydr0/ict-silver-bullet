# Stage 0 data check: existing EC2 export

Inspected on 2026-10-19 through Edge and AWS Systems Manager Session Manager.
This is a metadata/timestamp audit, not a strategy backtest. No price path was
evaluated and no lockbox strategy result was examined.

## Location and contents

Remote directory:
`C:/ict-silver-bullet/ict-silver-bullet/backtest/data/validation_20261018/`.

| File | Rows | Bytes | First timestamp (labelled UTC) | Last timestamp (labelled UTC) | Duplicate timestamps | Gaps exceeding interval | Largest gap (minutes) |
| --- | ---: | ---: | --- | --- | ---: | ---: | ---: |
| EURUSD_M1.parquet | 1,114,442 | 15,559,611 | 2023-10-18 05:49 | 2026-10-16 23:59 | 0 | 523 | 4,322 |
| GBPUSD_M1.parquet | 1,114,442 | 15,648,112 | 2023-10-18 05:49 | 2026-10-16 23:59 | 0 | 615 | 4,322 |
| EURUSD_H1.parquet | 18,602 | 599,559 | 2023-10-18 06:00 | 2026-10-16 23:00 | 0 | 167 | 4,380 |
| GBPUSD_H1.parquet | 18,601 | 601,183 | 2023-10-18 06:00 | 2026-10-16 23:00 | 0 | 168 | 4,380 |

Every file has these columns: `pair`, `timeframe`, `ts`, `open`, `high`, `low`,
`close`. All four timestamp series are monotonically increasing. The timestamp
dtype reports UTC. There is **no recorded spread, Ask OHLC, tick volume or real
volume column**. Any subsequent friction results must explicitly identify spreads
as modelled, not recorded. True VWAP and volume-based signals cannot be computed
from these columns.

The top-level `backtest/data/EURUSD_M1.parquet` and `GBPUSD_M1.parquet` are
different, shorter exports: 100,008 and 100,000 rows respectively, from July 2,
2026 through October 7, 2026. Do not accidentally substitute those for the
three-year files or merge overlapping exports into the experiment.

## M1 gap diagnostics

Counts below use the stored timestamp labels. They do **not** assume that every
calendar minute should have a price. No prices or missing bars were filled.

| Symbol | Gaps >1 to 5 minutes | >5 to 60 minutes | >60 to 720 minutes | >720 minutes | Absent calendar minutes between first/last rows | Saturday/Sunday bars |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| EURUSD | 313 | 42 | 7 | 161 | 462,009 | 0 / 0 |
| GBPUSD | 421 | 25 | 8 | 161 | 462,009 | 0 / 0 |

Absent calendar minutes include weekends and holidays. They must not be reported
as missing tradable bars. Classification against the broker's actual market
calendar remains pending. Invalid OHLC, off-grid timestamps and raw-file schema
types have not yet been fully audited; zero duplicates is not proof of overall
data quality.

The last three long gaps, for both symbols, run from:

- 2026-09-25 23:59 to 2026-09-28 00:00;
- 2026-10-02 23:59 to 2026-10-05 00:00;
- 2026-10-09 23:59 to 2026-10-12 00:00.

Each is a 2,881-minute timestamp difference. The consistent Friday-midnight to
Monday-midnight boundaries are a source-clock warning: a UTC dtype alone does
not establish that broker wall-clock timestamps were correctly converted. This
is a suspicion requiring provenance, not proof of a specific offset. Do not
silently apply a guessed +2/+3-hour correction. The existing exporter samples a
single broker offset and explicitly cannot reconstruct historical DST changes.

## Source fingerprints

SHA-256 values computed on EC2 before any transformation:

```text
EURUSD_M1.parquet  0d5c5d2bd2cb8037a3bd5a9d2437ef234629736a50b8747017d821ffb26fcc3a
GBPUSD_M1.parquet  656f4acb24ccf08fb2a538ab0bfdc88663b90230982b32df67be5f2decee84ac
EURUSD_H1.parquet  9bc970832dafcc51c880bc7ce8c6f180d974bac5504062086360a1e2fdbad878
GBPUSD_H1.parquet  f76e5c28069d0a49143b7f109a96ddad412d4a66f1359a64d8ad7254ea58eb61
```

## Remaining requirements before judging a strategy

1. Establish the export's broker/server identity, timestamp conversion and
   historical DST rule. No metadata-only observation can establish this reliably.
2. Establish prior use of the newest four months. Existing research code says
   examined data is development data; a new split cannot make it untouched again.
3. Locate an existing historical news-event calendar, or explicitly document
   unavailable historical blackouts rather than claim the gate was exercised.
4. Complete raw OHLC validation and broker-calendar gap classification before
   any candidate run. Record any exclusions without inventing replacement bars.

No trials have been run in this request. The trials ledger contains only its
header. No pass/fail verdict, monthly target, Monte Carlo probability or trading
risk recommendation is supported yet.

## Follow-up: broker identified and prior research provenance found

The user identified the source as **MetaQuotes Ltd / MetaQuotes-Demo**. This
identifies the server, but does not prove the export's historical conversion.

The official [Python copy_rates_range documentation](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesrange_py)
describes returned timestamps as UTC and exposes spread and volume fields in
the raw return structure. This exporter discards those fields. Current live
feeder log lines from October 13, 2026 report a sampled +3-hour offset for both
symbols; a current offset does not determine historical DST.

First-hand tests in an [MQL5 discussion](https://www.mql5.com/en/forum/476823)
report UTC+2/+3 server time but disagree about US versus European DST for
EURUSD. These community observations are not an authoritative 2023–2026 mapping.
Do not use the source website's ownership to elevate user posts into vendor
confirmation. No fixed or DST-aware correction has been applied to these files.

The following existing artifacts were located on EC2 (source files inspected,
not executed):

- `C:/Windows/Temp/ict_export_7major.py`: exports to a separate
  `logs/scalper-screen-20261018/market_data/` folder, converting raw epoch seconds
  directly to UTC without subtracting an offset. It writes `data_provenance.json`.
  This is a separate export; it is not proof of how `validation_20261018` was made.
- `C:/Windows/Temp/ict-breakout-holdstudy.py` and
  `ict-breakout-management.py`: read the `validation_20261018` price files and
  earlier tune trades. Prior research exists beyond this request's empty ledger.
- `logs/scalping-research-20261018/run_specification.json` explicitly records
  `data_status: previously examined history, development only` and split
  `2025-10-16T00:00:00Z`.
- `logs/scalping-research-20261018/screen/frozen_specification.json` independently
  records `data_status: previously examined history: development only` with the
  same split.
- That screen directory contains prior ICT 2R, breakout 1.5R and breakout 2R
  tune JSON and JSONL files. No outcome statistics were extracted in this audit;
  exact prior trial counts and the scope of any prior later-period evaluation
  remain to be reconstructed. File names alone do not prove completed trials.

Consequently an untouched latest-four-month lockbox **cannot be certified** from
the existing artifacts. The three named variants are not the full historical
multiple-testing count. A zero-trial ledger for this request must not be read as
zero prior research trials.

The validation-design conflict needs resolution before candidate evaluation:
continue with explicitly retrospective research and reserve future demo data as
the genuine untouched test, or identify a demonstrably unexamined portion of the
existing history. Retrospective results cannot satisfy the original untouched
lockbox gate, regardless of their numerical performance. No new prices are
requested and no claim of an impossible 10% target follows from this limitation.
