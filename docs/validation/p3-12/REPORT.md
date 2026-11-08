# P3-12 — Negative controls and statistical robustness

**P312_ENGINEERING_ACCEPTED**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## Design and estimands

The experiment reuses P3-09's 270-session synthetic fixture, 24-security universe, MOM20/REV1 features, five-session next-open label, four small fixed model configurations and expanding walk-forward splits. The original four models are fitted once. Only Ridge receives an additional label-shuffled fit. Validation still selects from the original fixed candidate set; Test never selects parameters or acceptance thresholds.

The protocol is written before training/evaluation. Seeds, control definitions, allocation, costs, primary families and inference settings are fixed in the entry point. Random scores use seeds 17, 29 and 43. Training labels use seed 17 and independent deterministic date-specific permutations. This preserves each Train date's finite-label multiset, missingness, features and maturity timestamps, while destroying that permutation's security/label assignment. The existing runner's partition hook supplies untouched Validation/Test partitions; a global shuffled dataset is not used. Cross-sectional shuffling is a structural negative control, not a guarantee of zero fitted predictive performance.

Four lag controls use scores from exactly five earlier sessions of the complete synthetic calendar. Destination labels and fold identities remain unchanged. A source observation absent from an OOS gap is not forward-filled. Targets remain dated at decision close and the existing engine schedules next-session execution.

All signal arms use the same score-sensitive allocator, 80% investment, 15% maximum weight and five-observation rebalance cadence. Equal allocation uses the complete contemporaneous universe. The fixed-rule baseline allocates 80% equally to the first six symbols in canonical ascending order, independent of outcomes. All portfolios use the same execution bars, status constraints, initial CNY 100,000 and fee contract. Missing/unavailable symbols are not replaced using future observations. The first-six rule targets a subset and therefore has deliberately different concentration from full-universe equal weight.

Random positive profit, a negative contrast or a non-significant result is not an engineering failure. Failure means leakage, incorrect timing or identity, non-reproducibility, incorrect cash/fee arithmetic, incorrect multiplicity or invented statistical availability.

## Sample and identity

The fetched `origin/main` baseline was `72571efeac498515a0765b6fa344ced296307daf`.

- Dataset identity: `d6f607f7761fceca9c6c3e8d16bcb3950f7973ac24940fa004064eef1f721f74`.
- Bars identity: `4dd570ca91e809a0e62df8cccb1b43785e9cf207870938ab18ba78bf6d0d6ff8`.
- Common date/security/fold/label identity: `b44e020e8aa5e72a3c1d981114c34be181ccbafc37d5a17456546a5f44c7eba8`.
- Each original/random/shuffled arm starts with 1,552 rows. Lag availability reduces every evaluated signal arm to the identical 1,192 rows, spanning 50 prediction dates and 24 distinct securities. Cross-sections vary; 1,192 is not treated as the inference sample size.
- Common prediction window: 2023-07-10 through 2023-12-01. Exact dates are retained in `results.json`; the window contains fold gaps.
- Execution starts from the common origin on 2023-07-10; returns run continuously from 2023-07-11 through 2023-12-04, 105 observations. Positions remain marked and exposed during prediction gaps. No post-execution NAV truncation or renormalization is used.

`receipt.json` binds the code files, baseline, protocol, result summary and verification evidence. The experiment's captured checkout provenance identifies its base commit; source-file identities distinguish the added implementation. Reproducible output remains local; no complete model objects or generated trade CSV collection is committed.

## Descriptive out-of-sample results

Returns include the complete continuous execution window and all recorded costs. Fees below are CNY. RankIC is the mean of daily cross-sectional RankIC on the common OOS rows; it is descriptive, not 1,192 independent observations.

| Arm | Mean RankIC | Net return | Transaction costs |
|---|---:|---:|---:|
| FactorScore | 0.09422 | -2.0198% | 1,038.87 |
| Ridge | 0.32457 | +0.0943% | 819.39 |
| LightGBM | 0.31967 | +0.9961% | 810.77 |
| XGBoost | 0.31517 | +1.0520% | 952.23 |
| Train-shuffled Ridge | 0.33838 | +1.1363% | 777.20 |
| Random 17 | -0.02167 | +0.5209% | 1,227.91 |
| Random 29 | -0.00600 | +2.8104% | 1,114.03 |
| Random 43 | -0.01843 | -2.3062% | 956.23 |
| FactorScore lag 5 | -0.05204 | -4.5635% | 990.29 |
| Ridge lag 5 | 0.18152 | -1.1297% | 770.80 |
| LightGBM lag 5 | 0.18030 | -2.0110% | 740.42 |
| XGBoost lag 5 | 0.21914 | -0.6328% | 917.16 |
| Equal | — | -1.0990% | 210.20 |
| Fixed rule | — | -4.4535% | 110.85 |

Equal/fixed portfolios have no model forecast RankIC estimand. Their missing RankIC is not filled with zero.

The shuffled Ridge result is materially positive on this fixture. Thus this small experiment does **not** separate every label-shuffled fit from the original model by performance. Similarly, Random 29 outperforms all original models by realized return despite near-zero mean RankIC. These are reported outcomes, not grounds to change seeds or control definitions. A successful software test is not successful Alpha discovery.

## Inference and multiplicity

The inference unit is a market trading day. Paired contrasts use identical destination samples and windows. Two predeclared, separately corrected families are used:

1. Eleven daily RankIC contrasts: every non-FactorScore signal arm minus FactorScore.
2. Thirteen daily net-return contrasts: every non-equal base-fee portfolio minus equal.

No joint family-wise error guarantee across these two families is claimed. Cost stress is an accounting/sensitivity experiment, not another Alpha-testing family. Intervals are individual, not simultaneous.

Settings: two-sided 95% intervals, alpha 0.05, Bartlett HAC lag 5, non-circular overlapping moving blocks of 10 sessions, 500 bootstrap draws, seed 42, at least 30 valid days and 80% coverage. HAC/block lengths cover the five-session label overlap. Existing inference functions retain null p-values/intervals and explicit statuses when observations are insufficient.

**Prediction inference is unavailable:** 50 valid prediction days occupy 105 actual trading-calendar slots, coverage 47.62%, below the prespecified 80%. All eleven tests report `INSUFFICIENT_COVERAGE`, with null HAC p-values, Holm p-values and confidence limits. Missing fold-gap slots are preserved rather than compressing the time series. An evaluation-only correction reindexed the saved daily metrics to the full calendar; no models were refitted and no thresholds changed.

Portfolio inference uses all 105 daily observations and is available. Differences below are candidate minus equal; interval endpoints are daily-return units, not cumulative return.

| Arm | Mean daily difference | 95% HAC interval | Raw HAC p | Holm p |
|---|---:|---|---:|---:|
| FactorScore | -0.000089 | [-0.000325, 0.000147] | 0.460583 | 1.000000 |
| Ridge | 0.000115 | [-0.000200, 0.000429] | 0.474760 | 1.000000 |
| LightGBM | 0.000200 | [-0.000078, 0.000478] | 0.158312 | 1.000000 |
| XGBoost | 0.000205 | [-0.000084, 0.000494] | 0.163666 | 1.000000 |
| Train-shuffled Ridge | 0.000213 | [-0.000113, 0.000540] | 0.200807 | 1.000000 |
| Random 17 | 0.000155 | [-0.000052, 0.000362] | 0.142352 | 1.000000 |
| Random 29 | 0.000369 | [0.000165, 0.000573] | 0.000385 | 0.004616 |
| Random 43 | -0.000117 | [-0.000249, 0.000016] | 0.084155 | 0.925710 |
| FactorScore lag 5 | -0.000339 | [-0.000496, -0.000182] | 0.000022 | 0.000292 |
| Ridge lag 5 | -0.000003 | [-0.000301, 0.000295] | 0.985440 | 1.000000 |
| LightGBM lag 5 | -0.000088 | [-0.000347, 0.000171] | 0.505687 | 1.000000 |
| XGBoost lag 5 | 0.000045 | [-0.000228, 0.000318] | 0.745910 | 1.000000 |
| Fixed rule | -0.000326 | [-0.000797, 0.000145] | 0.175415 | 1.000000 |

The random arm's adjusted significance is an observed warning against interpreting a selected profitable path as reliable prediction. Holm reduces multiplicity error under its assumptions; it does not establish that a random signal has predictive information. None of the four original portfolios has adjusted evidence of positive mean daily excess return versus equal. Full moving-block Bootstrap percentile limits, valid draws, coverage and status are retained per contrast in `results.json`.

## Cost stress and reconciliation

These are configured research sensitivities, not claims about historical available execution prices or actual broker fees. The existing date-effective default stamp-duty schedule and minimum CNY 5 commission remain in force. Base commission/slippage is 3/5 bps; stress scenarios are 3/20 bps and 15/5 bps respectively. Slippage is the fee engine's additive cost, not an independently shifted execution price.

| Model | Base net return / fee | High slippage net return / fee | High commission net return / fee |
|---|---|---|---|
| FactorScore | -2.0198% / 1,038.87 | -2.6609% / 1,663.99 | -2.0673% / 1,086.40 |
| Ridge | +0.0943% / 819.39 | -0.5025% / 1,344.31 | +0.0439% / 869.78 |
| LightGBM | +0.9961% / 810.77 | +0.5209% / 1,285.95 | +0.9659% / 840.99 |
| XGBoost | +1.0520% / 952.23 | +0.4755% / 1,528.72 | +0.9924% / 1,011.83 |

Each scenario reruns the existing execution engine with identical targets. High-slippage Ridge changes from 113 to 115 trades, demonstrating that changed cash feasibility can alter fills. Consequently, actual NAV differences must not be equated mechanically with fixed-fill fee differences.

For every one of the 22 execution arms, each trade fee is recomputed through its schedule, trade costs reconcile to NAV costs, sequential `cash_after` and daily cash reconcile to initial cash plus sell notionals minus buy notionals minus costs. The largest daily-cash reconciliation residual is CNY 1.02e-9. A deterministic adverse test explicitly rejects treating fees as additional income. Fixed-fill repricing separately confirms nonnegative incremental stress costs; it is labelled cost-only and is not presented as scenario NAV.

## Engineering evidence and interpretation

Deterministic tests exercise training-only permutation and Test-label perturbation isolation, finite-mask/sample identity, full-calendar lag across absent OOS sessions, fixed seeds, preserved missing-value reasons, gap-preserving inference, hand-calculated Holm including unavailable tests, insufficient dates/coverage, an artificial strong daily contrast, and fee-income rejection. The targeted suite, including existing evaluation/robustness/tracker contracts, passed **107 tests in 8.50 seconds**. The live experiment creates 22 distinct successful synthetic tracker records, one for every portfolio/control/cost arm; immutable definitions bind each arm, protocol and fee schedule.

The inference adapter reuses existing comparability and paired inference functions. The training adapter depends narrowly on the runner's private `_fold_frames` hook; its partition behavior is tested. Core pipeline orchestration, optimizer, execution modules, registry and financial-admission policy are unchanged.

Statistical limitations are substantive: one small synthetic realization; three random-score seeds are not a calibrated randomization test; only Ridge receives a shuffled-training fit; cross-sectional permutations preserve within-date marginal distributions and do not remove every possible learnable structure; dependent overlapping labels; short/nonstationary execution history; asymptotic normal HAC; fixed-lag/block choices; only 500 bootstrap draws; uncertainty conditional on observed coverage. Synthetic business-day calendars are not verified exchange calendars. Prediction coverage refuses inference rather than pretending fold gaps are adjacent observations.

**Software:** control construction, sample/timing contracts, explicit unavailable inference, multiplicity and cost accounting function correctly in the targeted checks. **Synthetic effects:** the numerical results above include profitable random and shuffled controls. **Real-market predictive ability:** not established by this experiment. **Actual investment deployability:** not upgraded; financial admission remains blocked.

## Reproduction

From the repository root in Git Bash, using a fresh output directory:

```bash
PYTHONPATH=src D:/Environments/factorlab-py313/Scripts/python.exe \
  -m examples.research.alpha_robustness_synthetic --output .p312-reproduction
D:/Environments/factorlab-py313/Scripts/python.exe -m pytest \
  tests/research/test_alpha_robustness.py \
  tests/research/test_research_evaluation.py \
  tests/research/test_model_robustness.py \
  tests/research/test_experiment_tracker.py -q
```

The entry point writes the frozen protocol, common samples, individual evaluations, tracker records and summary locally. The committed `results.json` retains the minimal comparative results and inference receipts, not the full generated ledger collection. One core training/execution experiment was run; the calendar correction and additional cash reconciliation used its saved evaluation tables only.
