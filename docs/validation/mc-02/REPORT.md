# MC-02: tail reliability evidence

NO LIVE MONEY. Financial admission remains blocked.

## What the experiment establishes

Increasing independent simulated paths materially reduces conditional tail-estimation noise on this input. It does not remove historical-window sensitivity, establish market-model correctness, or validate the 16-path Grid account tail conclusions.

The frozen CPU design completed in **76.817 seconds** of its 600-second cooperative budget: 10 diagnostic, 30 primary and 30 one-axis sensitivity cells, each with all five prespecified seeds; **zero cells skipped**. Statistical simulation/interval cells took 14.964 seconds in aggregate and four representative Paper accounts took 60.410 seconds. No GPU matrix, parameter search or outcome-based seed selection was used.

- `ENGINEERING_VALID`: mathematical references, deterministic replay, recording contracts and representative Paper ledger/interface checks passed.
- `STATISTICALLY_UNSTABLE`: 16-path tails; sparse 99% tails at 100/1,000 paths; rare 20%-drawdown breach estimates even at 10,000. The flags are diagnostics, not a calibrated acceptance test.
- `INSUFFICIENT_DATA`: strict historical rolling PIT risk coverage cannot be measured with this snapshot.
- `FINANCIAL_NOT_ESTABLISHED`: applies to the entire study, including numerically more precise cells.

## Input, estimands and design

The input is the certified Binance USD-M NVDAUSDT hourly snapshot, data version `352d960fad24e50f1423ff173d838a994a29603d2629652f46c7edc76e5e2a83`, bars identity `16aa66258880efdace966880f59691a29cb74f4f87428240a5ef247ad044a2ab`. There are 2,160 bars and 2,159 returns; return observation closes span 2026-05-14 02:00 through 2026-08-12 00:00 UTC. Acquisition was 2026-11-13 17:12:50.891167 UTC. Calibration as-of is 2026-11-14 00:00 UTC; synthetic scenario start is 01:00 UTC.

All risk numbers below are fractions of initial NAV, displayed as percentages; dispersion and interval widths are percentage points (pp). Loss includes negative values for gains. VaR is the engine's linear quantile; CVaR is its fractional worst-tail mean. Maximum drawdown includes initial NAV; breaches are strictly above 20%. Engine results are costless fixed-weight reference portfolios, not dynamic Grid account risks.

The five seeds are 1729, 2718, 31415, 65537 and 104729. Every horizon has 168 hourly steps, dt=1, CPU batch size 256. Primary calibration uses the trailing 1,440 returns; primary Bootstrap block length is 24 hours. Sensitivities change one axis at a time at 10,000 paths: windows 720 and 2,159, or blocks 6 and 72 hours. GBM parameters are reestimated independently per window. Python 3.14.0 / NumPy 2.3.4 were used; engine metadata records actual RNG/backend/batch identities.

The 16-path diagnostic is a new fixed-seed experiment, **not a reproduction or replacement of R10's published values**. R10 artifacts remain untouched.

## Primary convergence results

Entries are five-seed means and sample standard deviations. These SDs and the recorded min/max ranges are repeatability descriptions, not confidence bounds for market predictions; five repeats give only a coarse view of repeat variability.

| Model | Paths | VaR95 mean | VaR95 seed SD, pp | CVaR95 mean | CVaR95 seed SD, pp | CVaR99 mean | CVaR99 seed SD, pp |
|---|---:|---:|---:|---:|---:|---:|---:|
| GBM | 16 | 7.211% | 1.141 | 9.030% | 2.922 | 9.030% | 2.922 |
| GBM | 100 | 6.993% | 0.378 | 10.610% | 1.254 | 14.927% | 3.071 |
| GBM | 1,000 | 7.500% | 0.392 | 9.574% | 0.358 | 12.398% | 0.388 |
| GBM | 10,000 | 7.899% | 0.020 | 9.939% | 0.096 | 12.864% | 0.294 |
| MBB, 24h | 16 | 6.061% | 1.597 | 8.705% | 1.941 | 8.705% | 1.941 |
| MBB, 24h | 100 | 7.167% | 0.922 | 9.545% | 0.767 | 11.359% | 1.479 |
| MBB, 24h | 1,000 | 7.537% | 0.362 | 9.672% | 0.436 | 12.720% | 0.445 |
| MBB, 24h | 10,000 | 7.564% | 0.111 | 9.842% | 0.103 | 13.120% | 0.126 |

The large-sample improvement is real in this conditional experiment, but not monotonic for every small set of repeats: GBM VaR95 SD increased slightly between 100 and 1,000, for example. Same-seed comparisons across sizes share RNG structure and are not treated as independent difference tests or guaranteed nested samples.

At 10,000, the CVaR95 seed ranges are **9.773–10.017%** (GBM) and **9.675–9.952%** (MBB). CVaR99 ranges are **12.593–13.352%** and **12.967–13.309%**, respectively. All seeds, including unfavorable extremes, are in the raw results.

### Conditional intervals and tail mass

Intervals are pointwise 95%, conditional on the calibrated model/history. Quantile intervals invert binomial ranks; probability intervals are Wilson score; CVaR uses 999 independent path-loss bootstrap resamples with a separate RNG. They do not cover model error, historical parameter uncertainty or joint multiplicity across this table.

| Paths | Expected tail paths at 95% / 99% | GBM average CVaR95 / CVaR99 interval width, pp | MBB average CVaR95 / CVaR99 width, pp |
|---:|---:|---:|---:|
| 16 | 0.8 / 0.16 | 3.827 / 3.827 | 6.662 / 6.662 |
| 100 | 5 / 1 | 6.394 / 6.382 | 3.876 / 2.892 |
| 1,000 | 50 / 10 | 1.468 / 2.419 | 1.492 / 2.744 |
| 10,000 | 500 / 100 | 0.470 / 0.844 | 0.522 / 1.014 |

At 16, both CVaRs equal the maximum loss and both upper quantile bounds are unbounded. The apparently narrower GBM bootstrap width at 16 than at 100 is **not** evidence that 16 is better: resampling cannot discover the unobserved tail beyond the empirical maximum. At 100 the 99% tail has only one observation of nominal mass; at 1,000 only ten. At 10,000 there are 100 nominal 99% tail observations, but CVaR99 still has wider intervals than CVaR95. Positive-weight counts in JSON follow exact engine floating-point arithmetic: an effectively integral tail mass may assign an epsilon-sized extra boundary weight, without increasing its meaningful tail information.

For the prespecified first seed (1729) at 10,000 paths:

| Model | VaR95 estimate [rank interval] | CVaR95 estimate [bootstrap interval] | CVaR99 estimate [bootstrap interval] |
|---|---:|---:|---:|
| GBM | 7.877% [7.682, 8.102]% | 9.977% [9.721, 10.191]% | 12.772% [12.340, 13.196]% |
| MBB, 24h | 7.545% [7.340, 7.720]% | 9.869% [9.599, 10.137]% | 13.309% [12.793, 13.813]% |

This seed is illustrative because it was first in the frozen list, not because its interval was favorable. Other seeds' intervals remain in results.json.

## Drawdown and event probabilities

At 10,000 paths under the primary window, five-seed mean maximum-drawdown quantiles (median / 95th / 99th) are **5.333% / 10.510% / 13.245%** for GBM and **4.589% / 10.244% / 13.364%** for MBB. Loss probabilities average **44.996%** and **42.968%**. For seed 1729, Wilson intervals are **44.376–46.327%** and **42.003–43.943%**, respectively.

The 20% drawdown breach counts per 10,000 paths are **0, 0, 0, 0, 1** for GBM and **3, 0, 1, 1, 0** for MBB. These rare-event estimates remain STATISTICALLY_UNSTABLE. With zero events, a 95% Wilson upper bound is still **0.03840%**, not zero risk; for MBB seed 1729, 3 events yield an interval **0.01020–0.08817%**. Increasing path count helped common quantiles, but did not produce enough events to estimate this very low breach probability precisely. No seed pooling or historical-coverage inference is used to hide that sparsity.

## Dependence and calibration sensitivity

| MBB block, hours (1,440-return window) | CVaR95 mean | CVaR99 mean | Loss probability mean | Drawdown95 mean |
|---:|---:|---:|---:|---:|
| 6 | 9.862% | 13.088% | 44.432% | 10.295% |
| 24 | 9.842% | 13.120% | 42.968% | 10.244% |
| 72 | 9.857% | 12.906% | 42.288% | 10.258% |

CVaR95 is relatively insensitive to these particular block choices, while loss probability shifts **2.144 pp** between 6 and 72 hours. CVaR99 changes by about 0.214 pp from 24 to 72 hours; with only five repeats and shared seed structure, no independent significance claim is made. Small sensitivity on one input is not a general block-length validation.

Blocks preserve observed dependence within 6-hour short spans, daily 24-hour spans or multi-day 72-hour spans, but truncate it at sampled block boundaries. Assets use synchronized rows; independent paths independently select blocks conditional on the same finite history. Longer blocks have fewer admissible starts and preserve longer local regimes. All three assume the relevant historical block distribution is approximately stationary; none preserves dependence beyond arbitrary block boundaries or supplies new market regimes.

| Calibration returns | GBM CVaR95 / CVaR99 | MBB 24h CVaR95 / CVaR99 | GBM / MBB loss probability |
|---:|---:|---:|---:|
| 720 | 10.255% / 13.283% | 11.007% / 14.817% | 44.566% / 39.140% |
| 1,440 | 9.939% / 12.864% | 9.842% / 13.120% | 44.996% / 42.968% |
| 2,159 | 11.549% / 14.605% | 11.380% / 14.753% | 52.920% / 52.698% |

Window changes shift CVaR95 by up to **1.610 pp** for GBM and **1.539 pp** for MBB versus the primary calibration—larger than their approximately 0.10 pp primary cross-seed SDs. Loss probability is substantially more window-sensitive. Narrow conditional simulation intervals cannot resolve this historical calibration/model uncertainty.

## Independence, PIT and representative Grid checks

The statistical unit is an entire simulated path. Within-path hourly observations and historical overlapping blocks are not counted as independent trials. Conditional path resampling is appropriate for engine RNG-generated independent paths; it does not infer a larger effective historical sample. Distinct fixed seeds provide nominal independent pseudo-random repeats, while cross-configuration comparisons can remain dependent. No interval here establishes real-market coverage.

Observation time and research availability differ. The snapshot was acquired after every observation it contains, and availability remains `max(close, acquisition)`. No realized post-acquisition evaluation period is supplied. Consequently **strict rolling OOS coverage is INSUFFICIENT_DATA**, and observed coverage is null. No retrospective split, backdated availability, synthetic observation or fabricated return substitutes for that missing evidence.

Four fixed-index representative Grid accounts were evaluated: first two paths from seed 1729 for each model, with the existing geometric Grid configuration, 4 bps fee, 6 bps slippage and synthetic capacity 0.10. Every FIFO reconciliation residual is zero. GBM path 1 has one fill, terminal NAV **998.7978587084**, inventory 0.10 and maximum drawdown **0.1664935%**; the other three accounts have zero fills and terminal NAV 1,000. Their compact metrics exercise the statistics interface and ledger; **zero fills are not demonstrated safety**, and four accounts cannot establish Grid tail convergence. Funding and exchange execution remain unestablished.

## Sample-size recommendation

There is **no defensible universal minimum** from this study: business precision tolerances were not specified, tail levels differ, rare events remain sparse and calibration sensitivity dominates some conditional errors.

For this 168-hour input, 10,000 paths is a useful **tested research baseline**, not a pass criterion: 95% CVaR bootstrap widths average about 0.47–0.52 pp, compared with 1.47–1.49 pp at 1,000. The 99% widths at 10,000 remain about 0.84–1.01 pp. A metric-specific tolerance could accept or reject those widths. If probability p is tiny, expected event count is n*p; 10,000 paths at p=0.0001 provide only one expected event. Neither 16 nor a blanket recommendation of 10,000 resolves all tail questions.

## Mathematical and engineering validation

All **85 targeted tests passed across two invocations**. The first invocation produced 84 passed and one existing subprocess import failure because pytest's source path was not inherited. Rerunning that CPU-only subprocess test with explicit PYTHONPATH passed (1 passed in 6.17 seconds); no model code was changed to address environment resolution.

The added tests compare Wilson bounds with SciPy's independent score-interval implementation; verify rank coverage and tail error by direct binomial probability sums; compare Gaussian quantile/CVaR with analytic formulas and independent quadrature; compare CVaR resampling to a scalar hand-weighted reference; exercise ties, fractional boundaries, degenerate tails, strict event inequalities, deterministic seed replay, PIT calibration, scale/seed recording, budget skips, compact output and CLI parsing. Existing engine and Grid tests cover the preserved simulation/RNG and execution contracts.

## Reproduction and artifacts

From the repository root in Git Bash, with the supported environment:

```bash
export PYTHONPATH="$PWD/src"
D:/Environments/factorlab-py314/Scripts/python.exe -m pytest \
  tests/research/test_mc_tail_reliability.py \
  tests/research/test_monte_carlo.py \
  tests/research/test_grid_monte_carlo.py -q

D:/Environments/factorlab-py314/Scripts/python.exe tools/research/run_mc_tail_reliability.py \
  --snapshot D:/Code/p7-r04b-official-payloads/certified/runs/bde00b2f948842e2b8c2a00465a9f8c6 \
  --as-of 2026-11-14T00:00:00Z --start 2026-11-14T01:00:00Z \
  --output results/mc-02-reproduction --budget-seconds 600
```

The snapshot argument may point to another location containing the same certified manifest and bars. Market-data bodies are not redistributed in Git. Use a fresh output directory. Timing-dependent admission of cells can differ on slower hosts; all admitted cells use frozen seeds/configurations. Replay estimates require the same backend, software versions and actual batch plan; elapsed times and Git provenance naturally differ.

- [run/definition.json](run/definition.json): frozen design, input identity, code-state fingerprint and command.
- [run/results.json](run/results.json): all 70 cells, intervals, metadata, complete seed ranges/SDs, Grid metrics and OOS status (about 710 KB).
- [run/statistics.csv](run/statistics.csv): 140 compact tail-level rows (about 33 KB).

No full simulated asset histories are retained. The independent module leaves R10 evidence, GBM/MBB mathematics, RNG contracts and financial admission unchanged.
