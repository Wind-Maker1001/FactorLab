# MC-COLAB-03 — research dispatch and batch evidence

Protocol: [MC-COLAB-03](../../research/mc-colab-03.md).

## PR #275 download-deadline regression

Offline controller regression passed 47 tests in 1.94 s with zero skips.
Controlled clocks cover a successful 60-second download with 300 seconds of
remaining work time for both single experiments and batches, and retrieval
after a research failure with remaining work time. Expired-work tests cover
zero, 20 and 31 seconds of overrun: salvage shrinks within the original
lifecycle reserve and cannot consume the final 90 seconds reserved for release.
Existing partial-file, transfer failure, release failure and original-exception
preservation tests also pass. This revision runs no real Colab experiment.

## PR #275 benchmark admission and CPU dependency regression

The per-cell planner is shared by selection and execution. Regression covers
scale admission at 8192 MiB for CPU/local GPU/simulated Colab, sequential-cell
peaks, selected-seed concurrency, automatic GPU batches, exact prepared-array
persistent bytes, genuine over-budget rejection, and disk/statistics admission.
The initial combined run passed 111 tests; three existing benchmark integration
tests exposed missing Git/worktree mapping and `threadpoolctl` in the isolated
test environment. After supplying Git with Linux worktree paths and declaring
`threadpoolctl` in the benchmark extra, all 17 benchmark tests passed in 141.10 s.
The CPU extras explicitly declare `packaging>=24,<27`; the existing full
research snapshots already pin compatible `packaging==26.3`. Trading execution
dependency sets and locks are unchanged. Independent review found no remaining
blocking issue after the shared host/device planning corrections.

A provenance-built wheel at `9787f31d2b3817c4b5c5de976a46cbf5985d1fec`
was installed with only the documented CPU research extras in a new Python 3.13
environment. A registered CPU GBM batch (32 paths, eight steps, one asset,
batch 16) completed with `SUCCEEDED` and retained its report and samples.
The installed runtime included packaging 26.4 and threadpoolctl 3.8.0, with
NumPy 2.3.5 and SciPy 1.16.3; it contained neither CuPy/CUDA toolkit packages
nor pytest/build development dependencies.

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
not a placement model.

The Windows → WSL → official CLI batch at commit `38c4444` completed all three
registered workloads on NVIDIA L4 (23034 MiB), using one allocation and one
installation. GBM and Bootstrap each used 1M paths × 252 steps; static grid
used 100k paths × 168 steps. Fresh-worker times were respectively 7.436,
5.176 and 4.274 s. Distinct PIDs 1081, 1116 and 1143 loaded Python 3.13.16,
NumPy 2.3.5, SciPy 1.16.3 and CuPy 14.0.1 from the installed wheel environment.
All outputs were retrieved and unpacked. The official server listing confirmed
no active sessions after release, with zero assignments and 0 CU/h.
[Compact cloud evidence](cloud-acceptance.json).

Controller lifecycle was 263.067 s: deployment 85.845 s, remote execution and
bundling 37.072 s, download 7.604 s and release 11.885 s; the remaining
120.660 s covers allocation, probes and other control overhead. Remote batch
work before final bundling was 16.960 s. At the observed 2.33 CU/h, the entire
controller wall-time proxy is 0.170 CU; it is not exact billable runtime duration.
The rounded balance remained 198.88 CU during this successful attempt, which
must not be interpreted as free compute or finalized billing. Including the
prior recovery's 0.15 CU observed balance change, the conservative combined
observation-plus-wall-time proxy is approximately 0.320 CU, below 10 CU.

Batching paid the 85.845 s deployment once rather than three times. Holding
this measured deployment duration constant, separate fresh installations would
add 171.690 s (about 0.111 CU at 2.33 CU/h), before any repeated allocation,
probe, transfer or release overhead. This is a lifecycle counterfactual, not a
measured three-single-job comparison. Local RTX 2070 fresh workers totaled
19.133 s for these workloads; cloud task workers totaled 16.885 s but its full
263.067 s lifecycle dominates this small batch. These measurements support
keeping such immediately executable work local rather than selecting cloud
from kernel speed alone.

The installed-wheel auto/registered-benchmark batch also succeeded: the
1000-path GBM task selected CPU conservatively (2.434 s fresh worker), and the
registered reliability benchmark completed on CPU in 49.258 s. Total batch
work was 51.819 s. The native-session-storage fix passed 44 focused controller
and admission tests in 4.16 s. The subsequent combined batch, dispatch,
selection, process-tree, admission and controller suite passed 152 tests in
8.26 s with zero skips. Explicit Linux account-lock acceptance passed both
tests in 3.65 s, including competing controllers and lock release after crash.

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

The next allocation attempt at commit `38c4444` passed native session-storage
preflight, but the official assignment endpoint returned Service Unavailable.
Cleanup verified no active server sessions, zero assignments and 0 CU/h;
the rounded balance stayed 198.88 CU. No experiments ran in this attempt.
Record actual hardware, initial/final account balance, observed active rate,
deployment/execution/download/release times, retained results and server-side
release confirmation. Rounded balance differences and rate-times-wall estimates
are distinct evidence, not exact per-experiment billing.
