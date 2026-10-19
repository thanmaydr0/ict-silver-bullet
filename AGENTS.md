# Project rules (apply to every session)

## Spec and scope
- The strategy spec is `docs/blueprint/Automated_Forex_Strategy_Blueprint.md`. Read it end to end, including code blocks.
  Formulas are embedded as base64 images at the bottom of the file; extract them to `docs/blueprint/images/` and read them.
- Implement the blueprint FAITHFULLY first ("faithful baseline"). Any deviation goes in `docs/DEVIATIONS.md` with a reason.
  Improvements are allowed only after the faithful baseline's result is recorded, and only as labelled variants.
- The blueprint's headline return figures are assumptions, not evidence. Never cite them as results.
- The blueprint's code is a draft. Unit-test it, check it for lookahead, and fix bugs. Log every fix.

## Limits (single source of truth: config/limits.yaml)
- Hard limits: 3% daily, 7% overall. Auto-halt floors: 2.4% daily, 6.3% overall. See the table in the pack.
- Daily DD is measured on floating equity against the strictest day-start anchor (max of balance, equity).
  Overall DD is trailing from the equity high-water mark AND checked against initial capital.
- These limits are enforced INSIDE the backtest loop on every bar's adverse extreme, never after the fact.

## Honesty rules
- Profitability must come from the data. Do not curve-fit, loosen gates, peek at the lockbox, or raise leverage to hit a number.
- Log every variant and parameter set in `reports/trials_ledger.csv`. Report the trial count. Apply DSR and PBO.
- If a kill criterion trips, STOP and report. Do not tune past it. A truthful negative result is a success.
- Every report states data provenance, whether spreads were recorded or modelled, and the account type used for costs.

## Backtest integrity
- No lookahead. Any indicator, bar, level, regime label or model may only use data available at decision time.
  Add tests that fail if future data leaks (shift tests, truncation tests).
- Clock: MT5 export timestamps are broker server time (EET/EEST, GMT+2/+3, New York-close DST), not UTC.
  Convert server time -> UTC -> New York explicitly. Verify weekly closures land near 17:00 New York in every season. Add a test.
- Costs on EVERY leg: spread (recorded if present, else modelled and flagged), commission, slippage, latency (1-3 s), swaps
  for holds across rollover. Run all results at base cost AND +50% stressed. Account type is explicit config: STANDARD
  (wider spread, no commission) or RAW (tight spread + commission). Never charge both.
- Signals may use aggregated bars, but fills, stops and targets resolve on the underlying M1 bars.
  If stop and target are both inside one M1 bar, assume the stop hit first.
- Walk-forward with purged and embargoed validation. The most recent ~15% of history (min. 6 months) is a LOCKBOX,
  touched exactly once, for ONE frozen configuration.
- Reproducibility: fixed seeds, data manifest with hashes, config hash saved with every run.

## Monthly report (required for every presented run)
For EVERY calendar month in the data, write `reports/<run_id>/monthly/<YYYY-MM>.md` and one summary table
`reports/<run_id>/monthly_summary.csv` and `.md` with: month, TUNE/VALIDATE/LOCKBOX tag, symbols, engine, trades, win rate,
average R, expectancy, profit factor, gross and net return %, costs paid (spread, commission, slippage, swaps), max intraday
DD, worst daily DD, max trailing overall DD, number of soft/hard breaker events, best day, worst day, longest losing streak,
months hitting >= +10%, months profitable, months breaching a limit. Include a monthly equity chart. Produce it for:
(a) full history with the frozen configuration, (b) out-of-sample months only, (c) +50% stressed costs. State which supports
claims about the future (only (b) and the lockbox do).

## Process
- Work in stages. Report after each stage. If blocked (missing data, failing assumption), stop and ask. Never invent data.
- Demo accounts only. Never deploy, push, or use live credentials. No secrets in any file.
- Run the full test suite before reporting a stage done.
