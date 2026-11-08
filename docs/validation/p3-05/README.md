# P3-05 numerical acceptance

Baseline: `d165c65a17c1ff4b7d174a063a6607589cd38522` (latest origin/main at start;
contains known baseline `fe2b250e03b7004fe795efd12e6d0076d7bc8f19`).

Python: existing `D:/Environments/factorlab-py313/Scripts/python.exe`.

From repository root:

```sh
D:/Environments/factorlab-py313/Scripts/python.exe -m pytest tests/research/test_portfolio_optimizer.py tests/research/test_portfolio_backtest_phase2.py -q
```

Final result: **44 passed in 1.14s**, including optimizer mathematics and existing
Portfolio/backtest compatibility tests. Full focused execution output: `pytest.txt`.
Engineering result: **P305_ENGINEERING_ACCEPTED**.

Analytical oracles:
- Diagonal covariance `(1,4)`: min-variance weights `(0.8,0.2)`, variance 0.8.
- Budget 0.5: weights `(0.4,0.1)`, cash 0.5.
- Expected returns `(0,1)`: mean-variance first weight `0.8 - 0.2/lambda`;
  lambda 1,2,10 gives 0.6,0.7,0.78.
- Cap 0.6: constrained min-variance `(0.6,0.4)`.
- Prior `(0.5,0.5)`, turnover cap 0.2 and tech deviation 0.1 about 0.5:
  target `(0.6,0.4)`, turnover 0.2.
- Sample returns A `(0,.02,.04)`, B `(.01,.03,.05)`, full diagonal shrinkage:
  equal portfolio variance 0.0002.

Tests also cover singular/zero covariance, invalid covariance and missing data,
future input rejection, exact baseline constraint failures, liquidation turnover,
input ordering and identities, risk-aversion and cap sensitivity, zero investment,
single-security portfolios, locked prior holdings and solver iteration failure.

API and mathematical conventions: [portfolio_optimizer.md](../../portfolio_optimizer.md).
The output identities and full input/configuration parameters are returned with every
successful solve; `numerical.json` preserves one small reproducible result.

Scope: single-period continuous long-only allocations, no calibrated forecast model,
no multi-security execution or transaction-cost optimization. Numerical checks do not
establish point-in-time data provenance or HFQ correctness.

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED
