# Linux Demo cloud runtime — NO LIVE MONEY

This deployment covers two mutually exclusive systemd units over the same private persistent state root (`/var/lib/factorlab-demo`):

- `factorlab-demo.service`: GET-only account recovery and market observation (`demo_cloud_runtime run`). It cannot create or cancel exchange orders. Offline mode does not construct a network client.
- `factorlab-demo-execute.service`: Existing Binance Demo Grid Execute worker (`factorlab-demo grid-supervisor start --execute`), retaining the existing `DemoRuntime`, `DemoSupervisor`, `TradingController`, `DemoExecutionGateway`, `DemoGridBridge`, persistent budgets, account binding, UNKNOWN protection, 5-second account/market freshness bounds, and continuous Campaign window rotation.

The two units declare bidirectional `Conflicts=` and share `/var/lib/factorlab-demo/session.lock` (`flock`) so GET-only and Execute cannot simultaneously hold ownership of the same account. Docker and Kubernetes are not supported deployment paths.

## Storage and installation

Use Python 3.13 or 3.14 on a Linux host running systemd. Install the reviewed release into `/opt/factorlab/venv` using `python -m venv` and `venv/bin/python -m pip install /path/to/release`. Keep application code owned by root and not writable by the service user.

Provision the service identity and private persistent directory before starting:

```sh
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin factorlab-demo
sudo install -d -o factorlab-demo -g factorlab-demo -m 700 /var/lib/factorlab-demo
sudo install -d -o root -g root -m 700 /etc/factorlab-demo
```

`FACTORLAB_DEMO_STATE_ROOT` must name an existing absolute private directory. Linux has no fallback to a checkout, `/tmp`, `$HOME`, or container layer. Windows without an override continues to use `%LOCALAPPDATA%/FactorLab/BinanceDemo` and Windows Credential Manager.

`/var/lib/factorlab-demo` must live on persistent local storage. If using a separate data disk, provision its mount in `/etc/fstab` before installing the service. The unit requires that path's backing mounts and deliberately does not create the directory. Do not substitute ephemeral instance disks. Only single-host local filesystems supporting SQLite and `flock` are supported; do not put the state on NFS or use separate copies for competing workers. Two containers using this same local volume contend on the same lock; unrelated volumes cannot enforce account exclusivity.

Keep the entire root together: `runtime.sqlite3`, deterministic account journal directories containing `protocol.sqlite3` and `controller.sqlite3`, `grid-supervisor.sqlite3`, market cursors, private binding metadata, and any SQLite sidecars. Never delete `session.lock` to fix contention. Never remove a `-journal`, `-wal`, or `-shm` file to repair an active database. Do not initialize a replacement root when the expected disk is missing.

## Demo Secret injection and binding

Provision `/etc/factorlab-demo/demo-secret.json` through a protected administrative editor or your existing secret delivery mechanism, root-owned mode 0600. Do not put real values in shell arguments, environment files, Git, CI, logs, or support reports. Its schema is JSON with explicit `environment: "DEMO"`, `key`, and `secret` fields. Production credentials are not accepted as a different environment, and requests always target the fixed official Demo host.

The unit uses systemd `LoadCredential` to deliver a private read-only credential copy; `FACTORLAB_DEMO_SECRET_FILE` contains only its pathname. `DemoFileCredentialStore` enforces regular non-symlink single-link files owned by the service user with mode `0600`, or root-owned read-only systemd `LoadCredential` mounts inside `$CREDENTIALS_DIRECTORY` whose Linux POSIX ACL strictly limits read access to root and the current service UID (zero group or world access). The binding is separate private state, not the Secret. For an existing account, migrate the existing binding and all journal files through the approved offline state-transfer process; do not fabricate another binding or run setup over a different account. Inspect migrated Linux ownership and modes before starting.

For a previously provisioned dedicated Demo identity with no existing binding, perform the existing **GET-only** setup command as the service user with the explicit root and Secret path; setup observes the account before binding. Do not run `enable-demo`, continuous authorization, agreement signing, Grid execution, or any order command as part of this installation.

```sh
sudo -u factorlab-demo env \
  FACTORLAB_DEMO_STATE_ROOT=/var/lib/factorlab-demo \
  FACTORLAB_DEMO_SECRET_FILE=/path/to/service-readable-private-demo-secret.json \
  /opt/factorlab/venv/bin/python -m factorlab.integrations.binance_futures_trading.demo_cli setup
```

The temporary service-readable file must be 0600 and owned by the service user; remove it using the secret delivery mechanism after setup. The service receives its own copy from systemd. Missing credentials, invalid permissions or binding mismatch refuse startup. Environment configuration does not grant writes.

Rotate the externally provisioned Secret atomically, preserving binding and all databases. The existing identity is API-Key-digest-bound: a different key fails the existing identity check rather than silently opening fresh journals or resetting budgets. A new-key identity migration is not a runtime restart operation; keep the previous evidence and arrange a separately reviewed migration before changing that identity. Do not delete the binding to make a rotation pass.

## Service management

Install the units from `deploy/systemd/`, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now factorlab-demo.service factorlab-demo-health.timer
sudo systemctl status factorlab-demo.service
sudo systemctl stop factorlab-demo.service
sudo systemctl restart factorlab-demo.service
sudo journalctl -u factorlab-demo.service
```

For an offline installation, add a systemd drop-in replacing `ExecStart` with the same command plus `--offline`, and clear `LoadCredential=`. Do not use that override to inject execution flags; the cloud CLI has none.

`systemctl stop factorlab-demo` sends SIGTERM, permits database close, and does not request exchange cancellations or change persisted trading controls. `disable --now` prevents boot start. An operator trading STOP is the existing `factorlab-demo stop` command with the same state root; it remains independent of the Worker lock. A durable STOP/HALTED is detected before credentials/network and exits successfully; `Restart=on-failure` does not revive it. Even a manual service start does not clear it. Ordinary SIGTERM produces EXITED, not a newly persisted operator STOP.

Crashes restart after 15 seconds, at most five starts in five minutes. Configuration, identity, permission, integrity and low-space failures exit 78 and do not restart automatically. Fix the underlying condition before starting again; never clear journals as a restart procedure. Startup replays existing events and GET-reconciles the account before publishing readiness. UNKNOWN is never re-POSTed.

## Backup and recovery

Stop all processes and maintenance commands using the root, including any separate previews, then run backup as the state owner:

```sh
sudo systemctl stop factorlab-demo
sudo -u factorlab-demo env FACTORLAB_DEMO_STATE_ROOT=/var/lib/factorlab-demo \
  /opt/factorlab/venv/bin/python -m factorlab.integrations.binance_futures_trading.demo_cloud_runtime \
  backup --destination /persistent/backups/unique-backup-name
```

The destination must not exist and must be outside the state root. The backup holds the account lock and SQLite write reservations across all databases before using SQLite's backup API. This also excludes concurrent STOP writes during the snapshot. It validates SQLite integrity and publishes a completed backup atomically. Secrets are excluded. The maintenance window must also exclude creation of new control databases or external edits of binding files; stop all writers, not only the cloud service. Normal operation may restart after backup completes.

Restore with `restore --backup /persistent/backups/unique-backup-name --destination /new/private/state-root`. Restore does not overwrite an existing root. It validates the completed backup and preserves account binding, order events, cursor and budget evidence. A persistent `restore-quarantine.json` restricts ordinary session opening: the restored root can be inspected and cloud GET-recovered, but not used for execution.

An old snapshot cannot prove there were no later orders, budget reservations, or STOP changes. GET reconciliation does not reconstruct missing cumulative risk evidence. There is deliberately no command to remove restore quarantine in this release. Retain the surviving original state and backup evidence; future execution recovery requires a separately reviewed high-water reconciliation, not deletion of this marker.

## Health and response

The Worker atomically publishes private `cloud-health.json`. Read it with the `health` action, which makes no network requests, initializes no database, and does not obtain a trading lock. Exit 0 means a recent ready heartbeat (explicit OFFLINE is identified as such); exit 1 means stopped, stale or not ready. The timer produces a failed health service/journald signal for operational alert routing. Connect those existing OS events to your organization's alert delivery rather than installing a separate web platform.

The snapshot includes process startup time, per-endpoint request counts/error categories and total/last/maximum request latency (`api`), last account recovery duration, process-local `CLOCK_SKEW` request failures and `STALE_POLL`/`R13_QUOTE_GAP` pause counts (`safety_events`), process heartbeat, market/reconciliation success times, market batch age, method counts and failure rate, UNKNOWN count, positions/equity, remaining campaign inputs, global cumulative consumption, desired STOP/HALTED state, journal event high-water marks, SQLite/sidecar sizes, free disk, process starts/restarts and last exception classification. Missing observations are null. Request counters and failure rate are process-scoped; starts/restarts persist across normal process restarts, and are not claimed as independent host reboot accounting. Secrets, signed queries and full binding records are excluded.

`health` reports elapsed uptime for the current heartbeat, `market_fresh` and `account_recovered` independently. `STALE_ACCOUNT` counts recovery rounds refused for an expired account read set, including during session construction. Account recovery can succeed in a round that subsequently fails to collect market data. A failed account recovery or an expired heartbeat/reconciliation suppresses current positions, equity and `nav_usdt`. NAV uses the recovered USDT wallet balance plus unrealized profit. `sqlite_integrity_checked_at` records the startup check of the existing databases. The account freshness threshold here is for observation; order admission uses its own freshness rules.

Use the OS state alongside the heartbeat. systemd's `NRestarts` counts automatic restarts for the current service activation. The health snapshot's cumulative starts include manual starts.

```sh
sudo systemctl show factorlab-demo.service \
  -p ActiveState -p SubState -p MainPID -p NRestarts -p ExecMainStartTimestamp -p ExecMainStatus
sudo -u factorlab-demo /opt/factorlab/venv/bin/python \
  -m factorlab.integrations.binance_futures_trading.demo_cloud_runtime \
  health --state-root /var/lib/factorlab-demo
```

| Classification | Response |
| --- | --- |
| Network interruption/exchange failure | Existing bounded exponential backoff, GET-only retry; investigate sustained failures. |
| Market stale/invalid | Not ready, retain cursor and GET recovery; no strategy dispatch. |
| Reconciliation failure / UNKNOWN | Not ready, continue GET recovery; do not resubmit, cancel speculatively, or erase journals. |
| PAUSED | Preserve desired control; process presence does not mean permission to trade. |
| Budget exhausted / HALTED | Preserve cumulative evidence; this service cannot replenish or authorize it. |
| Operator STOP | Successful process exit; no automatic supervisor restart. |
| Low disk / corrupt SQLite / unsafe permissions | Refuse writes/start; free unrelated space or recover into a separate root, preserving original evidence. |
| Stale heartbeat / failed service | Check journal and exact exit category; do not interpret absence as healthy. |

The default free-space threshold is 256 MiB, checked before network recovery/journal activity each poll; tune `--min-free-bytes` to expected data growth. It is a reserve threshold, not a filesystem quota or guarantee against concurrent disk consumption. Keep backups on separately provisioned persistent storage.

## Execute service wiring (P8-C04)

`deploy/systemd/factorlab-demo-execute.service` runs `/opt/factorlab/venv/bin/factorlab-demo grid-supervisor start --execute` under the same `factorlab-demo` user, `/var/lib/factorlab-demo` state root, `LoadCredential=demo-secret:/etc/factorlab-demo/demo-secret.json`, `UMask=0077`, hardening directives, `Restart=on-failure`, `RestartSec=15`, and `RestartPreventExitStatus=78`.

Control state is read authoritatively from the persisted SQLite databases (`runtime.sqlite3` and `grid-supervisor.sqlite3`):

- Startup never overwrites persisted `STOPPED`, `HALTED`, or `PAUSED` to `RUNNING`. Initial `RUNNING` state is created only when no control row exists yet and the runtime is neither `STOPPED` nor `HALTED`.
- Durable `STOPPED` or `HALTED` is detected before credential/network access and exits `0` cleanly without modifying budgets or order history; `Restart=on-failure` does not revive it.
- Persisted `PAUSED` remains `PAUSED` across restarts (GET monitoring only, no window creation/rotation, no order submission) until explicit `factorlab-demo grid-supervisor resume --execute`.
- Recoverable startup network, clock calibration, staleness, and UNKNOWN reconciliation failures enter the existing `DemoSupervisor` exponential backoff and recovery loop without creating duplicate Grid deployments, Campaigns, or ledgers, and without submitting orders during recovery.
- Fatal credential, binding/identity, exchange `AUTH`/`SIGNATURE`, SQLite corruption, lock contention, restore-quarantine, and low-disk errors fail explicitly with exit code `78` (`RestartPreventExitStatus=78`) and sanitized JSON refusal logs containing zero secrets.

On the same Linux state root, switch from GET-only to Execute after coordinating exclusive account ownership with any external runner.

```sh
sudo systemctl disable --now factorlab-demo.service
sudo systemctl enable --now factorlab-demo-execute.service factorlab-demo-health.timer
```

The existing `factorlab-demo-health.service` and `factorlab-demo-health.timer` reuse `/var/lib/factorlab-demo/cloud-health.json` for `OFFLINE`, `GET_ONLY`, and `EXECUTE` modes. `demo_cloud_runtime health` reports real systemd unit state (`systemctl show`), mode, last valid account reconciliation and market timestamps, Grid state, positions/wallet/NAV (suppressed when account recovery is not fresh), GET/POST/DELETE counts, remaining campaign and global budgets, desired `STOPPED`/`HALTED`/`PAUSED`/`RUNNING` state, recent orders and UNKNOWN orders, and automatic recovery state. `ready` / `execute_healthy` / `demo_execution_ready` are never inferred from systemd `active` alone.

## Transfer an existing Windows Execute owner to Linux

Use this procedure with the same official Demo API identity and the existing continuous authorization. Ordinary `backup` and `restore` remain disaster-recovery operations. A restored root stays quarantined.

1. Prepare the service identity, package, units, persistent local disk and `LoadCredential` as above. Keep both worker units stopped and disabled on the receiver. In this example, `/var/lib/factorlab-demo` is the private parent directory and `/var/lib/factorlab-demo/execute-state` is the absent destination. Set `Environment=FACTORLAB_DEMO_STATE_ROOT=/var/lib/factorlab-demo/execute-state` in a drop-in for each worker and the health service. Their existing mount and write-path settings cover this subdirectory. Obtain the target's `/etc/machine-id` over the verified administrative connection. Keep that identity private.
2. Stop the Windows Execute process without setting a trading STOP. Disable any scheduler, restart wrapper or outstanding runner that can restart it. Verify exit and stop all maintenance writers and previews using its state. Retain the original and take a separate ordinary backup before exporting. Export from the original state as the Windows user that owns the Credential Manager binding. Supply the actual target machine ID in `$targetMachineId`.

   ```powershell
   python -m factorlab.integrations.binance_futures_trading.demo_cloud_runtime handoff-export --destination "$env:LOCALAPPDATA\FactorLab\p8-c05-transfer" --target-machine-id $targetMachineId --target-state-root /var/lib/factorlab-demo/execute-state
   ```

3. Export freezes the SQLite writers, snapshots every database, and reads the non-secret binding from Credential Manager. It durably seals the Windows state before publishing `execute-handoff.json`. Keep the sealed Windows directory and disable its launchers after the transfer. If release publication fails after sealing, rerun the same command with the same arguments to publish the original package. The seal leaves the trading controls intact. While the source is sealed and the target has yet to pass GET, both Execute workers stay off.
4. Transfer the entire package via authenticated SSH to a private directory owned by the Linux service user. Keep directories mode 0700 and files mode 0600. Provision the original key pair separately through the existing systemd credential mechanism. On the target, run the following transient unit with the same service identity and credential delivery as Execute.

   ```sh
   sudo systemd-run --wait --pipe --collect --unit=factorlab-demo-handoff \
     --property=User=factorlab-demo --property=Group=factorlab-demo \
     --property=UMask=0077 \
     --property=LoadCredential=demo-secret:/etc/factorlab-demo/demo-secret.json \
     /bin/sh -c 'export FACTORLAB_DEMO_SECRET_FILE="$CREDENTIALS_DIRECTORY/demo-secret"
       exec /opt/factorlab/venv/bin/python -m factorlab.integrations.binance_futures_trading.demo_cloud_runtime \
         handoff-import --package /var/lib/factorlab-demo/incoming \
         --destination /var/lib/factorlab-demo/execute-state'
   ```

5. Import verifies the complete snapshot before opening the account using the existing signed GET recovery path. It recovers UNKNOWN by original client order ID and reconciles the existing order, fill and funding ledgers. A failed GET or unresolved order leaves the destination absent and can be retried against the same package. Successful import atomically publishes the destination with `execute-handoff-result.json`. Retain the original, package and receipt privately. Never replay the package to replace a ledger that has already taken ownership.
6. Match the unit arguments to the existing Grid configuration in the import receipt. If the source used a custom interval, backfill limit or Grid settings, use an `ExecStart` override with those same values and the existing `grid-supervisor start --execute` command. Keep both account access and control commands pointed at the new state path. Reload systemd and enable `factorlab-demo-execute.service` and `factorlab-demo-health.timer` only after successful import. The service inherits STOPPED, HALTED and PAUSED. A confirmed recovery can therefore coexist with a deliberate trading stop.
7. Retain the target's unit status, PID and `NRestarts`, the first healthy Execute snapshot, and the result of a controlled systemd restart test. Keep persistent journald records for the 24-hour, 72-hour and seven-day windows. Use the timer snapshots for Worker, account and market freshness, request counts, positions, equity, Grid, budget, UNKNOWN, restart and storage metrics. Use the protocol and controller journals for complete order, fill, funding and fee records. Keep financial snapshots and account identifiers private. Observe the windows while the existing authorization and risk controls govern Execute.

After sealing, resolve a failed transfer from the retained package and the designated receiver. After Linux takes ownership, returning to Windows requires a separate handoff of the latest surviving ledger. The Windows pre-transfer directory is a historical record.

## Stability observation profiles

For GET-only deployments, use the GET-only unit and health timer for 24 hours, 72 hours and seven days. Retain the full observation window's journald records and periodic private health snapshots outside the active state volume.

For each window require: zero POST and DELETE; no binding/cursor/budget reset; zero unexpected readiness during failed reconciliation or UNKNOWN; intentional STOP remains effective; disk free stays above reserve; backup restores validate and remain quarantined; every crash/restart is accounted for. Track maximum market and reconciliation age (alert at 90 seconds), request error rate (alert for sustained nonzero rates across five minutes), UNKNOWN immediately, restart bursts immediately, and any integrity/low-space/credential failure immediately. Document network outages separately from process crashes and retain recovery latency. Progress from 24h to 72h to 7d only after investigating each unexpected incident. Passing a startup smoke test is not passing these windows.

## C02 preflight and account handover

Before provisioning or transferring anything, retain a private inventory with the approved host identifier, Linux release/kernel, systemd version, Python 3.13/3.14 version, reviewed commit and installed package version, service user, mount source/type, free bytes/inodes, log capacity, operation owner and maintenance window. Useful read-only commands on the proposed host:

```sh
uname -sr
cat /etc/os-release
systemctl --version
/opt/factorlab/venv/bin/python --version
/opt/factorlab/venv/bin/python -m pip show factorlab
findmnt -T /var/lib/factorlab-demo -o TARGET,SOURCE,FSTYPE,OPTIONS
df -h /var/lib/factorlab-demo /var/log
df -i /var/lib/factorlab-demo /var/log
stat -c '%U:%G %a %n' /opt/factorlab /opt/factorlab/venv /var/lib/factorlab-demo
sudo -u factorlab-demo test ! -w /opt/factorlab/venv/bin/python
systemctl show factorlab-demo -p User -p Group -p MainPID -p ActiveState -p NRestarts -p ExecMainStartTimestamp -p ExecMainStatus
journalctl --disk-usage
```

Confirm the backing disk survives host reboots; a filesystem name alone does not prove persistence. Reject NFS/shared or ephemeral ledger storage. Record only the Secret's presence/ownership/mode, not its contents. A public endpoint connectivity test does not prove the bound account is usable. Confirm official Demo connectivity and binding only after the account owner grants the exclusive GET-only window; retain redacted identity-match results, not the binding itself.

Before moving historical state, the Windows/F04/F05 owner must stop all account access, scheduled restarts and maintenance writers and explicitly transfer ownership. A Linux flock cannot exclude a separate Windows state copy. Validate the surviving original ledger and complete the existing coherent backup procedure before transfer. Restore into a new private root, retain the original and restore quarantine, verify the original account/journal identity and cursor/budget/STOP high-water marks privately, then GET-reconcile. Do not run setup against a replacement account or delete quarantine to resume execution. If any original evidence or exclusivity is unavailable, account acceptance is BLOCKED.

For handover to F05, stop and disable the C02 service and health timer, close maintenance and manual processes, verify service inactivity and process exit, and record the operation-owner transfer. Do not infer release from a stale heartbeat or delete the lock file. F05 must separately establish its own exclusive ownership before accessing that account.

## C02 observation evidence and incident record

The health command retains legacy `ready` for the OS exit check but separately reports `offline_heartbeat_healthy` and `get_only_healthy`. `cloud_get_only_accepted` and `demo_execution_ready` remain false: a heartbeat cannot certify deployment history, a completed observation window or execution permission. Cloud acceptance is an operator-reviewed evidence result, never an automatic readiness promotion.

The existing health timer writes its JSON to journald, so use its retained snapshots rather than adding a collector/database. Configure sufficient persistent journal retention before the window; retain records on separately provisioned persistent storage, with restricted access because account balances/positions are private. At each window boundary capture:

```sh
sudo journalctl --no-pager --output=json --since '<window-start-UTC>' --until '<window-end-UTC>' -u factorlab-demo.service -u factorlab-demo-health.service
sudo systemctl show factorlab-demo.service -p MainPID -p ActiveState -p NRestarts -p ExecMainStartTimestamp -p ExecMainStatus
sudo -u factorlab-demo env FACTORLAB_DEMO_STATE_ROOT=/var/lib/factorlab-demo /opt/factorlab/venv/bin/python -m factorlab.integrations.binance_futures_trading.demo_cloud_runtime health
```

Use UTC start/end and elapsed wall time, not sample counts, to prove 24h/72h/7d. Preserve all unhealthy samples and missing intervals. Health snapshots contain account recovery duration, last successful market/reconciliation times, current market age, UNKNOWN, desired STOP/HALTED, SQLite and disk sizes. Record host/installed revision and OS service restarts separately. For each process segment, derive GET success rate from endpoint count/error deltas and use the retained maximum request latency as the tail bound; do not call it p95/p99. Request and safety counters reset on process restart: segment by PID/startup time, retain each terminal/high-water sample and mark abrupt-exit losses unknown rather than claiming exact whole-window totals. In particular, zero sampled POST/DELETE is supported by the transport write-refusal regression, not by assuming no unobserved requests occurred.

For each incident record UTC onset/detection/recovery, last successful reconciliation/quote, classification and counter deltas, process/PID/restart changes, GET-only boundary, cursor/journal high-water marks, disk/log growth, operator action and evidence references. Record account contention explicitly. Alerts: market/reconciliation age over 90 seconds, sustained request errors over five minutes, UNKNOWN, restart bursts and credential/integrity/space refusals. CLOCK_SKEW is an observed request failure count, not a count of all clock calibration events. STALE_POLL/R13_QUOTE_GAP count supervisor pauses. Protocol recovery defects are evidence for F04, not changes to signed retry here.

24h requires the first complete real cloud GET-only window, accounted-for interruptions/restarts, reconciliation/quote continuity, recovery latency, intact SQLite/cursors and zero writes. Schedule controlled fault experiments separately and label their intervals. Begin 72h only after reviewing 24h incidents; additionally compare state/log growth, backlog catch-up, intermittent outages and repeated reconciliation cost. Begin 7d only after 72h acceptance; review retention, disk trends, account drift and multiple restart recovery. At each completed window perform the offline coherent backup and quarantine-preserving restore validation in a declared maintenance window. A missing interval, unknown request loss or unexplained state drift must be resolved before acceptance; never fill it with offline CI evidence.

Cloud acceptance is separate from offline Linux and systemd evidence. Without an existing authorized host and Demo credential, report `CLOUD_NOT_DEPLOYED`. `CLOUD_DEMO_EXECUTION_READY` remains false until the M19 execution loop and a later cloud execution integration are accepted.
