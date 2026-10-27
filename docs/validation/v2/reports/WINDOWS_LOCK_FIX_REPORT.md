# Windows lock fix report

Public input `ed2b4c1a0a10afd794c674b14d105b748b01b9c8`; main `afac4f9d4c2b123c0b32787ccd1cf67fe965c723`. Tested candidate `b2b6b472d500b078cc872c3b942c6e1916054512`; actual executed merge `50e94ab96d2ca17cd75fa4dce225b5c834152869` (identical Git tree `23a324933ba447f2516328a6b16b4b426593e3a9`). Evidence index SHA-256 `abcfe4beb91f8028a90a34f48f184844badb3ed2c39135f6dd2afd98f7d2e0b4`. [Remote run 40550674503](https://github.com/Wind-Maker1001/FactorLab/actions/runs/40550674503). Windows Server 2025 / Python 3.14.9; additional Linux/Python 3.14.8 and Windows/Python 3.13.16 evidence are identified in the index.

**Implemented and accepted in the executed Windows environment.** Production
changes are confined to signal_recorder's lock context and a stdlib-only native
lock helper. No prediction publication, manifest, factor, model or accounting
function changed; the non-lock function ASTs were compared with the public input.

1. Preserve the original O_CREAT|O_EXCL call, mode and all non-Windows behavior.
2. After Windows PermissionError, perform a new NtCreateFile(FILE_CREATE).
   It cannot open/truncate an existing lock. Its exact returned status separates
   delete pending from access denial. A success is a newly acquired exclusive
   lock; the native handle becomes an owned non-inheritable CRT descriptor.
3. Poll only confirmed delete pending or ordinary name collision, using the same
   finite monotonic deadline and configured polling interval. Generic denial,
   sharing failure and unknown errors refuse. Persistent deletion reaches timeout.
4. Preserve random-token ownership, stale-lock refusal and date-scoped release.
   Reject non-object JSON as SignalLockError as well as malformed/foreign tokens.
   A descriptor-conversion failure closes the native handle and leaves a stale
   lock for inspection; it does not authorize a writer or evict another owner.

Exclusion follows from O_EXCL/FILE_CREATE, not from a probe: every path entering
the critical section owns a successful exclusive creation. A deletion-pending
error returns no descriptor and grants no access. Retries do not write prediction
data. A later success creates a new exclusive lock after the old name is reusable.
ACL errors remain distinct 0xC0000022; arbitrary errno 13 is never enough to wait
or enter. Ownership checks are unchanged except stricter type rejection.

Existing canonical full predictions, append-only versions, atomic staging rename,
producer completion, hash checks and failure cleanup contracts are retained.
All 81 original publication tests plus 18 new lock tests passed locally (99).
Python 3.13 targeted locks plus nine V2 checker tests passed (108). Remote Windows
and Linux each passed 127 rounds / 505 publications; Windows fault tests 18 PASS,
Linux portable fault tests 12 PASS and six explicitly Windows-only SKIP. Remote
native comparisons and deterministic state injections are recorded separately.

Safety limits: no hard real-time guarantee for an OS I/O call or scheduler, no
certification of network filesystems or every Windows version, no complete
external-handle attribution. Stress zero-failure alone is not a correctness proof.
Rollback is a normal revert of the code/CI commit, restoring the known old failure;
it must never permit continuing publication after a lock error.

Historical archive certification, independent human migration review and
historical-risk approval remain blocked/pending. This fix grants no financial use.

**PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED / NO LIVE MONEY**
