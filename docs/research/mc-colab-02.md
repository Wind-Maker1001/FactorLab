# MC-COLAB-02 — heterogeneous Monte Carlo protocol

## Scope and status

Study status: final validation passed 119 related regression tests, including explicit CUDA hardware tests. Local five-seed reliability, million-path/batch, multi-asset, 168/8760-step, crossover, profiling, two-process CPU and isolated 10M/100M GPU capacity studies completed without skipped cases. Actual CPU is i7-14700KF and GPU is RTX 2070 8GB. [Local report and evidence](../validation/mc-colab-02/REPORT.md) are published; cloud cost evidence awaits actual allocation and CU observations. This study does not modify execution models or MC-02, MC-03 and MC-05 definitions/evidence. MC-COLAB-01 integration is optional; the command-line entrypoint also runs directly in Colab. No tracked executable Notebook exists in the baseline; a Colab recipe uses the same entrypoint instead of a second implementation.

The engine is float64, long-only, fixed-weight, rebalanced each step, with zero cash yield and costs. Performance claims apply to that simulation, not transaction execution. Loss is `1-terminal/initial`; VaR uses linear quantiles; CVaR uses a fractional boundary observation. Drawdown includes initial wealth; breach uses strict `>`.

## Preregistered design

Version: MC-COLAB-02-v1. Synthetic input seed is 1729, independent of path seeds. Formal path seeds are 104729, 130363, 155921, 181081 and 205019. Repeated timing observations at one seed do not increase the independent seed count. CPU MT19937 and GPU XORWOW are independent experiments even when numeric seed labels match.

Candidate dimensions: paths 100000/1000000/10000000/100000000; assets 1/8/32; steps 168/252/8760; GBM and moving-block bootstrap. Baseline GBM drift .05, volatility .2, correlation .3 off diagonal, equal asset weights, dt=1/252. Bootstrap has 4096 aligned synthetic daily observations with the same covariance; block length 20. These are controlled workloads, not calibrated market forecasts.

1. Pilot: one asset, 252 steps, both models, 100000 and 1000000 paths, batches 1024 and 4096; GPU budgets 1024 and 4096 MiB. Start with the smallest cell. CPU BLAS threads=1. Measure cold execution separately from warm repeats.
2. Reliability: 100000 paths, 252 steps, both models and 1/8 assets, all five seeds, at least two warm timing repeats. Path bootstrap uses 199 resamples by default; explicitly report its resampling resolution. Do not pool timing repeats as independent statistical evidence.
3. Scale: expand one factor at a time to 10M/100M paths, 32 assets and 8760 steps. Select combinations only after observed cost fits the experiment time and RAM budgets. Large simulations need not run expensive path bootstrap: report exact empirical statistics and label uncertainty as across-seed only when five independent repetitions exist. Do not label that as a path-bootstrap interval.
4. CPU multiprocess throughput: distribute independent complete jobs; record workers, BLAS threads, total paths, wall time, child-inclusive resources. Do not compare multiprocess throughput against a single CPU process without identifying the distinction.

Run admission uses a hard host-memory limit for the experiment's estimated array demand and a wall-clock budget checked between complete jobs. It is not an OS memory cap or preemptive time limit. Completed jobs remain evidence if subsequent work is skipped. GPU allocation failure remains failure; no CPU fallback or automatic smaller-batch retry.

## Correctness and statistical interpretation

Fixed-input deterministic checks use float64 `rtol=1e-10, atol=1e-12`. Test fractional tails, initial-wealth drawdowns, zero-volatility GBM and deterministic bootstrap on CPU and available CUDA. Existing correlated-input CUDA tests remain relevant.

Single-asset GBM with full investment has log terminal return `m=(mu-sigma²/2)*T*dt`, `s=sigma*sqrt(T*dt)`. Analytical loss VaR is `1-exp(m+s*Phi^-1(1-c))`; CVaR is `1-exp(m+s²/2)*Phi(Phi^-1(1-c)-s)/(1-c)`. Loss probability is `Phi(-m/s)`. No lognormal terminal formula is applied to a discretely rebalanced multi-asset portfolio or maximum drawdown.

Report pointwise 95% Wilson probability intervals, binomial-order-statistic VaR/drawdown quantile intervals and independent-path percentile-bootstrap CVaR intervals using the existing reliability module. CPU/GPU comparisons report unpaired seed-level differences and Welch intervals, with Bonferroni family coverage across the reported metric set. Descriptive seed SD/range is not an interval for market outcomes. Analytical comparisons include sampling uncertainty. No business equivalence margin has been supplied: results provide precision/difference evidence, not an invented financial equivalence verdict. Neither overlapping intervals nor failure to reject a difference establishes equivalence.

## Measurement

Record requested and resolved device, actual GPU name/driver/runtime, CPU identity, Python/library versions, input identity, actual batch, budgets and seed. No float32 or changed risk definitions. Define paths/s=`N/t`, path-steps/s=`N*T/t`, asset-path-steps/s=`N*T*A/t`.

A fresh worker process measures cold execution including imports, input preparation and backend initialization; warm repeats share that worker. Runner wall time includes its summary. External reliability analysis is separately timed and included in end-to-end time. Optional synchronized phase profiling perturbs execution and must not supply the headline steady-throughput denominator. Distinguish live sampled RSS/device-memory peaks, private allocator peak and analytical memory estimates; sampling is a lower bound on instantaneous peak.

Do not retain full path histories for scale experiments. If O(N) terminal/drawdown storage exceeds the RAM budget, use disk-backed arrays and exact in-place external storage statistics, including I/O in end-to-end timing. Never average per-batch VaR/CVaR. Whole-sample bootstrap may remain expensive even with disk-backed simulation; request it explicitly rather than silently subsampling.

## Cost and cloud execution

No paid Colab run before pilot budget approval. Obtain actual assigned GPU and observed CU/h, then request the expanded CU allowance. L4/local evidence does not wait for A100/G4. A requested GPU name is not evidence of allocation.

Use CU balance before/after and the matching allocation wall interval, covering setup and idle time as appropriate. Distinguish measured CU difference from an hourly-rate estimate. Missing CU remains null, never zero. Report total completed paths and path-steps per CU, including each actually executed repetition, and end-to-end cost including deployment. Local hardware has no Colab CU conversion.

Compare matched workload, batch, seed design and timing scope. Derive crossover only from measured CPU/GPU cells; do not extrapolate a universal boundary. Choose CPU for measured small/cold jobs where it wins, GPU for measured amortized jobs where it wins, and batch/storage by measured resource constraints.

## Reproduction

From a clean checkout of the study revision, use Python 3.13 or 3.14:

```sh
python -m venv .venv
# Windows: .venv/Scripts/python.exe; Linux/Colab: .venv/bin/python
.venv/bin/python -m pip install -e '.[dev,monte-carlo-benchmark,monte-carlo-cuda]'
.venv/bin/python tools/research/run_mc_colab_benchmark.py run --config configs/research/mc_colab_02.json --device cpu --first-seed-only --points-only --output results/mc-colab-02/cpu-pilot
.venv/bin/python tools/research/run_mc_colab_benchmark.py run --config configs/research/mc_colab_02.json --device gpu --first-seed-only --points-only --output results/mc-colab-02/gpu-pilot
```

Use `mc_colab_02_reliability.json` for the four formal five-seed workloads, `mc_colab_02_scale.json` for one-factor extensions, `mc_colab_02_crossover.json` for small-task boundaries, and `mc_colab_02_large.json` for the admitted 10M/100M GPU run. The scale and large capacity runs use `--first-seed-only --points-only`; do not describe them as five-seed reliability runs. The report exporter `tools/research/report_mc_colab_benchmark.py --source results/mc-colab-02 --output <new-directory>` rebuilds the compact evidence tables from all completed local studies.

Run CPU and GPU sequentially. Output directories must be new. Remove `--first-seed-only` for all five seeds and remove `--points-only` for path-level confidence intervals. Use `--profile --points-only` in a separate output directory for detailed synchronized phases. Use `--device cpu --workers 2` for a distinct two-process experiment, not as the single-process baseline.

```sh
.venv/bin/python tools/research/run_mc_colab_benchmark.py compare --cpu results/mc-colab-02/cpu/study.json --gpu results/mc-colab-02/gpu/study.json --output results/mc-colab-02/comparison.json
.venv/bin/python tools/research/run_mc_colab_benchmark.py cost --study results/mc-colab-02/gpu/study.json --cu-before <balance-before> --cu-after <balance-after> --allocation-seconds <whole-window> --setup-seconds <setup-duration> --output results/mc-colab-02/cost.json
```

For Colab, record allocation start/balance and `nvidia-smi` before setup. Clone/check out the exact study revision. If its Notebook interpreter is older than Python 3.13, use `pip install uv` and `uv venv --python 3.13 .venv`; install via `uv pip install --python .venv/bin/python ...`, then use the commands above in shell cells. CUDA 13 requires a compatible driver (Linux >=580); on an older CUDA-12-compatible Colab driver install `.[dev,monte-carlo-benchmark]` plus `cupy-cuda12x[ctk]>=13.6,<14` instead. Install only one CuPy distribution. Record the actual versions; do not compare a driver failure as GPU timing. GPU identity comes from CUDA PCI identity mapped to NVML, not the requested Colab tier. Record setup completion and final balance after all jobs; attach those observations to the cost command.

## Prior observation (not formal acceptance)

User-reported Colab Xeon versus L4, one asset and 252 steps:

| Paths | Xeon seconds | L4 seconds |
|---:|---:|---:|
| 100000 | 1.975 | .045 |
| 1000000 | 19.788 | .396 |
| 10000000 | 186.592 | 4.098 |

At 10M paths the reported CVaR values are CPU .370436 and GPU .370437. RNG seeds, full configuration, phase timing, software environment and CU evidence were not supplied with this observation. Preserve it as provenance, not as a formal paired comparison.
