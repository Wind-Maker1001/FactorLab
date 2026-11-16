# MC-02: conditional Monte Carlo tail reliability

NO LIVE MONEY. Financial admission remains blocked.

## Estimands and uncertainty

This study uses independent simulated paths conditional on one historical calibration. Loss is `1 - terminal NAV / initial NAV`, including negative losses for gains. VaR retains the engine's linear quantile; CVaR retains its fractional worst `(1-confidence)*n` mean. Maximum drawdown includes initial NAV; loss and drawdown breaches use strict `>` comparisons. Engine portfolios rebalance without costs and are not dynamic Grid accounts.

Intervals are pointwise 95%, not simultaneous across metrics or experiment cells:

- Wilson score intervals target independent path event probabilities.
- Binomial order-statistic intervals target population loss/drawdown quantiles. If `B ~ Binomial(n,q)`, the chosen ranks `l,u` satisfy `P(l <= B <= u-1) >= .95`. Rank zero and n+1 are unbounded endpoints, encoded as null with explicit flags. Ties are conservative; the point estimate remains linearly interpolated.
- CVaR percentile intervals use 999 iid resamples of complete path losses and the same fractional-tail estimator. This empirical approximation cannot discover unobserved extremes and is not a distribution-free coverage guarantee. The bootstrap RNG is separate from simulation RNG.

Path time points are dependent and are not independent trials. Independent simulation seeds supply repeated experiments; their observed ranges and sample SDs are descriptive, not market prediction intervals. Reusing seeds across sizes or models does not establish independent contrasts or nested samples: the engine RNG contract depends on actual batch plan.

At 16 paths, 95% tail mass is 0.8 and 99% mass is 0.16. Both CVaRs equal the observed maximum loss. Adding paths can improve conditional tail precision, but no finite sample establishes correct market dynamics. Expected tail mass, actual positive-weight count, largest-path weight/contribution and degeneracy flags accompany estimates. The fewer-than-20-tail-path flag is a screening diagnostic, not a universal sufficiency threshold.

## Frozen bounded design

CPU only; seeds 1729, 2718, 31415, 65537, 104729; 168 hourly steps; batch size 256; confidence levels .95/.99. Diagnostic size 16; primary candidates 100, 1,000, 10,000. Primary calibration uses the last 1,440 hourly returns, GBM and 24-hour moving-block bootstrap. One-axis sensitivities use 720/all available returns and 6-/72-hour blocks. GBM is recalibrated per window.

Non-wrapping blocks retain dependence within each selected block and synchronous asset rows, but sever dependencies at block boundaries. Six hours emphasizes short dependence; 24 hours retains a daily span; 72 hours retains multi-day spans with fewer historical starts and greater local-regime persistence. None is an optimized selection. All require approximate local stationarity; longer blocks do not automatically improve prediction.

The 600-second cooperative wall budget includes loading, statistics and representative Grid execution. Complete seed groups are attempted in deterministic order; measured cost gates scale upgrades, with separate time reserved for sensitivity and Grid. Completed adverse cells are never removed. Skipped or partial groups remain explicit; in-flight calls can overrun. Only compact statistics/provenance persist. Grid probes use the first two paths per model from the first seed, not selected tails, with bounded temporary full histories; they establish interface and ledger behavior, not Grid tail convergence.

## Historical evidence and interpretation

The certified NVDAUSDT snapshot contains 2,160 hourly bars in May–August 2026, acquired November 13. Availability remains `max(observation close, acquisition)`. It supplies no realized post-availability evaluation horizon, so strict rolling PIT coverage is `INSUFFICIENT_DATA`; a retrospective historical split would not repair this. No synthetic prices count as observed OOS data.

`ENGINEERING_VALID` describes contract and representative ledger checks. `STATISTICALLY_UNSTABLE` flags low tail mass, unavailable finite quantile bounds or empirical degeneracy; absence of a flag does not certify precision. `INSUFFICIENT_DATA` identifies missing historical evidence. `FINANCIAL_NOT_ESTABLISHED` remains independent of numerical convergence. A universal minimum n cannot be selected without metric-specific precision tolerances and model-validation evidence.

## Observed result

The certified-input run completed all 70 frozen cells in 76.817 seconds, with no budget skips. At 10,000 paths, primary CVaR95 seed SDs were about 0.096 pp (GBM) and 0.103 pp (MBB), versus 2.922 and 1.941 pp at 16 paths. Average conditional CVaR95 interval widths fell to 0.470 and 0.522 pp. This is conditional numerical stabilization, not financial validation: using all 2,159 calibration returns instead of 1,440 shifted CVaR95 means by about 1.610 and 1.539 pp. Rare 20%-drawdown breaches remained too sparse for precise estimation even at 10,000 paths.

Across 6-/24-/72-hour blocks, MBB CVaR95 means were similar on this input (9.842–9.862%), but loss probabilities spanned 42.288–44.432%. This does not validate one universally appropriate dependence horizon. Four fixed-index Grid accounts reconciled exactly; three had no fills, so they establish no Grid risk precision. The study provides no universal minimum path count; 10,000 is a tested research baseline whose adequacy depends on the requested metric and precision tolerance.

Complete execution evidence, statistical interval meanings, all seed results and reproduction commands are maintained in [MC-02 report](../validation/mc-02/REPORT.md).
