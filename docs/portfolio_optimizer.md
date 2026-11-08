# Single-period PortfolioOptimizer

```python
import pandas as pd
from factorlab.portfolio import OptimizationInput, OptimizerConfig, PortfolioOptimizer

covariance = pd.DataFrame([[0.0001, 0], [0, 0.0004]],
                          index=['A', 'B'], columns=['A', 'B'])
result = PortfolioOptimizer(OptimizerConfig(max_weight=0.9)).optimize(
    OptimizationInput(symbols=('A', 'B'), decision_time='2026-01-05',
                      covariance=covariance, covariance_as_of='2026-01-04'))
# target A=0.8, B=0.2; daily variance=0.00008, cash=0
```

## Objectives and units

- `equal`: existing equal-weight allocation multiplied by `investment`.
- `score`: nonnegative scores normalized to `investment`; a zero total is invalid.
- `minimum_variance`: minimize `w.T @ covariance @ w`.
- `mean_variance`: minimize `risk_aversion / 2 * w.T @ covariance @ w - expected_returns.T @ w`.

Only the last objective uses expected returns, supplied separately from scores with
an `expected_return_definition`. All risk estimates and expected arithmetic returns
must share `return_period`. No annualization or score calibration is implicit.
Every objective requires a risk source so every successful result reports risk.

`investment` is an exact NAV budget, with cash `1 - investment`. All positions are
long-only, each bounded by `max_weight`. Equal/score objectives do not project onto
a feasible region: their exact allocation must satisfy every constraint or fail.

## Risk and time contract

Supply either a labeled covariance DataFrame and `covariance_as_of`, or a complete
historical returns DataFrame. Historical returns require at least two observations,
unique strictly increasing DatetimeIndex values and precisely the target symbols.
No missing/nonfinite observations are dropped. Every observation timestamp and the
supplied covariance cutoff must be no later than `decision_time`. Naive timestamps
are interpreted as UTC; aware timestamps are converted to UTC. These are input-time
checks, not a certification of vendor publication times or historical point-in-time
availability. The caller must supply only information available at the decision.

Sample covariance uses `ddof=1`. `shrinkage=a` means `(1-a)*S + a*diag(S)`.
Supplied covariance is checked for symmetry and PSD *before* shrinkage. Symmetry and
negative eigenvalue roundoff tolerance is `1e-10 * max(abs(S))`; within that tolerance
symmetrization and clipping negative eigenvalues to zero are recorded numerical
normalization. Material asymmetry or indefiniteness is rejected. Singular PSD and
zero covariance are legal; no inversion or hidden ridge is used.

## Industries and turnover

`industries` maps every target symbol to an industry. `industry_bounds` maps industry
to `(lower, upper)` NAV fractions. `industry_deviation` maps industry to a symmetric
allowed deviation around the corresponding explicit `benchmark_industries` exposure.
Absolute and relative intervals are intersected. Industries absent from the universe
have zero exposure; a positive required exposure is infeasible. Benchmark exposures
are caller-supplied NAV fractions, not automatically reweighted to the investment budget.

Turnover is `sum(abs(target_security_weight - previous_security_weight))`, excluding
cash. A complete internal switch counts as 2, full-cash entry counts as 1. When a
turnover cap is configured, previous holdings must be supplied explicitly (an empty
Series means cash). Securities present only in previous holdings are liquidated and
count toward turnover. Missing target symbols in previous holdings mean zero.

## Results and failures

`OptimizationResult` exposes weights, cash, variance/volatility, turnover (None when
previous holdings are omitted), sector exposures, constraint checks, solver metadata,
configuration/input SHA-256 identities and reproducible parameters including numerical
library versions. Canonical symbol ordering makes permutations equivalent. Tied optima
need not be mathematically unique, nor are weights guaranteed bitwise identical across
SciPy versions. Result Series and mappings are caller-owned values, not immutable stores.

SciPy HiGHS checks linear feasibility. SLSQP uses analytical gradients and an objective
scaled by a positive constant to retain sensitivity to small covariance magnitudes.
`max_iterations` bounds SLSQP iterations. Final raw weights are checked with absolute
NAV tolerance `tolerance` (default 1e-8); they are not clipped or renormalized. Feasibility
is numerical within that tolerance, not exact rational arithmetic.

`OptimizationError.status` distinguishes `invalid_input`, `infeasible`, and
`solver_failed`. No successful target is returned after failure. This module does not
estimate transaction costs, submit orders, model fills, or integrate with DataHub.

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED
