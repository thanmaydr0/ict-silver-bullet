# Stage 0: preliminary code audit

Date: 2026-10-19. Status: **dataset located on EC2; historical clock provenance unresolved**.

This is a source-code audit, not an empirical explanation of historical losses.
No historical backtest was run, no candidate was selected, and no performance
or achievable monthly-return claim can be made from this audit.

## Findings

| Area | Observed implementation | Consequence / required work |
| --- | --- | --- |
| Source clock | `backtest/pull_history.py::_pull_pair` subtracts one sampled/configured broker offset over the entire export; its comment acknowledges historical DST cannot be reconstructed this way. | Historical UTC labels require source provenance and the broker's historical clock rule. Do not blindly shift already-correct UTC prices. |
| Session clock | `brain/scheduler/killzone_clock.py` uses `ZoneInfo("America/New_York")`; naive datetimes are rejected. | NY DST is handled in this clock, but cannot repair incorrect source timestamps. |
| Original replay costs | `backtest/replay.py::replay` explicitly has no spread, commission or slippage model. | Its returns do not represent executable net results. |
| Other research paths | `ResearchConfig` defaults to 0.5-pip spread, 0.1-pip slippage per side and USD 7 commission per lot round trip. `ask_quotes` can consume matched Ask OHLC. | Do not describe every existing path as cost-free. The fixed spread is not the requested time-of-day model; these are existing defaults, not approved assumptions for a new experiment. |
| Execution timing | Research paths implement next-open fills. The original replay instead fills a pending limit if the following bar spans its price. No explicit 1–3 second latency model was found in these fill paths. | A next-open quote is not an observed quote several seconds later. New tests must cover adverse latency allowances and uncertain intrabar execution. |
| Floating drawdown | The original replay marks equity at M1 closes and checks breakers after position processing. | Intraminute floating breaches can be missed. Stop-first exit ordering alone does not validate floating-equity drawdown. |
| Risk thresholds | `brain/risk/circuit_breakers.py` has soft daily 2.5%, hard daily 3.25%, soft overall 4.5%, hard overall 5.25%. | These differ from the requested 2%/3% daily and 5%/7% overall rules. Daily baseline is balance at 17:00 NY; overall reference is floating-equity high-water mark. The requested account day-reset convention still needs to be made explicit. |
| News | Replay contexts provide empty news events; reports label historical news as skipped. | Existing backtests do not establish performance with historical news blackouts. Locate an existing historical event calendar before making that claim. |
| Pivots | `find_swing_points` requires complete left and right flanks; `brain.app::_bars` excludes incomplete bars. | Right-hand confirmation is not automatically lookahead. Historical MSS/pivot consumption still needs prefix-invariance tests at each decision time; no blanket no-lookahead certification is justified yet. |
| Explicit ICT rules | `_detect_ict_candidate` specifies FVG midpoint entry, a sweep-extreme stop buffered by one pip, stop-distance and entry-distance guards, and a 2R target with liquidity-space check. | Rules are present in current code. Attribution of old losses to undefined rules would be unsupported without the original run configuration. |
| Baseline identity | `brain.app::_detect_candidate` currently defaults to `trend_reclaim`; ICT requires `strategy="ict"`. `replay` does not explicitly set ICT in its context. | Calling the default replay “old ICT Silver Bullet” would mislabel the strategy. A future baseline must explicitly select ICT on the common engine. |
| Monte Carlo | `backtest/monte_carlo.py` permutes closed-trade outcomes without replacement over the sequence, and stops at target/hard breaker. | This is not a 30-calendar-day block bootstrap, does not retain intratrade equity paths and does not supply the requested monthly breach probabilities. |
| Walk-forward provenance | `core_walk_forward` explicitly labels examined history retrospective development data and disallows live eligibility. | Preserve that disclosure. A new untouched lockbox cannot be assumed merely because a new date split is created. |
| Data normalization | `normalize_history` rejects conflicting duplicates but silently collapses identical duplicates. | Raw data quality counts must be recorded before normalization. |

## Data discovery

- Expected default location: `ict-silver-bullet/backtest/data/`, from the exporter and replay CLI. This directory is absent in this checkout.
- Searched the workspace recursively, including ignored/hidden files and excluding dependency folders, for Parquet, CSV, JSONL, Feather and archives. A separate recursive extension search also found dependency test fixtures, not market history.
- No saved backtest reports or historical candle dataset were located in this repository. `.gitignore` excludes `backtest/data/` and `*.parquet`; this explains why a clone need not include exports, but does not establish where the user's copy resides.
- Checked the neighboring `C:/5thsem/Projects/forex_auto` project. Its `backtest/results_2026-09-12.csv` contains trade-level fields, not the required OHLC history; its signals snapshot is not a replacement dataset. One unrelated pytest-cache directory was inaccessible.
- Symbols, coverage, row counts, raw duplicates, missing minutes, gaps, spread availability and column inventory are **unknown**, not zero. They cannot be inferred from configured symbols or exporter schemas.

## Decision and next input

### Update after authorized AWS inspection

The initial local-discovery blocker below is superseded: the user authorized
AWS access through Edge and signed into the console. Session Manager reached
the running `ict_silver_bullet` Windows instance. The existing three-year export
is in `C:/ict-silver-bullet/ict-silver-bullet/backtest/data/validation_20261018/`.
See `data_inventory.md` for actual counts, fingerprints, gap diagnostics and
the unresolved timestamp provenance. No strategy performance was evaluated.

Existing local verification completed: **172 tests passed in 54.65 seconds**
using `python -m pytest tests/backtest tests/brain/detection/test_swings.py
tests/brain/scheduler/test_killzone_clock.py tests/brain/risk/test_risk.py -q`.
These tests establish existing behavior, not compliance with the new specification.

The AWS access was read-only except for a temporary loopback-only file-serving
process used to attempt retrieval. That process was stopped. CloudShell could
not initialize because AWS reported account verification in progress. No
firewall, IAM, live-trading credentials, source files or deployed strategy were
changed. The price files have not been downloaded or re-pulled.

### Original discovery status (historical record)

Pause at the data check, following the user's instruction to stop and ask when
blocked. Supply the absolute path of the existing three-year historical dataset,
or the already-existing shared/mounted location. If it resides only on EC2, make
that existing export available locally or identify an authorized access route.
No new market-data pull is requested.

Also identify any existing historical news file and prior trial ledger/run
reports if stored alongside that export. Prior use of the latest four months
must be disclosed before calling them an untouched lockbox.

At initial local inspection no strategy or live-risk code was modified and no
remote host was contacted. The AWS inspection above supersedes that access status.
Nothing was deployed, committed or pushed. Engine fixes,
research, harness construction and acceptance testing remain unfinished.
