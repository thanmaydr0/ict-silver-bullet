# Proposed resolution of the validation provenance conflict

Status: **approved by the user on 2026-10-19; no candidate runs executed yet**.

The original request requires both the existing three-year dataset and a fresh,
untouched latest-four-month lockbox. Existing research specifications call that
history previously examined development data. Renaming a date range cannot
restore its statistical independence.

Approved treatment:

1. Keep the same original EURUSD/GBPUSD dataset, original fingerprints and price
   values. Verify the historical clock before any session-dependent judgment.
   Do not re-pull data or infer the clock from the strategy's profits.
2. Complete the engine audit and causal tests, then the sourced research survey
   and small, predeclared candidate shortlist in the requested stage order.
3. Reconstruct prior trials as far as existing scripts and run manifests permit.
   Keep new trials separate; explicitly disclose an unknown historical count
   rather than invent an exact multiple-testing adjustment.
4. Run rolling historical selection/validation, frozen-strategy comparisons,
   cost stress, monthly reporting, regime diagnostics and bootstrap risk
   analysis as **retrospective research**. Distinguish tune months from later
   months, but do not market later months as a newly untouched lockbox.
5. Evaluate the recent four months only after the final configuration is frozen
   in this request, and label the result **previously examined retrospective
   holdout**, not LOCKBOX. Predeclare its treatment before evaluation.
6. Reserve a future, predeclared demo observation period as the genuinely
   untouched test. Do not claim future results before they exist. Do not
   deploy or integrate a candidate as passing the original gates on the basis
   of retrospective history alone.

All original cost, latency, floating-equity, drawdown, sensitivity and reporting
requirements remain. The method change concerns evidence provenance, not a
relaxation of drawdown limits or a permission to curve-fit. The requested final
report must state that the original lockbox gate remains unsatisfied until
independent evidence exists. This proposal does not assert that any strategy
passes, fails, or supports a particular monthly target.
