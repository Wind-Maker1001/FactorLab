# MC-01: Grid Paper execution performance

NO LIVE MONEY. All observations and capacity are synthetic; funding is not simulated.

## Result

On the current main baseline `1e16a01615c325bc97e7982c1122636f4c477ae8`, changing only the research account SQLite journal from DELETE to WAL, retaining `synchronous=FULL`, reduced paired execution wall time from **267.638 s to 109.705 s (2.44× throughput; 59.0% less time)**. This is a newly measured baseline, not the historical R10 result.

The benchmark used nine fixed paths, each with 169 hourly observations, two capacities (`.10`, `.01`), and two repetitions. Each mode processed 36 accounts; execution order reversed in the second repetition. Six paths are deterministic stresses, one is a seeded GBM-style log-return path, one is a fixed moving-block resample, and one exercises risk exit. The full arrays are preserved in `representative-inputs.json`; prices are generated before timing. Configuration and costs are identical between modes.

| Metric, 36 accounts per mode | DELETE + FULL | WAL + FULL |
| --- | ---: | ---: |
| Wall time | 267.638 s | 109.705 s |
| Mean wall time per account | 7.434 s | 3.047 s |
| Process CPU time | 63.578 s | 34.453 s |
| Process write-transfer bytes | 143,557,418 | 52,215,076 |
| Process lifetime peak RSS | 164,958,208 B | 164,958,208 B |

Windows write-transfer counters measure bytes submitted by this process, not physical device writes or filesystem write amplification. RSS is the lifetime process high-water mark in the shared paired process, not an isolated per-mode allocation measurement. Timing includes execution, exports, attribution, reading receipts and SQLite authority for equality comparison, and temporary directory cleanup. Profile serialization and benchmark measurement JSON writes are outside the measured interval. CPU counters exclude provenance subprocess CPU.

## Why this change

Each strategy command still uses the existing controller transaction, full account snapshot and event journal. WAL avoids repeatedly rewriting the rollback journal and database pages at every commit; FULL still synchronizes every committed transaction. No financial constraint, transaction boundary, capacity, observation, fill rule or necessary reconciliation is removed. The ordinary PaperStore and ordinary Grid Paper default remain unchanged. Monte Carlo explicitly selects the research WAL option. Closing the last connection checkpoints the per-path account before existing FIFO attribution reopens it. Accounts remain individually bounded and temporary, not a combined database of all paths.

## Measured cost ranking

A separate cProfile run covered the same nine paths and two capacities once per mode. The following cumulative times overlap and must not be added:

| Scope, 18 accounts per mode | DELETE | WAL |
| --- | ---: | ---: |
| Grid Paper execution | 216.447 s | 84.726 s |
| Controller atomic state/command operations | 193.231 s | 62.473 s |
| SQLite execute calls | 183.281 s | 50.084 s |
| PaperStore transaction contexts | 158.363 s | 48.496 s |
| PaperStore snapshot saves | 23.586 s | 2.156 s |
| Provenance capture per account | 11.543 s | 11.272 s |
| PaperStore initialization, including attribution reopen | 2.379 s | 2.675 s |
| Lifecycle attribution, including its data reads | 0.449 s | 0.577 s |
| FIFO attribution, including authority reconciliation | 0.111 s | 0.222 s |
| Temporary directory cleanup | 0.150 s | 0.161 s |
| Temporary directory creation | 0.026 s | 0.020 s |

Controller and Grid signal/order handling are nested: the dominant Grid observe cost is the controller transaction it invokes, not independent signal arithmetic. `profile-functions.json` preserves all function self/cumulative times, including Grid observe/submit/fill operations, Pandas functions, CSV/JSON readers and writers. Pandas and serialization are distributed across execution and attribution; use self times rather than summing overlapping cumulative frames. They are substantially below the SQLite hotspot. Provenance is now a more visible remaining opportunity, followed by repeated snapshot encoding/decoding; neither was changed in this patch. FIFO/lifecycle, file round trips and directory management are low priority for this workload.

## Equality and regression evidence

Every paired run required exact equality of all returned path metrics, byte equality of `daily.csv`, `trades.csv`, `result.json` and `evaluation.json`, and equality of every persisted snapshot and journal row in insertion order. All 36 unprofiled pairs and 18 profiled pairs passed. This includes terminal NAV, hourly inventory/equity/drawdown, fees and slippage, FIFO reconciliation and risk-exit metrics. Dedicated tests additionally compare repeated WAL execution and DELETE execution on partial-capacity and incomplete-exit paths, and exercise rejected-event rollback and reopening the committed WAL account.

The expanded Grid/Paper/controller/FIFO/lifecycle regression command below passed **77 tests in 68.16 s**, including WAL rollback and reopen recovery.

Small-sample gate: 24-hour versions of eight paths × two capacities passed exact paired comparisons before expansion; the initial 15 Monte Carlo tests passed. The first benchmark attempt failed because its verification connection was not explicitly closed before Windows directory cleanup; the benchmark connection lifecycle was corrected before collecting these results.

## Reproduction

Environment versions are in `environment.json`. Measurements here used CPython 3.12.7, NumPy 1.26.4 and Pandas 2.2.2 on Windows. Run from the repository with its dependencies installed:

```bash
PYTHONPATH=src python tools/research/benchmark_grid_execution.py --output /new/output/small --steps 24
PYTHONPATH=src python tools/research/benchmark_grid_execution.py --output /new/output/representative --steps 168 --repeats 2
PYTHONPATH=src python tools/research/benchmark_grid_execution.py --output /new/output/profile --steps 168 --profile
PYTHONPATH=src python -m pytest tests/research/test_grid_monte_carlo.py tests/trading/test_paper_control.py tests/research/test_grid_paper.py tests/research/test_grid_lifecycle.py tests/test_grid_fifo.py -q
```

Output directories must be new. `measurements.json` contains per-account wall/CPU/write/RSS and economic metrics; `inputs.json` freezes actual arrays. Profile mode additionally writes native `.prof`, readable top functions, and complete function timing JSON. The checked-in data uses compact aggregate function evidence rather than binary profiles or whole account databases. Performance depends on filesystem synchronization latency; the financial equality assertions remain the same on another host.
