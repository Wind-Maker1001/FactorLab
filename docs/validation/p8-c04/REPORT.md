# P8-C04 Validation Report: Linux systemd Execute Wiring, Persistent Control State & Startup Recovery

**NO LIVE MONEY.** All acceptance checks below were executed strictly offline (Windows unit/integration suites + Debian 13 WSL2 real `systemd` with an isolated loopback HTTP stub). No live Binance Demo account or F05 process was touched, and no remote cloud host was deployed (`CLOUD_NOT_DEPLOYED`).

## Scope Completed

- **C04-01 Persistent Control State Preservation**:
  - Removed unconditional `control.request('RUNNING')` overrides from `demo_grid_supervisor.py` and `demo_strategy_supervisor.py`.
  - Added authoritative `durable_control_state(runtime)` (covering continuous `STOPPED`, `HALTED`, latched `continuous_diagnostic`, and legacy `stop_epoch`) checked before control initialization, credentials, or network access.
  - Preserved `PAUSED` across restarts without promoting to `RUNNING` or rotating campaign windows; initialized `RUNNING` only on truly fresh deployments without existing control state or `STOPPED`/`HALTED` runtime state.
- **C04-02 Startup Exception Recovery**:
  - Added `GridSupervisorRuntime` lazy/recoverable wrapper over the existing `open_grid_session`, `GridConsumer`, `DemoMarketFeed`, `DemoBookCollection`, and single `DemoSupervisor` backoff loop (no second retry framework).
  - Performed local non-network preflight upfront (`operator_lock`, SQLite integrity & hot-journal recovery, credentials, binding/account ID, authorization/policy match, saved Grid compatibility).
  - Fatal errors (credentials, identity/binding mismatch, exchange `AUTH`/`SIGNATURE` / HTTP 401/403, SQLite corruption, lock contention, restore quarantine, low disk) fail explicitly with sanitized JSON and exit code `78` (`RestartPreventExitStatus=78`).
  - Transient startup network, clock, staleness, and `UNKNOWN` reconciliation failures enter `DemoSupervisor` exponential backoff without creating duplicate Grid deployments, Campaigns, or ledgers, and with zero order submission during recovery.
- **C04-03 Execute systemd Wiring & Exclusivity**:
  - Added `deploy/systemd/factorlab-demo-execute.service` reusing `/opt/factorlab/venv/bin/factorlab-demo grid-supervisor start --execute`, `/var/lib/factorlab-demo`, `LoadCredential=demo-secret:/etc/factorlab-demo/demo-secret.json`, `UMask=0077`, `Restart=on-failure`, `RestartPreventExitStatus=78`, and bidirectional `Conflicts=` with `deploy/systemd/factorlab-demo.service`.
  - Fixed real-world systemd 252+/257 `LoadCredential` compatibility in `demo_file_credentials.py` by strictly verifying root-owned read-only POSIX ACLs inside `$CREDENTIALS_DIRECTORY` limited solely to root and the current service UID (zero group/world access).
  - Fixed `GridConsumer.quote()` quote-timestamp regression when `DemoBookCollection` delivers a recent cached book immediately after `bridge.recover()`'s direct `_fresh_book()`.
- **C04-04 Health Wiring**:
  - Extended `demo_cloud_runtime.py` and `demo_grid_supervisor.py` to share `/var/lib/factorlab-demo/cloud-health.json` across `OFFLINE`, `GET_ONLY`, and `EXECUTE` modes with zero changes required to `factorlab-demo-health.service` / `.timer`.
  - Reported real `systemd` unit properties (`systemctl show`), `mode`, `last_reconciled_at`, `last_valid_market_at`, `grid` state, `positions`/`equity`/`nav_usdt`, `GET`/`POST`/`DELETE`, campaign & global remaining budgets, `desired`/`status`/`diagnostic`, `recent_orders`/`unknown_orders`/`unknown_count`, and `recovery` state.
  - `demo_execution_ready` / `execute_healthy` / `ready` require all real account, market, Grid, budget, control, UNKNOWN=0, and PID-matching preconditions and are never promoted by `systemd active` alone.

## F01 Integration

PR #222 was rebased from `7160d557bc6d5ea262d97c29b1ce3fd1a157fab9` onto `7ceae2e89d1f5765eb8a74f7142a122b478a9f64` (F07, PR #221). The only Git conflict was in `demo_grid_supervisor.py`. The resolved worker keeps C04's durable control, local startup checks, delayed session recovery and health reporting. It hands the account refresh callback to the F07 market feed, so a live batch reads its closed Kline before refreshing account facts and delivering the bar and book. An existing automatic Grid retains its local journals and validates the bound configuration on open. Its first account reconciliation runs in the market callback. A new Grid still obtains the initial facts it needs to construct the deployment.

The bridge reuses the background book while requiring its quote timestamp to meet the controller's current watermark. Execute health ages the book against calibrated exchange time and the account facts against the exchange clock and the monotonic read start. A successful heartbeat can still be alive when either input exceeds five seconds, at which point it is unready for trading.

During the first Debian systemd run, a worker STOP issued immediately after the GET-only to Execute switch coincided with startup SQLite inspection and caused exit 78. Holding the control write lock reproduced `SQLITE_BUSY` with the old zero timeout. Writable startup integrity checks now allow up to five seconds for the control transaction to commit. The integrity check still runs and corruption still stops the unit with exit 78.

The [original Ubuntu jobs](https://github.com/Wind-Maker1001/FactorLab/actions/runs/44936861016) failed in the LoadCredential ACL test. The original test also failed in isolation on Debian WSL2 with CPython 3.14.10 and pytest 9.2.0. Its process-wide `os.fstat` patch returned a `SimpleNamespace` without `st_size`, which escaped the credential checks and broke context cleanup. The fixture now supplies a complete `os.stat_result` with modified mode, UID and link count through the credential module's OS reference. It still checks acceptance of the single-service-UID ACL and rejection of extra user/group access and a mismatched credential directory.

The [PR #222 checks](https://github.com/Wind-Maker1001/FactorLab/pull/222/checks) bind remote results to the pushed head. The offline matrix covers Windows and Ubuntu with Python 3.13 and 3.14. The trusted workflow includes the existing `ci-control-review` for the CI workflow and harness changes and publishes `FactorLab trusted merge gate` through the configured GitHub App.

| F01 local regression | Result |
| --- | --- |
| Windows, Python 3.12.7 | 294 passed |
| Debian WSL2, Python 3.13.18 | 305 passed, 1 Windows-only test skipped |
| Debian WSL2, Python 3.14.10 | 305 passed, 1 Windows-only test skipped |
| Storage and Execute retest after the SQLite timeout fix | 33 passed on each Debian Python version |

The suite covered Execute, cloud runtime and storage, credential ACLs, Grid and strategy supervision, GET recovery and window rollover, market collection, continuous campaigns, network signing, constructor clock, and observability. The standalone ACL test passed on both Debian Python versions after the fixture fix. The standalone SQLite overlap test confirmed that integrity inspection waits for the STOP write to commit and reads the preserved STOPPED value.

The F01 rerun of `tools/ci/demo_execute_systemd.py` completed on 2026-11-22 (Asia/Shanghai) with Debian 13 WSL2, systemd 257 and Python 3.13.18. Both unit files passed `systemd-analyze verify`, and all 11 loopback checks passed. SIGTERM stopped the service with exit 0, and SIGKILL produced a new PID with the same deployment and four intact SQLite databases. The two recorded POSTs matched the exchange stub, eight budgeted POSTs remained, and GET cleared the UNKNOWN order without a duplicate submission. The run also passed durable STOP/HALT/PAUSE, in-process outage recovery, GET-only/Execute switching, duplicate-process refusal, LoadCredential, secret scanning and corrupt SQLite exit 78. Cloud status is `CLOUD_NOT_DEPLOYED`.

## Original C04 Verification Evidence (before F01)

### 1. Windows Python 3.12 Offline Test Suite

- `tests/integrations/test_demo_execute_systemd.py` + `tests/integrations/test_demo_grid_supervisor.py`: **51 passed, 0 failed**.
- Existing Demo integration regression (`test_demo_cloud_runtime.py`, `test_demo_cloud_storage.py`, `test_demo_cloud_credentials.py`, `test_demo_continuous_campaign.py`, `test_demo_strategy_supervisor.py`, `test_binance_demo_runtime.py`, `test_demo_observability.py`, `test_demo_get_recovery.py`, `test_demo_market_collection.py`): **114 passed, 0 failed**.

### 2. Real Debian 13 Linux + systemd Acceptance (`tools/ci/demo_execute_systemd.py`)

Environment:
- OS/Kernel: `Debian GNU/Linux 13 (trixie)`, `Linux 6.6.87.2-microsoft-standard-WSL2`
- systemd: `systemd 257 (257.13-1~deb13u1)`
- Python: `Python 3.13.18` (`/opt/factorlab/venv`, root-owned `go-w`)
- Production unit verification: `systemd-analyze verify` **PASSED** on both `factorlab-demo-execute.service` and `factorlab-demo.service`.

All 10 C04-05 checks + corrupt-SQLite check **PASSED** (`all_passed: true`):

| # | Check | Result | Key Observed Evidence |
| --- | --- | --- | --- |
| 1 | `RUNNING` normal start | PASSED | `LoadCredential` loaded, `systemd.active_state=active`, `demo_execution_ready=true`, 1st LIMIT BUY submitted on fresh closed bar (`posts_consumed=1`, `remaining_posts=9`) |
| 2 | Durable `STOP` preserved | PASSED | Both `grid-supervisor stop --execute` and `factorlab-demo stop` exited `0`, `NRestarts=0`, zero network calls on subsequent `systemctl start` |
| 3 | Durable `HALTED` preserved | PASSED | Risk `HALTED` exited `0`, `NRestarts=0`, `diagnostic` preserved, zero network calls on `systemctl start` |
| 4 | `PAUSED` preserved | PASSED | Survived `systemctl restart` with `desired=PAUSED`, `demo_execution_ready=false`, resumed cleanly on explicit `resume --execute` |
| 5 | Startup outage & auto recovery | PASSED | Outage at `systemctl start` stayed active in `RECOVERING` (`NRestarts=0`); auto-recovered in-place (`pid_unchanged=true`, `NRestarts=0`, `state=RECOVERED`, same `deployment_id`) |
| 6 | `SIGKILL` same ledger recovery | PASSED | `SIGKILL` (`PID 9809 -> 10949`, `NRestarts=1`, `starts=3`), same `deployment_id`, all 4 SQLite DBs `PRAGMA integrity_check == ok` |
| 7 | Budget preservation | PASSED | `consumed={posts: 2, submitted_notional: "12.000"}`, `remaining_posts=8`, exact match with `exchange_posts=2` across all restarts/SIGKILL/STOP/outages |
| 8 | `UNKNOWN` no resubmission | PASSED | Dropped POST #2 reply -> `UNKNOWN` -> `SIGKILL` -> restart GET reconciled `unknown_count=0`, `exchange_post_count=2` (zero duplicate POST) |
| 9 | Dual-process & switchover exclusivity | PASSED | Concurrent Execute exit `78`, concurrent GET-only exit `78`, systemd `Conflicts=` switched cleanly between GET-only and Execute |
| 10 | No credential leakage | PASSED | Zero occurrences of API key/secret sentinels across `journalctl`, `cloud-health.json`, `runtime.sqlite3`, and `grid-supervisor.sqlite3` |
| + | Corrupt SQLite fail-fast | PASSED | `ExecMainStatus=78`, `NRestarts=0` (`RestartPreventExitStatus=78`) |

## Cloud Deployment Status & Objective Blockers

- **Cloud Host Deployment**: `CLOUD_NOT_DEPLOYED`. No remote Linux cloud host or administrative SSH access is provisioned in this environment; all Linux/systemd verification ran on local Debian 13 WSL2 (`systemd 257`), and production-ready systemd units are delivered in `deploy/systemd/`.
- **Official Demo Online Handover**: Live official Demo switchover requires coordinating exclusive account ownership with any active F05 session and provisioning the real Demo credential file `/etc/factorlab-demo/demo-secret.json` (`root:root 0600`) plus migrated private state root on the target Linux host.
