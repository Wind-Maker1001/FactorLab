# MC-COLAB-03 — research dispatch and batch evidence

Protocol: [MC-COLAB-03](../../research/mc-colab-03.md).

## Current evidence

The final combined portable research/backend regression run passed **220 tests,
zero skips, in 33.58 s**. Earlier integration runs passed 216 tests (35.47 s),
then 98 focused backend/controller and 67 batch/dispatch tests. These are not real GPU/cloud evidence.
The actual process-tree timeout test launches and terminates a child process;
resource/Colab policy tests substitute device and account state.

First installed-wheel batch at commit `586bd55` completed CPU GBM, Bootstrap
and static-grid workloads, retained a deliberately failed worker's completed
samples, and completed the following independent job. Its three CUDA workers
failed because the separate acceptance environment had not installed its own
CUDA runtime libraries; this attempt is PARTIAL, not CUDA acceptance. Install
the declared CUDA extra into that environment before repeating GPU acceptance.

The first Windows → WSL submission failed before allocation: importing the
manifest module eagerly imported the existing experiment tracker and pandas,
which are intentionally absent on the lightweight controller. The package now
loads the tracker only when requested; a standard-library-only control import
regression test covers this. No runtime was allocated or billed by this attempt.
The corrected installed-wheel local acceptance at commit
`33a8894a7157187da2c68a10905b67b12d9705ae` passed its explicit acceptance driver:
all six CPU/CUDA representative tasks succeeded, the intentional missing-output
task failed while preserving numerical samples, and its successor succeeded.
Eight distinct worker PIDs established fresh-process isolation. Aggregate state
is intentionally PARTIAL and the normal batch CLI returns 1; the acceptance
driver returns 0 after verifying those expected outcomes. Whole batch work was
63.944 s, excluding final bundling. [Compact evidence](local-acceptance.json).

| Workload | CPU fresh worker s | RTX 2070 fresh worker s | CPU research s | GPU research s |
|---|---:|---:|---:|---:|
| GBM, 1M × 252, 1 asset | 19.429 | 7.586 | 15.585 | 4.034 |
| Bootstrap, 1M × 252, 1 asset | 16.578 | 6.654 | 14.540 | 3.897 |
| Static grid, 100k × 168 | 3.664 | 4.894 | 1.329 | 2.123 |

Fresh-worker time includes imports, dependency validation and output creation;
research time includes result persistence and is not a synchronized kernel-only
time. These are one-seed acceptance observations, not a replacement for the
prior performance study. Dependencies actually loaded were NumPy 2.3.5,
SciPy 1.16.3 and, on GPU, CuPy 14.2.0. GPU research used RTX 2070 (8 GiB),
with no concurrent research GPU benchmark and process-owned GPU windows.
The cold grid observation itself illustrates why kernel throughput alone is
not a placement model. Corrected cloud acceptance is pending.

TODO: Complete integrated tests and real installed-wheel CPU/CUDA/cloud runs,
then replace pending statuses with measured evidence in this report.

## Placement evidence

Prior [MC-COLAB-02](../mc-colab-02/REPORT.md) cold-process 100000-path,
252-step, one-asset, batch-1024 GBM medians favored CPU (2.467 s vs 3.596 s GPU);
Bootstrap likewise favored CPU (2.297 s vs 3.156 s). The million-path GBM
steady cell (batch 4096) measured CPU 17.041418 s vs GPU 0.455560 s, with one
independent seed and two warm repeats. These are workload-scoped measurements,
not universal cold crossover or cloud lifecycle estimates.

## Cloud accounting

This acceptance has a 10 CU total authorized envelope, shared across allocations,
failures and retries. The second Windows → WSL attempt allocated a runtime but
failed before session identity was persisted: CLI 0.8.0 initialized its SQLite
file lock on `/mnt/d`, whose inherited permissions could not be restricted.
The subsequent cleanup hit the same error. Account observations changed from
zero assignments / 0 CU per hour to one assignment / 2.33 CU per hour, while
the rounded balance remained 199.03 CU. This is not zero-cost evidence.
Recovery reconstructed only the identified failed allocation in native Linux
session storage and stopped it through the official CLI. The server then
reported no active sessions; usage was 0 CU/h with zero assignments and a
198.88 CU balance. The observed rounded account difference is 0.15 CU, charged
to this round's 10 CU envelope. The controller now stores session state under
its native Linux home directory and initializes CLI storage before allocation,
so a storage failure cannot first create a paid runtime.
Record actual hardware, initial/final account balance, observed active rate,
deployment/execution/download/release times, retained results and server-side
release confirmation. Rounded balance differences and rate-times-wall estimates
are distinct evidence, not exact per-experiment billing.
