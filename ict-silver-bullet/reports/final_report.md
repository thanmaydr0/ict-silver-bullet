# Validation status — incomplete

Updated 2026-10-19. This is a progress/blocker report, not a completed strategy
evaluation. **Zero historical strategy trials have been executed in this request.**

The user approved retrospective research using the existing dataset, with a
future untouched demo period replacing any claim that the newest historical
months were unexamined. Prior artifacts explicitly identify this history as
previously examined development data. The exact lifetime prior-trial count is
unknown and must remain disclosed.

## Completed

- Source-code audit: `audit_old_strategy.md`.
- EC2 metadata/timestamp inventory, gaps and source fingerprints:
  `data_inventory.md`. Original EURUSD and GBPUSD M1 coverage is October 18,
  2023 through October 16, 2026, with 1,114,442 rows each. No recorded spread
  or volume exists. Broker/server: user-confirmed MetaQuotes Ltd / MetaQuotes-Demo.
- Sourced survey and predeclared four-family shortlist:
  `research/strategies.md`, including rejected infrastructure/data-dependent ideas.
- Offline cost/risk policy, causal signals and explicit ICT baseline adapter,
  next-open replay, stop-first ambiguity, news gate, floating-bound monitoring,
  monthly reporting, rolling orchestration, block bootstrap, sizing frontier,
  day-cluster quarter-Kelly cap, multiple-testing diagnostic, sensitivity grid
  and holdout receipt in `backtest/scalp_*.py`.
- Existing full Python suite: **595 passed in 189.20 seconds** at its recorded
  checkpoint. Expanded harness tests subsequently: **21 passed**, including real
  signals for all four causal-prefix tests, monthly CSV/PNG generation, and
  rejection of a sharp parameter peak without retuning.
  Unit fixtures test mechanics only; their returns are not strategy evidence.

## What the code does and does not establish

The new offline policy applies soft daily 2%, hard daily 3%, soft overall 5%
and hard overall 7% from floating liquidation equity inside the bar loop.
Daily reset is 17:00 New York, following the existing system convention;
that is a documented account-day assumption, not a newly verified broker rule.
The daily soft limit flattens/halts until reset; soft overall halves risk.
Headroom limits and lot rounding restrict entries, but gaps can still breach
limits and are counted rather than clipped away.

Minute OHLC does not reveal the actual intraminute ordering or three-second
execution price. The specified path/latency allowances are conditional models.
Modelled spreads, commission, slippage and latency allowances are all stressed
by 50%. The original production risk constants are unchanged: this is a separate
offline policy, not a claim that the deployed live risk engine has been updated.
Integration remains gated on evidence and has not occurred.

No historical news calendar was located and the source-clock/DST mapping remains
unverified. The code accepts explicit clock scenarios and historical events,
but refuses to treat absent evidence as a live-eligible pass. No source prices
have been shifted, synthesized, filled, or replaced.

## Remote execution and remaining work

The user authorized an unsigned research-code push and offline execution on the existing EC2 PC on 2026-10-19. Prices stay on EC2; local data transfer is no longer required. Production processes are not restarted or reconfigured.

Raw-price integrity checks, historical-clock diagnosis, all candidate
walk-forward/monthly results, cost-stress outcomes, ICT comparison, regime
statistics, sensitivity results, Monte Carlo probabilities, actual trial
counts and empirical final selection remain **not run**. Report-generation and
orchestration code has unit coverage but has not been exercised end-to-end on
the historical dataset. The runner will replace this status report with actual
results only after completing the experiment.

## Verdict

The +10% monthly target has not been evaluated, and neither success nor
unachievability is established. No forward monthly target or trading allocation
is supportable from the completed work alone. The research runner is being transferred to EC2 for evaluation. No strategy has been integrated or deployed, and no live trading credentials were read or changed.
