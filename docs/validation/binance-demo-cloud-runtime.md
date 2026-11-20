# Linux Demo runtime acceptance

NO LIVE MONEY. This acceptance covers the GET-only/offline runtime, not cloud trading execution.

The reproducible Linux/Windows suite and real Linux systemd SIGKILL/SIGTERM acceptance are defined in `.github/workflows/demo-cloud-runtime.yml`. The protected `FactorLab trusted merge gate` remains separate and unchanged. A successful supplemental workflow is not a substitute for that gate.

Windows Python 3.13 evidence during implementation:

- Existing runtime, continuous campaign, Grid supervisor and observability: 61 passed; Credential Manager independent-process case passed after supplying the checkout's `src` on `PYTHONPATH` to the child process.
- Cloud credential/lifecycle/storage regression run: **34 passed, zero skipped**. Platform-specific cases are collected only on their applicable platform. Windows exercises a real Credential Manager round trip in a unique namespace, including reopen, secret rotation, binding preservation and deletion. Includes killed SQLite transaction recovery, two-process lock exclusion, preserved STOP epoch, UNKNOWN/reconciliation withholding, method-write refusal, consistent backups, incomplete-backup rejection, fatal refusal classification, long-downtime cursor catch-up, strategy STOP precedence, progressive market staleness and disappearing-sidecar races.

[Offline CI run 44714569193](https://github.com/Wind-Maker1001/FactorLab/actions/runs/44714569193), commit `65522e6ef698b98f3f408d69923a52f5a0cdbe14`:

- Linux Python 3.13: **102 passed, 1 skipped** (Windows Credential Manager).
- Windows Python 3.13: **91 passed, 12 skipped** (Linux Secret/permission and POSIX signal cases).
- Real systemd service: **SIGKILL automatic restart passed; SIGTERM normal stop passed**.
- This runner executed the Linux Secret backend, private-permission checks, real subprocess locks and SQLite crash recovery; these are not inferred from Windows skips.

The repository's protected trusted merge gate is evaluated separately; supplemental offline success does not establish that gate's status.

## Acceptance states

| State | Current evidence |
| --- | --- |
| LINUX_RUNTIME_ACCEPTED | Accepted for commit 65522e6: Linux offline suite passed. |
| PERSISTENCE_ACCEPTED | Accepted for commit 65522e6: Linux/Windows crash, backup, STOP and budget-preservation cases passed. |
| WATCHDOG_ACCEPTED | Accepted for commit 65522e6: real systemd restart/stop job passed. |
| CLOUD_GET_ONLY_ACCEPTED | Not accepted: CLOUD_NOT_DEPLOYED. |
| CLOUD_DEMO_EXECUTION_READY | False: requires M19 execution closure and subsequent cloud execution integration. |

No cloud host or credential was provisioned for this acceptance. No exchange order POST or DELETE is part of the test plan. The 24h/72h/7d observation profiles are documented but have not been executed.

