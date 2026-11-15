# Monte Carlo risk scenarios

`factorlab.research.monte_carlo` exports `MonteCarloConfig`, `MonteCarloInput`,
`MonteCarloResult`, and `MonteCarloRunner`. These are research risk scenarios, not
historical backtests, predictions, or financial admission evidence.

```python
from factorlab.compute import ComputeConfig
from factorlab.research.monte_carlo import (
    MonteCarloConfig, MonteCarloInput, MonteCarloRunner,
)

runner = MonteCarloRunner(MonteCarloConfig(
    n_paths=10_000, n_steps=252, seed=42,
    compute=ComputeConfig(device="cpu"),
))
result = runner.run(MonteCarloInput(
    assets=("asset_a", "asset_b"), initial_prices=[100., 50.],
    weights=[.6, .3], drift=[.07, .04], volatility=[.2, .15],
    correlation=[[1., .3], [.3, 1.]],
))
print(result.var, result.cvar, result.loss_probability)
```

## Models and inputs

`model="gbm"` uses annualized arithmetic drift and volatility, with `dt=1/252`
by default. Correlation defaults to identity and must be symmetric, unit-diagonal,
and positive semidefinite; singular matrices are supported. The eigendecomposition
symmetrizes only within absolute tolerance `1e-12` and clips only negative
eigenvalues within that tolerance. Any such correction records its method,
minimum eigenvalue, maximum change, and effective matrix in the input receipt.
Other invalid matrices are rejected. The exact step is
`S_next = S * exp((mu - sigma**2/2)*dt + sigma*sqrt(dt)*Z)`, where `Z` has the
specified correlation. Thus asset log-return covariance per step is
`diag(sigma) @ correlation @ diag(sigma) * dt`. Drift is the arithmetic SDE
coefficient, not the mean log return. For bootstrap, each observed return is one
simulation step; `dt` does not rescale historical returns.

`model="moving_block_bootstrap"` accepts `historical_returns` shaped
`(observations, assets)` containing simple returns. Supply strictly increasing
`timestamps`, `available_at` for each observation, and an `as_of` cutoff.
Availability must not precede observation time. All supplied observations and
availability times must be at or before the cutoff; future information is
rejected rather than silently removed. Blocks are sampled uniformly with
replacement from overlapping contiguous windows; all assets use the same row
indices, preserving contemporaneous cross-asset dependence. The last sampled
block is truncated to the requested horizon. `block_size=20` by default.
Use UTC-normalized timestamps; naive timestamps are interpreted on the same
UTC timeline. Initial prices are valuation inputs at the simulation origin,
not historical estimation observations.

Weights are finite, long-only, and sum to at most one. Uninvested weight is cash
with zero return. Every step rebalances to the same weights, without costs.
Starting portfolio value is `initial_value=1`. Asset prices default to one and
asset names default to stringified indices. Shorting, leverage, dynamic signals,
financing, and transaction costs are outside this API.

## Results and conventions

`terminal_values` and `maximum_drawdowns` are host NumPy vectors of length
`n_paths`. Maximum drawdown is the **positive** peak-to-trough fractional loss,
including initial wealth in the running peak. This is the negative of the
repository's negative-return drawdown convention. Quantile dictionaries use
probabilities from `quantiles` as keys and NumPy's linear interpolation.

Loss is `1 - terminal_value / initial_value`. `var` is its `confidence` quantile.
`cvar` is the integrated upper empirical loss tail of mass `1-confidence`, using
a fractional boundary observation when the tail mass is not an integer.
Both may be negative when even the tail is profitable. `loss_probability` counts
strictly negative terminal returns. `drawdown_breach_probability` counts MDD
strictly above `drawdown_threshold` (default `.2`).

`save_full_paths=False` avoids storing time histories. Enable it only for small
experiments: `portfolio_paths` has shape `(n_paths, n_steps+1)` and `asset_paths`
has shape `(n_paths, n_steps+1, n_assets)`, both including initial values. These
histories reside in host RAM, not the GPU budget. Without this option both are
`None`. Random shocks or resampled returns exist only for the current batch.
CPU evaluates that batch with cumulative products and running maxima. CUDA uses
batched CuPy RNG/GEMM/gathers and a CUDA kernel with one thread per path, retaining
wealth, peak, and drawdown in registers while scanning time. No Python loop
iterates through individual paths or timesteps on the GPU execution path.

## Device selection, budgets, and reproducibility

The existing compute V1 resolver selects NumPy or lazy-imported CuPy. CPU never
imports CuPy. Install the optional CUDA 13 runtime with
`pip install 'factorlab[monte-carlo-cuda]'` on a compatible NVIDIA driver.
The CUDA probe runs random generation, matrix multiplication, return transforms,
indexing, and reduction on the actual GPU and synchronizes completion.
Auto and explicit GPU fallback obey `ComputeConfig`; execution
allocation failures become `ComputeMemoryError`, never a silent CPU rerun.

`batch_size=None` chooses a maximum of 4096 paths, reduced for available GPU
memory and any configured limit. An explicit batch size is also reduced to fit
the planner. A private CuPy memory pool enforces `gpu_memory_limit_mb` for
allocations routed through it. Context, cuRAND, and cuBLAS allocations outside
that pool are not bounded by this setting. The estimate includes persistent
arrays and horizon-/asset-scaled per-path workspace allowances, including
batch-local shock matrices, sampled indices, NAV storage, allocator rounding,
and intermediate scan/GEMM headroom; native allocation failures remain
possible and are reported rather than retried. Execution metadata records the
actual batch size, private pool limit and observed reserved-byte peak, plus
planning estimates and budget scope. V1's global selection evidence is not
rewritten; task-local enforcement evidence is under `compute.execution`.

The seed and RNG implementation/backend are recorded. Repeatability requires
the same backend, versions, configuration, and effective batch plan; NumPy and
CuPy random streams are not bitwise equivalent. Changing batch size changes the
mapping of random draws to paths. Metadata hashes the normalized numerical
inputs, PIT timestamps and availability, asset names, and initial prices.

## ExperimentTracker

Create an experiment with `model="monte_carlo/" + config.model` and parameters
exactly equal to:

```python
parameters = {
    "monte_carlo": result.metadata["config"],
    "sample_hash": result.metadata["sample_hash"],
}
```

Supply the tracker's remaining required definition fields with their real
meaning: dataset provenance, explicit windows or null, pinned factors if any,
and scenario costs. Reuse this immutable experiment for repeat runs of the same
identity. Call `runner.record(result, tracker, experiment_id, run_id, code=...,
command=..., qualification="SYNTHETIC")` with truthful code hashes (or explicit
nulls), command arguments, and qualification. Record rejects a mismatched model,
configuration, or sample identity. Duplicate run IDs retain the tracker's
immutable-publication behavior.

Only compact summaries and metadata are recorded; path arrays are not embedded.
The evaluation identity is `MONTE_CARLO_RISK_SCENARIO_V1`, not
`HISTORICAL_TEST`. Dataset and code identity remain caller-owned; the adapter
does not manufacture dataset identities or claim historical evaluation status.
`result.to_dict()` reuses the evaluation metric/evidence conventions without
constructing a `HISTORICAL_TEST` context. It excludes all sample/path vectors.
GBM parameters, initial prices, portfolio assumptions and risk definitions are
retained in compact metadata; historical data are identified by content hash,
sample interval and cutoff and must remain available in the caller's data store.

## Reproduction and measured execution

Run the small example with the installed package:

```sh
python examples/monte_carlo.py --device cpu
python examples/monte_carlo.py --device gpu --store results/mc-store
python -m pytest tests/research/test_monte_carlo.py tests/research/test_compute.py tests/research/test_experiment_tracker.py -q
python tools/benchmark_monte_carlo.py --output results/mc-benchmark.json
# One bounded case only:
python tools/benchmark_monte_carlo.py --case bootstrap_medium --output results/mc-bootstrap.json
```

Measured on 2026-11-15, Windows 11, NVIDIA GeForce RTX 2070 8 GiB, Python 3.14.0,
NumPy 2.3.5, CuPy 14.2.0, loaded CUDA runtime 13.2, driver API 13.4.
The isolated Python environment installs CUDA dependencies only as optional pip
packages; the runtime actually loaded is recorded independently of package names.
Times below measure complete `run()` calls after bounded model-specific warmup,
including probes, input validation/hashing, transfers and exact host quantiles.
All cases use three assets, float64 and a 512 MiB private-pool budget.

| Model | Paths × steps | Batch | CPU seconds | CUDA seconds | CPU/CUDA | Pool peak MiB |
|---|---:|---:|---:|---:|---:|---:|
| GBM | 2,000 × 24 | 512 | 0.00344 | 0.00326 | 1.05× | 0.57 |
| GBM | 20,000 × 252 | 4,096 | 0.38164 | 0.07055 | 5.41× | 47.26 |
| GBM | 100,000 × 252 | 4,096 | 2.45636 | 0.33283 | 7.38× | 47.26 |
| Moving block bootstrap | 20,000 × 252 | 4,096 | 0.19126 | 0.03081 | 6.21× | 40.67 |

The large case completed in 25 batches with no stored histories. The pool peaks
are observed reserved bytes, **not total process/device VRAM peaks**. Native
context/library allocations and other applications are outside that measurement
and cap. Budget tests also exercised 1 MiB batch reduction and rejection when
persistent inputs alone could not fit. V1's global enforcement flag stays false.

Numerical comparisons use independently generated CPU/CUDA streams. Thresholds
were fixed before measurement: terminal-mean difference at most six combined
standard errors, and two-sample KS distance for both terminal NAV and MDD at most
`sqrt(log(2/1e-5)/n)` for equal sample sizes. All four cases passed. In the large
GBM case the mean difference was 0.0006572 versus a 0.0037157 tolerance; terminal
KS was 0.00340 and MDD KS 0.00289 versus 0.01105. CPU/CUDA VaR values were
0.16449/0.16339 and CVaR 0.20825/0.20830, respectively. These thresholds concern
sampling error, not model adequacy or financial qualification.

First-use compilation can dominate small runs: the first Bootstrap measurement
before model-specific warmup took 0.66517 seconds on CUDA versus 0.22836 on CPU.
The benchmark explicitly warms both implemented models; warmup is excluded from
the table, not silently represented as zero startup cost.

The directed suite completed with **100 passed**, including real CUDA execution,
deterministic repeats, analytical GBM moments, cross-asset correlation, PIT and
block-order rejection cases, NAV/MDD conventions, fractional CVaR boundaries,
CPU-only imports, unavailable-GPU fallback, budget-driven batches and injected
native OOM propagation without rerunning on CPU. Both example devices also
completed and wrote compact immutable tracker records.

Execution labels: `ENGINEERING_ACCEPTED`, `GPU_RUNTIME_VERIFIED`,
`NUMERICAL_VALIDATION_PASS`, `PERFORMANCE_VERIFIED`. These describe software and
measured research execution only, not admission to financial backtesting.
