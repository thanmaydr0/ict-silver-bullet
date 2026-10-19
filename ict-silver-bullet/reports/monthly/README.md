# Monthly validation artifacts

No historical backtest has been run in this request. This directory deliberately
contains no invented monthly returns or charts. The study executes on EC2 beside the existing prices; see `../data_handoff.md`.

`backtest.scalp_walkforward` writes a separate subdirectory for every logged tune,
validation, stress, baseline and sensitivity run. Each includes `summary.csv`,
`summary.md`, one monthly JSON report and PNG equity chart per observed month,
the compounded equity chart, and gzip archives of real trades and equity marks.

Rolling validation uses six full calendar months for tune and two for validate.
Account equity and breakers carry continuously across validation folds. Frozen
full-history charts are post-selection comparisons; recent July–October 2026
charts are retrospective holdouts, not independent lockbox evidence. The original
untouched-lockbox gate remains unsatisfied under the approved revised protocol.
