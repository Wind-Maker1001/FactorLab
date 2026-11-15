# CI-PERF-01: Demo Grid capacity recovery

## Result

The unchanged capacity test passed in 7.17 s and 8.26 s after the change, versus 411.02 s before it. Full subprocess wall times were 7.887 s and 9.008 s versus 412.676 s: a 97.8–98.1% reduction (45.8–52.3×). Both repeats meet the 60 s stretch target.

Target: `tests/integrations/test_demo_grid_supervisor.py::test_manual_management_inherits_continuous_capacity_after_stop`.

Baseline: `ac3505f` (origin/main). All local comparisons used the same Windows 11 host, D: worktree, Python 3.14.0, SQLite 3.50.4 and virtual environment. Dependencies: pytest 8.4.2, NumPy 2.5.4, pandas 2.3.3, SciPy 1.16.3, PyArrow 22.0.0, PyYAML 6.0.3. These are local measurements, not new GitHub Actions timings. The previously observed Actions times were 445.28 s on Python 3.13 and 404.21 s on Python 3.14.

## Diagnosis

Phase timing used `perf_counter`, temporary AST-inserted phase markers and timing wrappers around existing functions in both the parent and recovery subprocess. It retained the actual test, database operations, exchange fixture and recovery. No profiler dump was generated. Instrumented results are diagnostic; the wall comparisons above use ordinary pytest without instrumentation.

| Phase | Before (s) | After (s) |
| --- | ---: | ---: |
| Initial session open, recovery and first order | 0.892 | 1.040 |
| Construct and persist 1,001 historical rejected orders | 0.091 | 0.078 |
| Write 10,001 metadata entries to each of two journals | 0.224 | 0.219 |
| Capture cursor and close initial session | 0.001 | 0.001 |
| Serialize offline exchange state | 0.001 | 0.001 |
| Entire child launch, restoration and permission checks | 224.785 | 3.989 |
| Child imports/startup | 1.166 | 2.212 |
| Child fixture setup | 0.002 | 0.002 |
| Child runtime/session open | 0.064 | 0.069 |
| Child account recovery and reconciliation | 223.234 | 1.424 |
| Child state/unauthorized-write assertions and session close | 0.046 | 0.023 |
| Child GET-only assertion | <0.001 | <0.001 |
| Parent journal assertions | 0.002 | 0.002 |
| Authorized runtime/session open | 0.214 | 0.195 |
| Authorized account recovery | 133.110 | 1.233 |
| Authorized cancel, state assertions and session close | 0.325 | 0.233 |

Child phases are contained in the entire-child row, not additive to it. Startup and final pytest overhead are outside the test phases. The instrumented test passed before in 362.70 s and after in 7.64 s; ordinary baseline/optimized repeats above provide the actual comparison.

Cumulative timings below overlap (for example, `save` contains serialization); they must not be added together.

| Child recovery process operation | Before calls / seconds | After calls / seconds |
| --- | ---: | ---: |
| Controller atomic operations | 2,005 / 222.067 | 3 / 0.980 |
| Snapshot load / deserialization | 2,007 / 57.486 | 5 / 0.058 |
| Snapshot serialization and save | 2,006 / 63.568 | 4 / 0.099 |
| State deepcopy | 2,005 / 46.979 | 3 / 0.029 |
| All storage serialization | 3,008 / 60.676 | 1,006 / 0.084 |
| SQLite transaction boundaries, excluding body | 2,015 / 47.804 | 13 / 0.268 |
| Adapter reconciliation | 2 / 0.569 | 2 / 0.489 |
| Recorded metadata/events | 1,002 / 2.043 | 1,002 / 0.017 |

The parent process likewise dropped from 1,008 atomic operations to 8; snapshot loads fell from 1,011 to 11 and saves from 1,010 to 10. It still made 21,012 metadata/event calls. Building the capacity fixtures was not the bottleneck.

## Fix and preserved behavior

Recovery previously opened a complete controller transaction for every timestamp correction and every replayed order. Each transaction deep-copied, loaded and saved the entire accumulated account. For 1,002 orders this repeatedly processed the same large snapshot, even on idempotent recovery.

The recovery loop now runs inside one existing controller atomic operation and applies each confirmed event in the same order using the existing broker event application. This retains per-event identity checking, cumulative fill and fee validation, account updates, and persisted event ordering. Existing import of missing protocol identities and final account reconciliation remain in place. A failed replay rolls back the whole replay batch and restores its in-memory account projection.

The target test is unmodified: 1,001 historical orders plus the original order, 10,001 additional metadata entries in each ledger, real SQLite persistence, a separate recovery process, GET-only restoration, unauthorized cancel/submit rejection, event evidence, and subsequent authorized cancellation all remain. There is no replacement recovery mock or reduction in order/event counts. No SQLite pragmas, schema, capacity limits, CI workflows, risk classification or shard scheduling changed.

The new regression interrupts after the second order has actually been applied, verifies both persisted and in-memory account rollback plus unchanged event journal, then retries recovery and verifies exact account totals and repeated-recovery idempotency. Existing Demo Grid restart/fragmented-fill/round-trip tests exercise the strategy recovery path without changing its FIFO or account assertions.

## Reproduction and test evidence

From the checkout root with the recorded Python environment:

```sh
python -m pytest -q tests/integrations/test_demo_grid_supervisor.py::test_manual_management_inherits_continuous_capacity_after_stop
```

A Python `subprocess.run([sys.executable, '-m', 'pytest', ...])` wrapper measured elapsed `perf_counter` around the complete pytest process. Baseline: 1 passed in 411.02 s, wall 412.675972 s. Optimized repeat 1: 1 passed in 7.17 s, wall 7.886820 s. Repeat 2: 1 passed in 8.26 s, wall 9.008247 s.

The first combined target/Futures validation passed all 51 tests in 11.64 s (wall 12.996483 s), including the batch rollback regression.

The first six-file Demo run had 161 passes and two import failures in subprocess tests (`test_capability_cannot_restore` and `test_cross_process_identity_and_unknown_query`): `ModuleNotFoundError: No module named 'factorlab'`. Set `PYTHONPATH` to this checkout's absolute `src` directory so child interpreters use the same source checkout, then run:

```sh
python -m pytest -q \
  tests/integrations/test_demo_grid_supervisor.py \
  tests/integrations/test_binance_demo_grid_bridge.py \
  tests/integrations/test_binance_demo_grid_network.py \
  tests/integrations/test_binance_demo_network.py \
  tests/integrations/test_binance_demo_execution.py \
  tests/integrations/test_binance_demo_strategy_session.py \
  tests/integrations/test_binance_futures_trading.py
```

Final combined regression result: **213 passed in 91.50 s**, including both cross-process tests, the unchanged capacity test and the new batch rollback regression.

All exchange activity used offline fixtures. NO LIVE MONEY.
