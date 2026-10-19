# Remote research execution

User instruction, 2026-10-19: push unsigned research code, pull it on the
existing remote PC, and execute all historical evaluation there. This
supersedes the earlier local-copy proposal. Existing source files stay at
`C:/ict-silver-bullet/ict-silver-bullet/backtest/data/validation_20261018`.

Use the existing Edge AWS Session Manager session. Pull only after checking
remote Git status. Keep research dependencies isolated under ignored
`backtest/.venv/research_packages`; do not change production dependencies.
Run tests and the offline study using the existing remote Python runtime,
in a hidden process at below-normal priority. Leave trading processes running.

From `C:/ict-silver-bullet/ict-silver-bullet`, with isolated plotting dependencies
on PYTHONPATH:

```powershell
brain/.venv/Scripts/python.exe -m backtest.scalp_walkforward --data-dir backtest/data/validation_20261018 --output reports/monthly/study --clock label_utc --simulations 2000
```

`label_utc` preserves stored labels as an explicit conditional scenario;
it does not certify the broker's historical clock. Do not choose a clock
based on profitability. Missing historical news and unknown prior trials
remain disclosed. The global exclusive holdout receipt must not be deleted
to repeat selection. Recovery must inspect existing results and ledger first.
