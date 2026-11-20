# Linux Demo cloud runtime — NO LIVE MONEY

The systemd service is a GET-only account recovery and market observation process, not an unattended trading worker. It cannot create or cancel exchange orders, create execution authorization, resume an existing Grid deployment, or replenish a campaign. Offline mode does not construct a network client. Docker is not a second supported deployment path.

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

The unit uses systemd `LoadCredential` to deliver a private read-only credential copy; `FACTORLAB_DEMO_SECRET_FILE` contains only its pathname. The binding is separate private state, not the Secret. For an existing account, migrate the existing binding and all journal files through the approved offline state-transfer process; do not fabricate another binding or run setup over a different account. Inspect migrated Linux ownership and modes before starting.

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

The snapshot includes process heartbeat, market/reconciliation success times, market batch age, method counts and failure rate, UNKNOWN count, positions/equity, remaining campaign inputs, global cumulative consumption, desired STOP/HALTED state, SQLite/sidecar sizes, free disk, process starts/restarts and last exception classification. Missing observations are null. Request counters and failure rate are process-scoped; starts/restarts persist across normal process restarts, and are not claimed as independent host reboot accounting. Secrets, signed queries and full binding records are excluded.

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

## Stability observation profiles

Use the same GET-only unit and health timer for 24 hours, 72 hours and seven days. Retain the full observation window's journald records and periodic private health snapshots outside the active state volume. Do not enable execution to conduct a stability observation.

For each window require: zero POST and DELETE; no binding/cursor/budget reset; zero unexpected readiness during failed reconciliation or UNKNOWN; intentional STOP remains effective; disk free stays above reserve; backup restores validate and remain quarantined; every crash/restart is accounted for. Track maximum market and reconciliation age (alert at 90 seconds), request error rate (alert for sustained nonzero rates across five minutes), UNKNOWN immediately, restart bursts immediately, and any integrity/low-space/credential failure immediately. Document network outages separately from process crashes and retain recovery latency. Progress from 24h to 72h to 7d only after investigating each unexpected incident. Passing a startup smoke test is not passing these windows.

Cloud acceptance is separate from offline Linux and systemd evidence. Without an existing authorized host and Demo credential, report `CLOUD_NOT_DEPLOYED`. `CLOUD_DEMO_EXECUTION_READY` remains false until the M19 execution loop and a later cloud execution integration are accepted.
