# Azure deploy3: reproducible account recovery and shared-mailbox backup

Baseline: `a74fec9404e4c4ff0f699595d7ad5a6b03d1ec45`. Formal release: `/opt/factorlab/releases/a74fec9404e4c4ff0f699595d7ad5a6b03d1ec45-deploy3`.

[Deployment capture](deployment.json) records the installed service commands, backup drop-in and read-only Execute projection. The five changed production modules were read from the formal release's installed package and compared as UTF-8 source against this revision; all match. Execute and Observer both use deploy3, remain active and report zero automatic restarts. Execute is PAUSED with `AUTHORIZATION_REQUIRED`, account recovered, UNKNOWN=0, no positions or open orders. Its captured process counters are GET=1782, POST=0 and DELETE=0. The cumulative 100/100 POST budget and submitted notional `635.9740000` remain unchanged.

## Existing 14-database isolated recovery

[Backup and restore result](backup-restore.json) is the existing C07 result, started 2026-11-24 18:58:59 UTC and completed 18:59:48 UTC. Backup `backup-20261124T185859Z` succeeded and verified all 14 SQLite databases, including `external/task-mailbox/tasks.sqlite3`. The recorded isolated restore completed at 18:59:38 UTC under `/var/backups/factorlab-demo/.restore-drill-6um2lw92/restored-state`; the temporary restored directory was subsequently removed by the drill.

All recorded comparisons passed: database inventory and integrity, binding, budgets, durable control, controller, orders, funding continuity, shared TaskMailbox contents and secret exclusion. The mailbox contains two tasks and six receipts; its content digest matches the isolated restored snapshot. The restore is quarantined for GET-only recovery: Execute lock and C05 handoff are blocked, while the GET-only lock is allowed. Restoring the snapshot does not write to the live external mailbox.

The maintenance window recorded the strategy deployment timer and Observer as interrupted auxiliary units. Execute restarted with a new PID after 10.096 seconds, recovered its account with fresh market data and UNKNOWN=0, and retained the exhausted budget and PAUSED state. Reconciliation succeeded. These are archived results of the existing deployment operation, not a new recovery exercise or trading acceptance.

## Targeted regression

Python 3.13: 87 tests passed across cloud storage, C07 backup and Grid supervisor; 55 adjacent runtime/systemd tests passed on the first run. The remaining Windows credential subprocess test initially failed with `ModuleNotFoundError` because pytest's configured source path was not inherited by its child interpreter. With `PYTHONPATH` set to this checkout's `src`, that single test passed (1.45 seconds). Total: 143 passing targeted tests. No production source change was needed for the local import-path correction.

## Reproduction

Build/install this revision using the existing deployment procedure. Pin Execute, Observer, health commands, strategy deployment and both backup commands to the same installed release interpreter. The captured absolute release prefix is an installation location, not a separate source revision.

Install `deploy/systemd/factorlab-demo-backup-task-mailbox.conf` as a drop-in for `factorlab-demo-backup.service`, retaining the base unit's `ExecStopPost` recovery hook. `/opt/factorlab/venv/bin/python` in the supplied units must resolve to the installed release interpreter, or both `ExecStart` and `ExecStopPost` must be pinned as in the capture. Preserve the existing state root, credentials, authorizations and live mailbox. The drop-in adds the external mailbox's private write grant and passes `--task-mailbox` and `--drill` to C07; it does not introduce another account owner.

The snapshot covers account journals and shared task intents/receipts, not Observer sampling history. Offsite backup was disabled in this recorded run; host-loss protection still requires the existing encrypted offsite procedure described in the [disaster recovery guide](../../binance-demo-disaster-recovery.md).

Existing official QQQ/SPY acceptance remains documented in [V69-F03](../v69-f03/REPORT.md) and [V69-F04](../v69-f04/REPORT.md). No new official trades, tasks or full restore exercise are needed for this source reconciliation.
