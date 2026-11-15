# CI-PERF-07 — Python 3.14 hotspot diagnosis

**Decision: NOT_WORTH_OPTIMIZING for a shard-2-specific implementation change.**

NO LIVE MONEY. The observations concern offline software validation, not financial qualification.

## Measurement identity

The initial observation is [run 43735048156](https://github.com/Wind-Maker1001/FactorLab/actions/runs/43735048156), PR #175. Its receipt identifies subject `847e807d7a3257a8eef0d6d5ed2804d0df0b2c40`, head `0c5dfe89962fdf056f7f4eb66ab3a7fc8cd66f64`, and trusted base `27180431626e6395dfb7907bab5e81f74cbb20fe`. It includes #172, but not #173. In particular, its source inventory does not contain `tools/migration/verify_pit_archive.py`; it is not an appropriate current PIT baseline.

The latest complete successful run at diagnosis time is [43735547182](https://github.com/Wind-Maker1001/FactorLab/actions/runs/43735547182), PR #173. All six receipts identify subject `31346cca2d233ce65bf64728009884c53788b86c`, head `cfdaafaee9cc1c2fad045696bc18133c59c28361`, and the same trusted base. Each Python version executed 7,819 tests without failures, errors, or skips. This run includes both earlier optimizations. Its receipt source hashes match the measurement checkout `bdd4e6f5eef14bd3f383c5af73ec695bcb34b901` for:

| Source | SHA-256 |
| --- | --- |
| Demo gateway | `6b60acf561912f2411b13c756672515041d5de14b0b58985a45ab59035da8fc5` |
| PIT verifier | `d911195f9346b1c4d582b18c780e8426c88d4b7afe0c2a4df2c2a2b4c7066520` |
| CLI | `a6ed6c34c1e5ab27a473338a7bc87d9aeae783bc5168860527832bf6d9424780` |
| Comparison-contract tests | `2f1cb8d02855a9042a10f54f1275bfd5eba35163fa899793ac8381bf4e1015eb` |

JUnit, `current/tests.log`, `current/receipt.json`, and GitHub job step timestamps are the evidence sources. JUnit testcase durations include fixture setup and teardown; they are not pure call time. The log's `--durations=30` separates the largest setup/call costs.

## Latest complete six-shard execution

All times below are seconds. Step duration is the integration-check step, not installation or complete job duration.

| Python | Shard | Nodes | JUnit sum | Suite | Step |
| --- | ---: | ---: | ---: | ---: | ---: |
| 3.13 | 0 | 3,130 | 712.400 | 734.766 | 744 |
| 3.13 | 1 | 2,304 | 423.748 | 435.494 | 439 |
| 3.13 | 2 | 2,385 | 517.803 | 530.057 | 534 |
| 3.14 | 0 | 3,130 | 780.208 | 808.113 | 814 |
| 3.14 | 1 | 2,304 | 774.983 | 793.525 | 799 |
| 3.14 | 2 | 2,385 | 770.018 | 786.510 | 793 |

Python 3.14 shard 2 is not the latest critical path. Its job completed at 16:43:27Z, shard 1 at 16:43:40Z, and shard 0 at 16:43:53Z. Holding all other work fixed, even eliminating shard 2 entirely saves **zero seconds** from the latest parallel test-stage completion. The shard-2 step trails the maximum by 21 seconds; its complete job finishes 26 seconds earlier than shard 0.

In #175, the corresponding Python 3.14 JUnit sums were 790.383 / 427.246 / 1011.133. No module changed shard between these two runs. The latest shard-2 comparison, CLI phase2, compute-receipt, admission, readiness, and partitioned-walk-forward times are respectively 78.2%, 73.6%, 73.0%, 73.6%, 72.3%, and 70.0% of that earlier run. Conversely, shard 1 grew substantially. These coherent changes across independent modules do not support attributing the original 1038-second step to one Python-3.14-specific defect.

This is not proof that repartitioning is the sole cause: runner/environment variation remains material even with unchanged module allocation. No scheduling change is proposed here.

## Actual shard-2 hotspots

Share uses the latest Python 3.14 shard-2 JUnit sum of 770.018 seconds.

| Module under tests/ | Nodes | Python 3.13 | Python 3.14 | Share | 3.14/3.13 |
| --- | ---: | ---: | ---: | ---: | ---: |
| model_readiness/test_comparison_contract.py | 62 | 81.978 | 125.904 | 16.35% | 1.54 |
| research/test_cli_phase2.py | 45 | 73.432 | 113.748 | 14.77% | 1.55 |
| research/test_cli_compute_receipts.py | 6 | 36.761 | 59.652 | 7.75% | 1.62 |
| market_admission/test_admission.py | 54 | 35.080 | 55.394 | 7.19% | 1.58 |
| model_readiness/test_readiness.py | 7 | 26.938 | 45.619 | 5.92% | 1.69 |
| research/test_partitioned_walk_forward.py | 23 | 29.343 | 41.652 | 5.41% | 1.42 |
| research/test_research_workflow.py | 18 | 22.918 | 33.268 | 4.32% | 1.45 |
| security/test_pr_identity.py | 31 | 28.778 | 30.484 | 3.96% | 1.06 |

The latest duration log assigns 44.22 seconds to module-scoped `comparison_bundle` setup and 41.30 seconds to readiness setup. The phase2 end-to-end four-model test costs 60.52 seconds. Each of six compute-receipt cases costs 9.72–10.13 seconds. They execute model/report production, not merely receipt serialization.

## Local profile and fixture boundaries

A complete comparison-contract module run on Windows 11, CPython 3.14.0, pandas 2.3.3 and NumPy 2.3.4 passed all 62 nodes. With cProfile enabled it took 295.29 pytest seconds (296.545 profile seconds). Hosted CI uses CPython 3.14.10 on Windows Server 2025; this local timing is not a hosted performance result.

The profile records:

| Function | Calls | Cumulative seconds |
| --- | ---: | ---: |
| command_compare_models | 63 | 199.016 |
| _verified_stage_cohort | 124 | 150.935 |
| validate_stage_diagnostics | 136 | 120.722 |
| diagnostic_frame_sha256 | 9,684 | 97.373 |
| command_walk_forward | 4 | 79.046 |
| read_parquet | 1,178 | 4.643 |

Cumulative entries overlap and must not be added. The digest deliberately sorts all columns and serializes canonical CSV; its result binds historical sample and artifact identities. Stage validation recomputes those identities from verified snapshots, including resealed counterexamples. These checks are substantive security/history assertions, not dead computations.

The fixture already produces all four models once per module. The 62 node identities include 40 provenance/input mutations, 20 resealed out-of-sample mutations, byte-tamper rejection, and the historical-comparison/formal-admission separation test. Each mutation restores its files in `finally`. Widening fixture scope or caching validation across these writes risks hiding exactly the mutations under test. `_inputs()` already reuses process-local immutable serialized preparation and returns fresh mutable values.

The profile identifies canonical hashing and validation as a future cross-cutting investigation, not evidence for a minimal safe patch here. Changing canonical ordering risks changing existing historical digests; skipping repeated validation requires proving immutable ownership at each producer/consumer boundary. Neither should be introduced as a shard-2 timing workaround. Parquet reads are a small fraction of the measured cost; database initialization and repeated Git processes are not the dominant observed path.

No module was reassigned and no fixture lifecycle was changed. Consequently this diagnosis introduces no additional fixture initialization in adjacent modules and no shared mutable cache.

## Bounded unprofiled measurement

The first unprofiled run passed all 62 nodes: 197.551 suite seconds, 193.709 summed testcase seconds, and 199.587 process wall seconds. Peak working set was 571,834,368 bytes (545.34 MiB); Windows peak pagefile usage was 2,291,810,304 bytes. Process CPU time was 152.609 user / 48.203 system seconds. A `subprocess.Popen` counter installed before test collection recorded **zero subprocesses**. Retained measurement output, including fixture files, was 27,713,781 bytes across 395 files.

Windows process I/O counters reported 7,760,257,657 read bytes and 35,727,906 write bytes. These are OS process counters, not measured physical-disk traffic. Peak pagefile usage likewise is not a claim of physical paging. The counter plugin adds observation overhead; it does not replace model execution or patch tested functions.

Both unprofiled repetitions use the same interpreter and environment: pytest 8.4.2, NumPy 2.3.4, pandas 2.3.3, SciPy 1.16.3, scikit-learn 1.7.2, PyArrow 22.0.0, matplotlib 3.10.7, LightGBM 4.6.0, XGBoost 2.1.4, and psutil 7.2.2. No dependency was installed or added for this diagnosis. Set `PYTHONHASHSEED=0`, `MPLBACKEND=Agg`, `PYTHONUTF8=1`, and `TZ=UTC` before starting the interpreter.

Functional reproduction from the measurement checkout:

```text
python -m pytest tests/model_readiness/test_comparison_contract.py -q --durations=20 --junitxml=tests.xml --basetemp=<new-empty-output>/tmp -o cache_dir=<new-empty-output>/pytest-cache
```

For profiling, prepend `-m cProfile -o comparison.prof` before `-m pytest`. For the resource measurements, a pytest plugin records `psutil.Process().memory_info()`, `cpu_times()`, and `io_counters()` at session finish and counts `subprocess.Popen` constructions from before collection; a parent times `subprocess.run` and sums retained output file sizes. Exact node identities and compact raw measurements accompany this report.

| Same-code repetition | Passed nodes | Suite seconds | JUnit sum | Process wall seconds | Peak working-set bytes | Retained bytes | Files | Subprocesses |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 62 | 197.551 | 193.709 | 199.587 | 571,834,368 | 27,713,781 | 395 | 0 |
| 2 | 62 | 195.096 | 190.742 | 197.194 | 572,866,560 | 27,713,761 | 395 | 0 |

The two process wall times differ by 1.2%. The second run used 142.625 user / 47.563 system CPU seconds and reported 7,760,259,517 read / 35,728,384 write bytes. All 62 parameterized JUnit identities match between repetitions and match the same module in the latest hosted run. The profiled run also passed all 62 nodes. These runs include the existing provenance, resealing, tamper, and formal-admission separation assertions.

No implementation optimization has been applied; no before/after speedup is claimed. These are bounded same-code repetitions supporting the diagnosis, not a substitute for an optimization experiment. No additional regression suite is required for this documentation-only negative result; functional and security assertions execute unchanged in the measured module.

## Merge recommendation

Merge the diagnostic evidence, not an implementation refactor. The latest actual critical path and the safety-sensitive nature of the measured cost do not justify a shard-2-specific change. A future canonical-hashing optimization should be evaluated across all affected modules with unchanged historical digests and dedicated before/after resource measurements. It is separate from this negative result and from shard scheduling.
