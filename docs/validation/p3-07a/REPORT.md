# P3-07A — ResearchEvaluation

## Baseline and scope

Base: `d165c65a17c1ff4b7d174a063a6607589cd38522` (fetched main).
Branch: `feat/p3-07a-research-evaluation`.
Environment: `D:/Environments/factorlab-py313/Scripts/python.exe`.

Engineering decision: **P307A_ENGINEERING_ACCEPTED**.

The 16 deterministic P3-07A tests pass, including direct use of existing execution
results. Related regressions also pass; detailed commands/results are below.

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**.

This evaluation layer never confers financial admission. Both synthetic samples
and real candidate artifacts remain non-formal financial evidence. Dataset and
universe IDs are caller declarations, not independent provenance attestations.

## API and use

```python
from factorlab.evaluation import EvaluationContext, ResearchEvaluation

context = EvaluationContext(
    dataset_id="snapshot-and-feature-version",
    universe_id="scheduled-security-universe",
    label="EXCESS_RET5",
    label_id="label-definition-and-values-version",
    benchmark_id="benchmark-data-version",
    calendar=("2024-01-02", "2024-01-03"),
    holding_days=5,
)
evaluator = ResearchEvaluation(context)
result = evaluator.predictions("ridge", predictions, folds=fold_receipts)
payload = result.to_dict()
```

The calendar is the complete ordered session calendar for the evaluated test
window, not merely the dates with successful scores. Input tables must cover it
exactly. Unavailable predictions and labels remain explicit missing values on
scheduled security/date observations. Session dates must be normalized and
 timezone-naive. Identity, frequency, horizon, annualization, effective annual
risk-free rate, stage and source kind are bound into the context.

Public entry points:

- `predictions`: already-produced scores with explicit fold receipts.
- `walk_forward`: adapts a completed result's public predictions; accepts the
  same explicit receipts, including for legacy runs without full audit metadata.
- `factors`: existing cross-sectional factor diagnostics and temporal tables.
- `portfolio`: normalized full-window NAV plus optional actual trades/positions.
- `benchmark`: benchmark-only portfolio diagnostics; its strategy and benchmark
  return series must coincide.
- `ablation`: existing descriptive receipt-based factorial summaries.
- `compare_results`: strict context/sample comparison, without ranking winners.
- `paired_inference`: opt-in daily paired differences with HAC, block Bootstrap
  and Holm adjustment across every declared non-baseline arm.
- `reprice_costs`: named `FeeSchedule` scenarios applied to actual recorded fills.

`EvaluationResult` separates metrics, tables, receipts and metadata. `MetricValue`
contains `value`, `status` and `reason`; an undefined scalar is
`NOT_COMPUTABLE` with a null value, never a fabricated zero. The version-1
`to_dict()` format is strict-JSON compatible and can be consumed by downstream
reports or trackers without importing report rendering or an experiment runner.
Existing daily/summary tables retain their native schemas and coverage counts.

## Samples, boundaries and comparison

Prediction inputs require date, symbol, fold, score and the configured label.
Fold receipts require unique fold plus train_end, validation_start,
validation_end, test_start, test_end and label_end. End bounds are exclusive;
label_end declares training-label maturity no later than train_end. The order
must satisfy:

```
label_end <= train_end <= validation_start < validation_end <= test_start < test_end
```

Every score date must lie in its own fold's test interval. These are evaluated
receipts, not reconstruction or certification of an upstream training process.

Comparisons require identical actual observation/label/fold and finite-pair
receipts, not just equal lengths. Factor comparisons additionally bind requested
factors, quantiles, minimum cross-section and factor-specific finite-pair masks.
Portfolio comparisons bind the actual benchmark returns and NAV. Benchmark-only
and portfolio results share the portfolio comparison layer; prediction ranking
spreads never become strategy returns. Different labels, calendars, frequencies,
benchmarks or holding horizons require separate comparison groups and cannot
enter the same paired test. Model names and factor-selection/orientation methods
are not hard-coded; already-computed FactorScore, Ridge, LightGBM, XGBoost and
fixed/dynamic or direction-adjusted arms use the same prediction entry point.

## Financial and statistical conventions

All formulas are delegated to the existing metrics implementations. Portfolio
returns compound from NAV origin 1; inputs must reconcile to this origin, including
entry-day fees. Cumulative excess return is the difference between compounded
strategy and benchmark returns, not compounded daily active return. The supplied
periods_per_year applies consistently to annual return, volatility, Sharpe,
Sortino, tracking error and information ratio. It must describe the supplied
return frequency. The risk-free rate is effective annual and is converted by the
existing risk functions. ICIR uses daily mean/sample standard deviation and is
not annualized. No frequency conversion or silent window truncation is performed.

Missing turnover, cost, trade or position receipts are explicit unavailable
metrics even where legacy aggregate APIs default them to zero. Provided daily
costs must reconcile to supplied per-fill costs. Turnover retains the source
engine's NAV convention; it is not inferred from a different denominator.

Statistical units are trading days, never independent security-days or expanding
folds. Daily ICs are calculated cross-sectionally first. Paired differences
preserve all calendar slots and missingness. `RobustnessOptions` enforces HAC lags
and non-circular overlapping moving-block lengths appropriate to holding-period
overlap. Output identifies the two-sided zero-mean paired-difference hypothesis,
window, sample/coverage counts, confidence level, HAC lags, block length, Bootstrap
repetitions and seed, and the full Holm family size. Insufficient coverage,
insufficient dates and degenerate variance remain the existing explicit statuses.
Ablation fold contrasts remain descriptive; no independent-fold significance is
invented. Results do not feed back into selection or fitting.

## Cost scenarios and execution constraints

Pass named schedules such as baseline, higher commission and explicit slippage:

```python
from factorlab.backtest.fees import FeeSchedule
from factorlab.evaluation import reprice_costs

costs = reprice_costs(execution_result.trades, {
    "baseline": FeeSchedule.a_share_default(),
    "higher_commission": FeeSchedule.a_share_default(commission_bps=10),
    "explicit_slippage": FeeSchedule.a_share_default(slippage_bps=20),
})
```

Every fill is repriced using its actual execution date, side and notional,
including date-effective sell taxes and minimum commissions. Fill quantity,
price and notional must reconcile. Slippage is the schedule's explicit fee
component, not a second price haircut. The result is labeled
`FIXED_ACTUAL_FILLS_COST_ONLY` and contains component costs and deltas, not a NAV
or hypothetical performance estimate. Cash feasibility, board lots, rejected
orders and subsequent holdings can change when costs change. Exact scenario
performance therefore requires actual outputs from the existing engine; it
cannot be reconstructed from the fixed ledger alone. Separate actual constrained
execution runs can be evaluated and compared as portfolios with the same declared
context. No order-book liquidity or new execution engine is introduced.

## Compatibility and known limits

Existing model, metrics, statistics, report and backtest APIs are unchanged.
No dependency on P3-04, P3-D01 or PortfolioOptimizer is introduced. There is no
P3-07B orchestration, automatic model selection, admission workflow, filesystem
artifact lifecycle or report-template modification. Legacy artifacts need explicit
caller-supplied context and fold receipts; table attrs alone are not provenance.
Results contain mutable DataFrames and are not signed or immutable audit records.
Input identities are not proof of PIT/HFQ correctness. A result serialization is
an export, not a new financial-evidence authority. Paired inference currently
supports prediction daily IC/RankIC/spread and portfolio daily strategy returns;
factor and ablation tables retain descriptive statistics.

## Verification

Deterministic acceptance tests exercise metric equivalence, strict comparison
identities, boundaries, undefined values, evidence export, actual-fill repricing,
benchmark pairing, ablation recomputation and reproducible paired inference.
Targeted regression execution covers the reused prediction, portfolio,
robustness, ablation and rule-aware execution contracts. Large P2-09/P3-03
performance suites are outside this verification scope.

| Directed run | Result | Receipt |
| --- | --- | --- |
| Evaluation + prediction/portfolio/robustness/ablation/execution regressions | 129 passed, 56.92s | `targeted-regression.txt` |
| Final evaluation + factor golden/reference + factor report regressions | 356 passed, 75 warnings, 11.65s | `factor-integration.txt` |

The final run includes all 16 new evaluation tests. The 75 warnings originate in
unchanged factor/reference empty-slice and pandas categorical-grouping behavior;
the warnings and exact locations are recorded in the second receipt. The two
run totals overlap on evaluation tests and must not be added as a unique count.

The actual-execution integration test generates deterministic bars, runs the
existing rule-aware backtester, feeds its NAV/trades/positions to the evaluator,
and reprices the original fee schedule to zero cost delta. Synthetic execution
is software evidence only. Existing statistical regression tests independently
exercise HAC, moving-block Bootstrap and missing-family Holm behavior; the new
coordinator tests additionally recompute their paired inputs and outputs.
