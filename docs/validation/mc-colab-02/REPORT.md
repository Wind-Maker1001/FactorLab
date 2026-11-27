# MC-COLAB-02 — performance and reliability evidence

Protocol: [MC-COLAB-02-v1](../../research/mc-colab-02.md). Canonical configuration: [`mc_colab_02.json`](../../../configs/research/mc_colab_02.json).

## Evidence coverage

The local pilot, five-seed reliability (both models, 1/8 assets), million-path/batch sweep, 32-asset, 168/8760-step, small-job crossover, synchronized profiling and two-process CPU experiments have completed without skipped cases. Regression validation passed 118 tests, including explicit CUDA hardware tests and MC-05 frozen-manifest protection. Actual local CPU is i7-14700KF, not the initially requested 14700K. The isolated 10M/100M GPU capacity experiment also completed. Final regression validation passed 119 tests in 33.42s. [Measured tables](evidence/MEASURED.md), [raw studies and configurations](evidence/) and [CPU/GPU comparisons](evidence/comparisons.json) accompany this report. The approved 2 CU cloud run has also completed; its evidence is being incorporated below. A100/G4 are optional, not acceptance prerequisites for local/L4 evidence. The prior user-reported L4 timings remain separately attributed in the protocol and are not part of formal evidence.

## CPU CI and explicit hardware acceptance

The CUDA-only fixed-input comparison belongs to `tests/research/cuda_monte_carlo.py`, executed explicitly on a CUDA host. Ordinary `test_mc_colab_benchmark.py` collection does not import or probe CuPy. The hardware test requires the existing `cuda` fixture rather than skipping when CuPy is missing, retains CPU/CUDA terminal and drawdown comparisons, and checks synchronized GPU phase names, finite nonnegative durations and profiling metadata. No trusted-CI acceptance rule or skip exemption is changed. Python 3.13 local verification: ordinary CPU suite 16 passed in 20.07s, zero skips; explicit RTX 2070 hardware suite 15 passed in 3.60s, zero skips. Existing performance/seed evidence was not rerun or replaced.

Cloud execution completed on an officially allocated NVIDIA L4 (23034 MiB, driver 580.82.07), 12 vCPU and approximately 53 GiB host RAM using Python 3.13.16 and source revision `2f9bb3e7fff2d1f94175238cf1d825ea126a3356`. All three registered stages completed: GPU five-seed reliability (60 run rows), GPU scale (21 rows), and CPU five-seed reliability (60 rows). A fresh Python process verified loaded NumPy 2.3.5, CuPy CUDA13 14.0.1, SciPy 1.16.3 and psutil 5.9.5 against installed metadata. The downloaded report and NPZ contain 400 original evidence files. [Compact cloud studies, configurations, environment, allocation receipt and comparisons](cloud-evidence/) retain the measurement and statistical evidence. The instance was automatically released and the service reported no active sessions.

## Failure evidence retention

The cloud launcher packages retained stage outcomes and artifacts even when its execution subprocess fails, preserving the nonzero failure. TODO: Extend the separate controller's failure path to download this bundle before releasing the runtime; its current success-only download path can still discard partial cloud evidence. This controller extension is outside the completed study run. The measured run completed every stage and downloaded its evidence before release.

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

## Observed Colab conclusions

Matched five-seed, 100000-path, 252-step workloads used batch=1024, a 1024 MiB GPU pool budget and single-process CPU with one BLAS thread. Steady runner medians include exact engine summaries but exclude external interval analysis:

| Workload | CPU seconds | L4 seconds | Observed speedup |
|---|---:|---:|---:|
| GBM, 1 asset | 2.250107 | 0.079644 | 28.25x |
| Moving-block bootstrap, 1 asset | 1.382178 | 0.108501 | 12.74x |
| GBM, 8 assets | 8.630403 | 0.272721 | 31.65x |
| Moving-block bootstrap, 8 assets | 1.347565 | 0.172529 | 7.81x |

Fresh-process launch-to-first-result medians also favored L4 for these four measured cells: CPU/GPU seconds were 3.443/1.896, 2.594/1.950, 9.848/2.054 and 2.595/2.016 respectively. These exclude installation and allocation; unlike the local fresh-process observations, the measured cloud process-level choice favored GPU at 100000 paths. This does not establish a smaller cloud crossover. GPU reliability, GPU scale and CPU reliability stage walls were 65.656, 30.934 and 241.008 seconds respectively, including their subprocess and external statistical work.

The seven GPU scale cells completed with two warm repeats each. First warm observations were 0.771405 seconds for 1M-path GBM at batch=1024 versus 0.383860 at batch=4096; 1M bootstrap was 0.397171 seconds. The 100000-path GBM 32-asset and 8760-step cells took 0.676880 and 2.357128 seconds respectively. These are single-seed scale observations; full repeat records are retained rather than using one repeat as a five-seed estimate.

All nine family-corrected CPU/GPU contrast intervals included zero in each of the four cloud reliability workloads. Single-asset GBM CVaR99 GPU-minus-CPU was -0.0006532, interval [-0.0054356, 0.0041292]; bootstrap was 0.0022265, interval [-0.0030805, 0.0075335]. Deterministic backend seeds reproduce the local point contrasts; the cloud rerun supplies independent hardware/runtime evidence, not new independent seeds to pool with the local study. These remain precision/difference results, not business equivalence claims.

## Cost and selection

The completed Colab allocation reported 2.33 CU/h while active. Displayed account balance changed from 199.35 to 199.22 CU, an observed rounded balance difference of 0.13 CU, within the approved total 2 CU budget. This is an account-balance observation, not exact per-job billing. The controller lifecycle lasted 600.251 seconds including allocation, deployment, execution, download and cleanup; the script recorded 352.272 seconds for its source deployment and studies, excluding the controller's initial wheel installation. After release, usage was 0 CU/h with zero active assignments. Local throughput is not converted to CU/h. Device-selection comparisons use matched measured cells and distinguish cold from steady execution; no theoretical FLOPS conversion or extrapolated universal crossover is used.
