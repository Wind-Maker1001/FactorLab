# MC-COLAB-02 — performance and reliability evidence

Protocol: [MC-COLAB-02-v1](../../research/mc-colab-02.md). Canonical configuration: [`mc_colab_02.json`](../../../configs/research/mc_colab_02.json).

## Evidence coverage

The local pilot, five-seed reliability (both models, 1/8 assets), million-path/batch sweep, 32-asset, 168/8760-step, small-job crossover, synchronized profiling and two-process CPU experiments have completed without skipped cases. Regression validation passed 118 tests, including explicit CUDA hardware tests and MC-05 frozen-manifest protection. Actual local CPU is i7-14700KF, not the initially requested 14700K. The isolated 10M/100M GPU capacity experiment also completed. Final regression validation passed 119 tests in 33.42s. [Measured tables](evidence/MEASURED.md), [raw studies and configurations](evidence/) and [CPU/GPU comparisons](evidence/comparisons.json) accompany this report. Cloud measurements require allocation and the approved pilot-then-budget workflow. A100/G4 are optional, not acceptance prerequisites for local/L4 evidence. The prior user-reported L4 timings remain separately attributed in the protocol and are not part of formal evidence.

## Implementation measurement scope

The experiment driver invokes the existing float64 `MonteCarloRunner`; the engine's default risk definitions are unchanged. Cold launch-to-first-completion includes subprocess startup, imports, input generation, resource instrumentation and CUDA discovery. Per-run runner wall time starts after resource sampler setup, and therefore must not be described as uninitialized CUDA context latency. Warm timings include the runner's exact risk summary but exclude external confidence-interval analysis, whose time is separately recorded. A whole-job resource sampler includes that analysis. GPU memory measurements cover the whole allocated device; private pool peak is a distinct metric.

Disk-backed mode preserves path-aligned terminal NAV and maximum drawdown arrays. Temporary sorted float64 files support exact global quantiles and fractional tails, with heapsort's constant auxiliary workspace. This bounds explicit algorithm workspace, not OS page-cache RSS or virtual-memory residency. Disk sorting and I/O remain charged to runner wall time. Disk mode is selected by conservative admission estimates; it is not proof of a measured host-memory cap.

## Large-path capacity

RTX 2070 completed 10000000 paths in 4.752690s on the warm repeat (2.104 million paths/s) and 100000000 paths in 49.204884s (2.032 million paths/s), both 252-step single-asset GBM, batch=4096, float64. The 100M warm run additionally required 6.757829s for the external exact metric suite: approximately 55.963s for runner plus that suite. Runner-region sampled RSS peaked at 3.55 GiB; the whole-device used-memory sample peaked at 2.16 GiB across the two 100M runs. Device usage includes other processes and is not a process VRAM allocation bound.

These are one-seed capacity observations with one warm repeat, not five-seed reliability acceptance or a measured CPU speedup at 100M. The 64 GiB host fit these samples in RAM, so this experiment did not need the disk-backed path. Disk mode has deterministic exactness regression coverage, not a beyond-RAM hardware benchmark. Its I/O throughput remains a future capacity measurement if larger outputs exceed the host budget.

An earlier capacity attempt is retained locally but excluded: a regression process was briefly launched during its execution, making resource overlap possible. The published capacity measurements were rerun sequentially before final testing; the exclusion record is bundled with the evidence.

## Statistical interpretation

A formal workload requires at least five distinct seeds per backend. Repeated timings reuse seeds and are not extra independent samples. Pointwise path-level intervals and across-seed Student-t intervals answer different uncertainty questions. CPU/GPU unpaired Welch contrasts use Bonferroni coverage across nine metrics within each workload. No business equivalence margin has been specified, so confidence intervals do not yield a financial equivalence certificate.

## Observed local conclusions

At 100000 paths, 252 steps and one asset, five-seed steady median GBM times were CPU 1.003568s and GPU .094320s (10.64x); bootstrap was .782215s versus .109847s (7.12x). Fresh-process launch-to-first-result medians reversed that choice: GBM CPU 2.467s versus GPU 3.596s, bootstrap 2.297s versus 3.156s. Cold includes instrumentation/imports and warm disk/kernel caches may persist across workers; it is not a clean-machine first-install benchmark.

At 1000000 paths with batch=4096 and a 4096 MiB GPU pool budget, GBM was 17.041418s versus .455560s (37.41x), bootstrap 14.445657s versus .288307s (50.11x). GBM batch=1024 produced GPU .798496s instead, demonstrating the importance of batch choice. These scale cells have one independent seed and two warm repeats, not the reliability study's five-seed design. CPU timing varies across cells and runs; use the recorded distribution rather than treating one speedup as a hardware constant.

The steady small-job sweep put CPU ahead at 100 paths and GPU ahead at 1000 and 10000 paths for both models. This brackets the measured steady crossover between 100 and 1000 paths for this 252-step, one-asset configuration. Single-seed cold launch times in this small-job sweep are noisy and do not establish a cold crossover. The five-seed 100000-path result supports CPU for fresh one-off one-asset jobs; persistent jobs favor GPU. Do not transplant this boundary to 32 assets or 8760 steps.

The GBM 8760-step workload (100000 paths) measured CPU 52.059s versus GPU 3.225s; 32 assets at 252 steps measured 25.738s versus 1.031s. Thus long horizon and asset correlation change both cost and the device tradeoff, rather than simply multiplying the single-asset speedup.

For single-asset GBM, analytical 99% CVaR is .3941772, CPU five-seed mean .3940407 and GPU .3933875. The GPU-minus-CPU difference is -.0006532, with within-workload family-corrected interval [-.0054356, .0041292]. The largest absolute CVaR99 mean difference among the four reliability workloads is .0022265 (single-asset bootstrap), interval [-.0030805, .0075335]. All nine corrected contrast intervals in each workload include zero; this is difference/precision evidence, not an equivalence claim. Full per-seed pointwise intervals, seed SD/ranges, drawdown quantiles and probability contrasts are retained in the raw studies.

## Cost and selection

No measured Colab CU observations have been collected for this study. CU fields remain absent until an observed balance window is supplied. Local throughput cannot be converted to CU/h. Device selection will be stated only for matched measured workload cells, separately for cold versus steady execution; no theoretical FLOPS conversion or extrapolated universal crossover is used.
