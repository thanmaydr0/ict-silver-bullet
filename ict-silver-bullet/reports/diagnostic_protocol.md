# Existing-engine diagnostic protocol

User-authorized diagnostic reuse of the original EURUSD/GBPUSD prices, 2026-10-19.
No strategy family is added or selected. Existing historical holdout receipts and
results remain intact; this is not independent evidence or a trading allocation.

| Item | Fixed treatment |
| --- | --- |
| Prices | Original, fingerprint-checked M1 files, no interpolation |
| Source time | Stored UTC labels, explicitly conditional and historically unverified |
| Rules | Existing four families and explicit score-8 mechanical ICT baseline |
| Reward | Last eligible choice from each existing rolling manifest: opening range 1.5, failed breakout 2, mean reversion 1.5, compression 2; ICT baseline existing geometry |
| Cost stages | Zero; spread only; spread and $7 round-trip/lot commission; full including .15 pip per-fill slippage and .15 pip extra entry latency allowance |
| R denominator | Original signal price-stop distance times actual units, excluding all fees; previous fee-inclusive engine R retained separately |
| Diagnostic account | $10,000,000, risk fraction 0.000001, approximately $10 loss budget; used to avoid lifetime risk censoring while preserving retail lot rounding and engine breakers |
| Resolution account | Original $10,000 and 0.05% screening risk; monthly returns belong to this account |
| Random seeds | All integers 0 through 199, no seed selection |
| Random controls | Reward 1.5 and 2, each at zero/full cost; a zero-cost 1:1 symmetry control |
| Random times | One uniform actual timestamp/pair per London/NY session per observed date; uniform direction; same prior ATR20/five-pip stop and 60-minute hold |
| Signal sessions | Existing 08:15–10:00 London/NY closed-bar decision windows, DST-aware zones, with the existing market-hours gate |
| Intrabar bounds | Stop-first; target-first; O-L-H-C for up/doji bars and O-H-L-C for down bars |
| M5/M15 | Completed contiguous aggregates only, unchanged indicator bar counts; wall-clock sessions/cooldown/holding unchanged; stops 3x/6x the known M1 ATR/five-pip rule |
| Execution | Original M1 bars even for M5/M15 signals; next-open fills |

The actual rerun cost table is the primary zero-cost/full-cost comparison. Its
trade counts can differ across stages because spread changes executable barrier
geometry, position duration and later opportunities. The identical-trade cost
bridge instead holds full-cost trades fixed: gross/reference-R, minus spread,
minus commission, minus slippage/latency. It is an accounting attribution, not a
claim to have re-executed those trades in a frictionless market. The ratio of
average cost in pips to average reference stop in pips and the average individual
cost/R are both retained; they are different estimands.

The null distribution is conditional on the existing historical price path.
Report all 200 per-seed means, their empirical 2.5/97.5 percentiles and the
one-sided randomization tail fraction `(1 + count(null >= real))/201`.
Rewards are matched to each family. The ICT baseline uses structural stops,
so an ATR-stop null is not an exact matched ICT inference; disclose that
limitation rather than treating its p-value as conclusive. Reused history and
family comparisons do not establish fresh out-of-sample significance.

A nonzero finite-sample random mean does not by itself prove a code bug.
Check zero-cost intervals, symmetric barrier-only win rates, price gaps,
finite holding exits and measured stop-first ambiguity. Accounting assertions
verify `gross - costs == net` on every completed trade. Risk flags and any
overall halt remain visible in diagnostic summaries.

The faster replay uses the existing engine. It skips only flat/no-order
intervals and retains daily endpoints. Once an order is pending or a position
is open, it processes every original M1 quote. Tests cover identical trades
and balances for full versus skipped replay, including two symbols, gaps and
a breaker. A real historical prefix independently matched the original engine,
the instrumented default and the faster replay exactly (96 trades).

No historical spread or volume was preserved in the original exports. There
is no empirical spread series to calibrate against, and no complete historical
news calendar. The hourly spread table reports assumptions, not measurements.
