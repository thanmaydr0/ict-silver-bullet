# Intraday FX hypotheses and evidence limits

Research date: 2026-10-19. This survey motivates tests; it is not evidence that
the proposed rules are profitable on MetaQuotes-Demo or support +10% monthly.
Evidence from other venues, instruments and periods is not a retail FX backtest.
Primary research below was retrieved using Firecrawl. Abstract-only coverage is
identified rather than represented as a full replication.

## Evidence and feasibility

| Approach | Claimed inefficiency / possible persistence | Evidence and its limits | Failure modes / feasibility here |
| --- | --- | --- | --- |
| Session / opening-range breakout | Concentrated information and liquidity at regional opens can initiate directional repricing; stop orders beyond salient ranges can reinforce it. | Holmberg, Lönnbark and Lundström's ORB working-paper abstract reports favorable results for an opening-range rule and identifies crude-oil futures and contraction/expansion as its setting. Only the abstract/metadata were accessible; this is not an FX or retail-cost validation. [Umeå working paper 845](https://swopec.hhs.se/umnees/abs/umnees0845.htm). | False breaks, news jumps, expensive narrow stops and delayed entries. A completed 15-minute range and subsequent closed-M1 break are implementable with the existing polling path; a first-tick race is not. |
| Failed-breakout fade | Transient liquidity demand may exhaust without continued information flow, allowing price to return into a prior range. | Osler distinguishes positive-feedback stop activity from negative-feedback take-profit activity. That mechanism motivates testing continuation against fading; it does not establish that the proposed wick/close rule identifies either flow. [NY Fed staff report 150, author abstract](https://www.newyorkfed.org/research/staff_reports/sr150.html). | Fading a genuinely informed trend can produce repeated losses. Restrict to a low directional-efficiency state, use a hard stop, and never martingale. Feasible on M1; no assertion that a wick reveals actual stop orders. |
| VWAP / intraday mean reversion | Temporary inventory pressure or price impact may revert toward an intraday reference as liquidity returns. | Barzykin and Lillo solve liquidation relative to a VWAP benchmark under transient impact. This is an execution model, not proof of speculative VWAP-reversion alpha. [Authors' preprint, arXiv 1901.02327v2](https://arxiv.org/html/1901.02327v2). | A benchmark is not a fair-value oracle. The actual export contains neither tick nor traded volume: true VWAP is infeasible. Test a rolling **time average** as a separately named OHLC hypothesis, with a range-regime filter; reject trade-volume claims. |
| Volatility compression → expansion | Quiet intervals may precede new information, liquidity arrival or stop-triggered expansion. Volatility persistence alone predicts magnitude, not direction. | The ORB abstract motivates contraction/expansion. Intraday FX activity studies demonstrate systematic changes in volatility with market activity; neither source validates a generic “squeeze” signal. [ORB abstract](https://swopec.hhs.se/umnees/abs/umnees0845.htm), [Ito and Hashimoto, NBER 12413](https://www.nber.org/system/files/working_papers/w12413/w12413.pdf). | Compression can persist; spread can exceed the expected move. Use completed rolling ranges and a directional expansion close; no future volatility percentile, full-day range or same-bar fill. Feasible at M1 speed. |
| Momentum after volatility events | Information diffusion and stop-triggered positive feedback can persist after the immediate jump. | Osler's FX evidence motivates persistence at horizons relevant to delayed execution. Gao et al.'s author abstract documents first-to-last-half-hour predictability in US equity ETFs, with stronger association in volatile/news states; that result is not transferable proof for intraday FX. [NY Fed](https://www.newyorkfed.org/research/staff_reports/sr150.html), [Gao et al., SSRN 2552752, author abstract only](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2552752). | The opening jump may already contain the entire opportunity; slippage and news blackouts can remove apparent profits. Retain directional expansion within the compression family; reject sub-second news sniping. |
| Tick-volume / range-imbalance proxies | Activity and signed pressure may accompany price discovery. | Berger et al. analyze actual signed institutional EBS order flow. Their finding is not evidence that candle body/range measures true order flow. [Federal Reserve IFDP 830r, revised September 2006](https://www.federalreserve.gov/pubs/ifdp/2005/830/revision/ifdp830.htm). | No volume column exists in this export. Tick-volume signals are unavailable, and OHLC body/range is merely a price-shape proxy. Use body size relative to prior ATR as a transparent filter, not “institutional imbalance.” |
| Cross-asset lead–lag | Related instruments may incorporate common information at different speeds. | EBS research establishes associations between signed flow and returns but does not provide a directly usable two-retail-pair lead–lag model. Shared USD exposure also creates contemporaneous correlation without a predictable lag. [Federal Reserve IFDP 830r](https://www.federalreserve.gov/pubs/ifdp/2005/830/revision/ifdp830.htm). | Unverified symbol clocks, asynchronous bars and costs can manufacture a lag. No futures/order-book feed exists in the original dataset. Reject as a candidate until synchronization is independently established; one portfolio position avoids doubling USD exposure. |
| Time-of-day spread and volatility | Regional participation and overlap alter available liquidity and trading costs. | Ito and Hashimoto study firm EBS quotes and transactions, including DST. Their observed patterns vary by region and differ from equity-market patterns. Only the first eight PDF pages were read here; detailed unseen tables are not being asserted. [NBER working paper 12413](https://www.nber.org/system/files/working_papers/w12413/w12413.pdf). | Old institutional observations do not calibrate a current demo broker's spreads. Use explicit conservative assumed spread profiles, report them as MODELLED, and stress **all** friction components by 1.5. |
| Regime filters | Trend/range and elevated volatility can change which of momentum or reversion is plausible. | FX seasonality and equity intraday-momentum evidence motivate state-dependent hypotheses, not optimized filter thresholds. [NBER 12413](https://www.nber.org/system/files/working_papers/w12413/w12413.pdf), [Gao et al. author abstract](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2552752). | Retrospective regime labeling and filter proliferation can overfit. Compute state only from the preceding 120 completed bars; do not classify months using their future return. Report regime samples and abstain from claiming coverage when sparse. |

## Predeclared shortlist

Four families: opening-range breakout, failed-breakout fade, rolling-time-average
reversion, and compression/expansion continuation. `backtest/scalp_signals.py`
contains the executable rules. All use original EURUSD and GBPUSD data only.

Common rules: one portfolio position; completed-M1 decision; immediately following
M1 open fill with friction; 60-minute maximum hold; no overnight/rollover position;
flatten by 16:50 New York; eight portfolio entries per FX day maximum; 15-minute
detector cooldown. Protective distance is the greater of 5 pips and 1.5 times
prior 20-bar ATR. Main sweep: reward 1.5R or 2R, otherwise identical parameters.
The ATR multiplier's neighbors 1.25/1.5/1.75 and rewards at the frozen reward
minus 0.25, unchanged, and plus 0.25 form a
separate declared sensitivity grid; those runs count as additional trials.
Predeclared robustness rejection: all eight neighbors must have positive normal
and stressed expectancy, with median normal neighbor expectancy at least 50%
of the positive center. This diagnostic cutoff is an explicit judgment call;
it is not an independent statistical test and cannot select a replacement
configuration after the recent holdout is opened.

- Opening range: 08:00–08:15 Europe/London and 08:00–08:15 America/New_York;
  require all 15 contiguous bars, then a closing cross beyond the range before
  two hours after the open; at most one signal per opening range.
- Fade: prior 30-bar high/low breached by a wick and reclaimed by a directional
  close; prior 120-bar directional efficiency below 0.2.
- Reversion: prior 60-close time average and population standard deviation;
  close remains more than two standard deviations displaced but the current
  body turns toward the average; efficiency below 0.2.
- Compression: prior 20-bar range less than 60% of prior 60-bar range, followed
  by a close outside the shorter range with body at least prior ATR20.

Regime labels: high volatility if prior ATR20 exceeds 1.5 times the mean true
range of the prior 120-bar window; otherwise trend if directional efficiency
is at least 0.2, else range. These are causal labels, not an assertion that all
economic market regimes have been captured.

## Frozen execution assumptions

Account currency USD, standard lot 100,000 units; these two USD-quoted pairs
have USD 10/pip/lot. Commission default USD 7/lot round trip. Spread assumptions
in pips: EURUSD 1.0, GBPUSD 1.5; multiply by 1.5 at NY 18:00–03:00 and by 4 at
16:00–18:00; otherwise multiply by 1. Spread doubles inside a supplied relevant
high-impact ±15-minute news window, during which new entries are blocked and
positions exit. Recorded `spread_pips`, if supplied, overrides the spread model.
An unqualified `spread` column cannot be treated as pips without broker point
metadata. Slippage 0.15 pip per side; entry additionally pays 0.15 pip as an
adverse allowance for the assumed three-second end-to-end delay. The allowance
is not a measured three-second quote. Stress multiplies spread, commission,
slippage and the entry latency allowance by 1.5.

There is no historical news calendar in the located export. The harness accepts
one and exercises the existing blackout function, but a run without complete
events must be labeled incomplete and cannot establish that historical news
blackouts were honored. Source clock remains unverified. Explicit clock scenarios
may diagnose sensitivity; they must never be selected for higher profits or
represented as a verified historical conversion. These limitations remain gates
against promotion even under the approved retrospective design.
