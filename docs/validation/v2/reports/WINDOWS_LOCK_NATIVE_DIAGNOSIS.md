# Windows native lock diagnosis

Public input `ed2b4c1a0a10afd794c674b14d105b748b01b9c8`; main `afac4f9d4c2b123c0b32787ccd1cf67fe965c723`. Tested candidate `b2b6b472d500b078cc872c3b942c6e1916054512`; actual executed merge `50e94ab96d2ca17cd75fa4dce225b5c834152869` (identical Git tree `23a324933ba447f2516328a6b16b4b426593e3a9`). Evidence index SHA-256 `81a23cb3e3704f8c7b8eb34725d41f4f7e4cf93d6d9c721ee0af43863bf86f9b`. [Remote run 40550674503](https://github.com/Wind-Maker1001/FactorLab/actions/runs/40550674503). Windows Server 2025 / Python 3.14.9; additional Linux/Python 3.14.8 and Windows/Python 3.13.16 evidence are identified in the index.

## Confirmed mechanism and limits

**Mechanism CONFIRMED: STATUS_DELETE_PENDING during exclusive creation. Actor attribution PARTIAL/UNKNOWN.**
The old recorder treats EEXIST as contention but immediately fails on EACCES.
That loses a recoverable Windows deletion state in the CRT's errno conversion.
In a local Windows 11 build 26340 / Python 3.14.0 unmodified-PR 100-round
observation, 97 passed and three failed;
all three recorded creation failures had errno 13, no Python winerror, CRT 5 and
observed NTSTATUS 0xC0000056. The same-condition main run passed 100/100.
Those are separate finite observations, not retries of a failing test.

On the equivalent remote Windows environment, main/input/candidate each passed
100/100 natural four-thread rounds. These zero-failure samples do not negate the
recorded earlier failures. The controlled experiment directly creates a handle
with GENERIC_READ|DELETE and sharing READ|WRITE|DELETE, sets FileDispositionInfo,
and queries FileStandardInfo.DeletePending=True on that handle. While it remains
open, exclusive creation gives errno 13 / CRT 5 / STATUS_DELETE_PENDING on both
unmodified main and input PR. Closing the final handle permits exclusive create.
Old recorders immediately raise SignalLockError; the candidate reaches its
bounded timeout without entering the critical section. A separate synchronized
fault test closes the handle only after the candidate observes pending deletion;
the candidate then publishes one complete version and releases its owned lock.

| Cause | Actual evidence | Finding |
| --- | --- | --- |
| Deletion/creation overlap | Queried native delete-pending flag, known handle lifetime, reproducible create failure and final-handle recovery | Confirmed recoverable mechanism |
| File sharing restriction | Known handle with share mode 3; DeleteFileW returns Win32 32; native exclusive create reports collision | Confirmed distinct state; not retried as deletion pending |
| ACL denial | Actual test-directory deny ACE, create failure STATUS_ACCESS_DENIED 0xC0000022, no retry, ACL restored | Confirmed distinct refusal |
| Security software or unknown external handle | No system-wide ETW/ProcMon handle trace | UNKNOWN; no antivirus root-cause claim |
| Ownership corruption | Explicit foreign/corrupt-token injections refuse release and preserve file | Covered; no evidence it caused original race |
| Crash, timeout, scheduling | Actual child-process os._exit, retained lock, bounded refusal; live owner not evicted | Covered; scheduling is not inferred as original cause |

Instrumentation records relative synthetic lock paths, process/native-thread IDs,
monotonic high-resolution timestamps, operation ordering, original flags, error
classes and CRT/Win32/observed NT status. It does not identify every implicit CRT
or kernel handle, original CRT sharing mode, security filter or historical CI
actor. RtlGetLastNtStatus is observational only. The fix uses a fresh operation's
returned status; it never authorizes a retry using that thread-local observation.

## Invalidated and superseded experiments

Initial sandboxed trials produced 100 failures per subject because the sandbox
refused access to test directories after permission changes. They remain on disk
with hashes in the index and are excluded from software/race conclusions. The
early DeleteFileW example allowed immediate name reuse despite an old handle's
delete-pending flag, reflecting POSIX deletion behavior on that local system.
It was not evidence of a blocking tombstone. Explicit FileDispositionInfo makes
the relevant blocking state deterministic on local and remote Windows. An early
new-test collection import error is retained as fix-r1.xml; its corrected run is
99 PASS. No failed CI run was repeated unchanged to obtain a green outcome.

Microsoft documents pending-delete CreateFile denial and sharing semantics in
[CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew),
and the direct status/exclusive creation interface in
[NtCreateFile](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile).
No third-party implementation was copied.

**PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED / NO LIVE MONEY**
