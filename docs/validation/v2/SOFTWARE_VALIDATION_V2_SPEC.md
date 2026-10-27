# CURRENT_SOFTWARE_VALIDATION_V2

Status: implemented software contract; historical migration approval **PENDING**.
Public input PR: `ed2b4c1a0a10afd794c674b14d105b748b01b9c8`.
Public main: `afac4f9d4c2b123c0b32787ccd1cf67fe965c723`.
Neither ref had advanced at the initial 2026-10-28 remote inspection.

## Scope and evidence

V2 validates this independent repository's software: wheel/sdist build and
installation, module and license integrity, factors, four existing models,
walk-forward, portfolio accounting, CN/US adaptation, corporate actions,
concurrent prediction publication, financial refusal and exploratory isolation.
It adds no factor, model, market, strategy, provider or financial data feature.

The exhaustive selection is the existing `integration_checks.py --suite current`:
all configured offline tests except the exact original 186 historical records.
The original two network exclusions remain unchanged. Newly failing tests cannot
be relabeled historical. Selection drift fails. Python 3.13 and 3.14 both run the
entire current layer; targeted suites remain additional overlapping checks.

`BASELINE_4157.json` contains the actual 4,157 passing nodes and 814 critical
nodes from run [40524983243](https://github.com/Wind-Maker1001/FactorLab/actions/runs/40524983243),
executed merge `922e04ab162656267f07d365a92f97a8542b5653` / PR head `ed2b4c1`.
Both original ZIPs were downloaded and checked against the GitHub artifact
SHA-256 digests before extraction. The manifest records those digests, original
JUnit/selection hashes and 335 original Git-blob byte hashes. This is a test
inventory, **not** a new Golden, trust root, safety receipt or archive substitute.

`validation_v2.py` independently reconstructs node outcomes from full/current
JUnit, compares every original node, critical node and historical status, checks
new nodes, duplicates, frozen execution inputs and the actual exit codes, and
emits an itemized CSV. It does not reuse the selection plugin's classification
algorithm. Its attack tests reject omitted software/safety nodes, changed
historical outcomes, extra failures, outcome drift, inventory lies, missing
historical records and duplicate JUnit nodes. Independent implementation is not
independent human approval; human review remains PENDING.

Original test, configuration, license, dependency and V1 control Git blobs must
remain exact. Worktree bytes must match those blobs apart from explicitly
reported CRLF/LF checkout conversion; execution receipts bind the actual bytes.
No V1 raw-byte verifier is changed or resealed. Such a verifier can still refuse
the current source scope or checkout, and that refusal remains visible.

## Lock safety contract

Keep `os.open(O_WRONLY | O_CREAT | O_EXCL, 0o600)`. Only a Windows
`PermissionError` invokes a fresh native exclusive `NtCreateFile(FILE_CREATE)`.
It cannot open, overwrite or truncate an existing lock. The direct operation's
status, rather than a last-thread status or metadata probe, decides behavior:

| Native result | Behavior |
| --- | --- |
| Success | Transfer owned handle to CRT; existing write/fsync/ownership/release protocol |
| Object-name collision | Ordinary existing-lock contention within existing timeout |
| `STATUS_DELETE_PENDING` (`0xC0000056`) | Bounded polling within existing lock timeout |
| Access denied, sharing violation or any other error | Refuse publication immediately |
| Descriptor conversion failure | Close native handle; leave lock for inspection; refuse |

Both creation operations preserve exclusion. A retry never enters the critical
section; only a successfully created lock does. No owner is evicted, no stale
lock is broken and release still requires the random ownership token. Invalid
non-object JSON also refuses release. The date namespace, version allocation,
staging rename, prediction schema, canonical serialization and hash verification
are unchanged. Native permission checks use inherited directory ACLs, like the
original Windows CRT creation. No security descriptor is relaxed.

`lock_acceptance.py` executes 127 independent rounds by default: one single
writer, 100 four-thread same-date rounds, 10 different-date thread rounds,
12 four-process same-date rounds and four different-date process rounds. Each
round records execution count, status, elapsed time, file count, unique
date/version pairs, all file hashes, verification and lock residue. Its separate
fault suite covers ACL denial, pending-delete recovery/timeout, corrupt/foreign
ownership, crash retention, body cleanup, sharing modes and descriptor failure.
Native tests run on Windows; Linux runs portable tests and explicitly skips the
six Windows-specific cases. Stress success alone is not a proof of correctness.

## HISTORICAL_ARCHIVE_VALIDATION_V1

The unchanged full-project command runs every original testpath and propagates
its real exit code. It retains the original 50 FAIL and 136 ERROR observations:
140 missing upstream workflow/control roots, 37 missing historical publications,
two unavailable original Git objects and seven incomplete historical attacks.
The single collection-error module represents one observed error record; its
authentic parameterized test count remains UNKNOWN without the original root.
No archive, original object, historical Golden, verifier, receipt, trust anchor or
attack material is synthesized to make these observations pass.

The CSV maps each of 4,157 old software nodes to itself with original test bytes,
each of 186 historical records to retained V1 with its explicit gap and no claimed
replacement, and each new test to additional V2 coverage. Missing archived
assertions remain uncovered historical risks, even where live software refusal
tests pass. V2 never inherits V1 financial authorization.

## CI and release boundary

Preserve Package, both Python regressions, the complete historical job and native
control diagnosis with actual failure propagation. Add candidate-only Windows
and Linux lock acceptance and a separate always-run V2 outcome comparison that
downloads both Python software artifacts and the complete full artifact.
`if: always()` retains evidence; it does not override any test exit code.
No continue-on-error, mass xfail, old-test removal or protection change is used.
Known control failures in main/input diagnosis remain actual failed observations.

Only `docs/validation/v2/reports/**` report-only PR changes are excluded from
starting another full Actions run. They cannot change code, tests, this spec,
the coverage baseline, workflows or safety controls. Reports name the actual
tested commit and final documentation commit separately. This avoids rerunning
an unchanged failing full job to seek a green result; it does not waive its FAIL.

Historical failures remain merge blockers until genuine recovery or explicit
maintainer risk acceptance plus independent review. PR #1 stays Draft. No main
merge, Release, PyPI publication, branch protection edit or financial admission.

**PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED / NO LIVE MONEY**
