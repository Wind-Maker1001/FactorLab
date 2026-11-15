# CI-PERF-02: historical PIT Git verification

## Measurement

Base: `ac3505faa13f2cc9533fb13ba16551c4d521cd36` (#170).
Environment: Windows 11, CPython 3.13.9 (MSC v.1944, AMD64).
Only `tests/evidence/test_pit_archive_verifiers.py` was executed.
The uninstrumented baseline completed in **295.73 s** (33 passed, 75 setup
errors). The final uninstrumented module run completed in **111.98 s**, with
**40 passed, 75 setup errors**, exit status 1: a measured 62.13% wall-time reduction
including seven additional security cases. This includes all original 33 synthetic cases,
five upstream comparisons, object replacement/reverification, and the additional
incorrect missing-blob binding refusal. Normal timing runs overlapped on this
machine, so these wall times include contention rather than isolated latency.

| Instrumented measurement | Original | Optimized, including six added cases |
| --- | ---: | ---: |
| Wall time, cProfile and filesystem sampling enabled | 302.52 s | 185.12 s |
| Original cases, setup/call/teardown total | 254.50 s | 32.18 s |
| Added cases, setup/call/teardown total | — | 65.10 s |
| Git subprocesses | 5,652 | 1,709 |
| `cat-file` calls | 3,640 | 883 |
| `ls-tree` calls | 1,708 | 446 |
| Temporary regular files, sampled peak | 5,054 | 5,917 |
| Temporary logical bytes, sampled peak | 2,143,793 | 2,496,506 |
| Passed | 33 | 39 |
| Historical publication setup errors | 75 | 75 |

Instrumentation wrapped real `subprocess.Popen` calls, counted Git commands,
recorded pytest phase durations, and ran cProfile. File counts and summed logical
file sizes were sampled recursively in a fresh pytest basetemp after each test's
teardown. They are test-boundary maxima, not continuously sampled peaks or NTFS
allocated space. Pytest retains the disposable repositories until session cleanup;
the six additional cases therefore increase retained file count and bytes.
The instrumented optimized snapshot precedes the final additional incorrect
missing-blob binding case and entry-before-availability ordering refinement;
the final uninstrumented run validates those changes. The original-case phase
total fell 87.35%; total Git calls fell 69.76% even including upstream comparisons.
Profiler/sampling wall times are not normal pytest runtime. Sampling and reporting
alone consumed 80.75 s in the optimized profile.

## Root cause and fix

The historical graph was already built once per module. Destructive cases already
received full independent copies without alternates. Rebuilding histories and file
copying were not the dominant cost: the baseline profile attributed 242.38 s to
5,652 subprocess runs. `cat-file` and `ls-tree` accounted for 226.15 s of measured
Git process time. Each delivery blob was queried separately for its tree entry,
type and bytes at both source and cherry-pick revisions.

The independent verifier batches requested objects with `git cat-file --batch`,
checks each returned object's type, length and reconstructed Git SHA-1, and queries
the recorded paths together with `git ls-tree -z`. Tree enumeration stays scoped to
receipt paths and protected roots, without recursively reading unrelated history.
Object and tree reuse lasts only for one verification invocation. The receipt,
SHA-256 byte bindings, parent/history checks, protected entry modes/types/OIDs,
working-tree checks and CLI error propagation remain enforced.

The original reference remains byte-for-byte intact, with SHA-256
`b211d3202b6bf159e5ea585b37469b745584889523b477f13722f0ee834cdb99` checked before
loading. Existing synthetic tests exercise the independent verifier; five added
cases compare valid history and wrong-parent, changed-tree, wrong-blob and
missing-object rejection against the original implementation. Another case
verifies successfully, replaces a real loose object with different valid zlib
content under the recorded OID, and verifies again: the object SHA mismatch must
be rejected rather than hidden by caching. All operations use real Git objects.

The original 33 synthetic cases still pass, including missing commits/blobs,
missing intermediate parents (both Git exit-status conventions), shallow history,
unrelated ancestry, changed protected trees, incorrect receipts, absent Git,
foreign object-store injection and inherited repository/configuration overrides.
Each destructive test retains its own writable repository copy. Neither reference
bytes nor source research objects are modified. No CI workflow or shard-selection
logic is changed.

The 75 publication cases fail at setup on both revisions because the existing
independent historical root expects `.github/workflows/market-admission.yml`,
which is absent from this main revision. These errors remain errors, not skips or
passes; no replacement trust root is introduced.

## Fixture compatibility and targeted layering

The fixture compatibility repair retains zero-argument direct loading while
checking the immutable upstream bytes before loading the optimized verifier.
After synchronization with main containing #172, local CPython 3.12.7 execution
of the migration boundary and archive modules produced **48 passed, 75 setup
errors** in 115.48 seconds. Direct loading and changed-byte refusal passed;
the errors retain the missing historical workflow cause above.

In [Actions #43726620179](https://github.com/Wind-Maker1001/FactorLab/actions/runs/43726620179),
the trusted targeted observer collected 115 archive cases without applying the
historical/current partition: 40 passed and 75 failed at setup. Comparing its
execution inventory against the unchanged matrix found exactly 75 historical
setup errors and zero errors outside that matrix.

The independent controller deployment is [#176](https://github.com/Wind-Maker1001/FactorLab/pull/176).
Its 34 targeted security regressions passed. A local replay of the original
risk plan's selected archive file on the repaired branch, with the replay subject
explicitly rebound rather than presented as the original trusted identity,
produced **40 passed, 75 exact historical nodes isolated**, exit 0, in 95.01 seconds.
The V2 inventory and receipt record history as `BLOCKED`, not passed. The
controller checks all current setup/call/teardown phases, exact historical
coverage and actual process status. This local replay is not trusted Actions
integration evidence. Deployment requires formal control review and merge to
main before dependent PR validation can use it.

## Reproduction

Run with a configured Python 3.13 environment from the repository root:

```sh
python -m pytest tests/evidence/test_pit_archive_verifiers.py -q --tb=line --durations=10 --basetemp=<fresh-directory>
```

For baseline execution, use the same module from the base commit in an isolated
source snapshot with its `ROOT` pointing at the base repository. Use fresh,
separate temporary directories for each run. To obtain the instrumented metrics,
profile `pytest.main`, collect `pytest_runtest_logreport` phase durations, count
real Git `Popen` calls and sample basetemp at teardown as described above.

NO LIVE MONEY. Software/object-integrity tests do not authorize financial admission.
