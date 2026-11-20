# Linux Demo runtime acceptance

NO LIVE MONEY. This acceptance covers the GET-only/offline runtime, not cloud trading execution.

The reproducible Linux/Windows suite and real Linux systemd SIGKILL/SIGTERM acceptance are defined in `.github/workflows/demo-cloud-runtime.yml`. The protected `FactorLab trusted merge gate` remains separate and unchanged. A successful supplemental workflow is not a substitute for that gate.

Windows Python 3.13 evidence during implementation:

- Existing runtime, continuous campaign, Grid supervisor and observability: 61 passed; Credential Manager independent-process case passed after supplying the checkout's `src` on `PYTHONPATH` to the child process.
- New cloud lifecycle and storage tests: 28 passed, four POSIX-only cases skipped. Includes killed SQLite transaction recovery, two-process lock exclusion, preserved STOP epoch, UNKNOWN/reconciliation withholding, method-write refusal, consistent backups, and incomplete-backup rejection.
- Linux credential cases must execute on Linux; Windows skips are not credential acceptance.

## Acceptance states

| State | Current evidence |
| --- | --- |
| LINUX_RUNTIME_ACCEPTED | Pending Linux workflow execution. |
| PERSISTENCE_ACCEPTED | Windows subprocess/SQLite evidence passed; Linux persistence evidence pending. |
| WATCHDOG_ACCEPTED | Pending real systemd restart/stop job. |
| CLOUD_GET_ONLY_ACCEPTED | Not accepted: CLOUD_NOT_DEPLOYED. |
| CLOUD_DEMO_EXECUTION_READY | False: requires M19 execution closure and subsequent cloud execution integration. |

No cloud host or credential was provisioned for this acceptance. No exchange order POST or DELETE is part of the test plan. The 24h/72h/7d observation profiles are documented but have not been executed.

Independent Codex Review is pending: both attempted CLI invocations terminated before review because their selected model was unsupported by the authenticated account. No review pass is claimed.
