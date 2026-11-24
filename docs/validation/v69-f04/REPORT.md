# V69-F04 deployment and shared-owner recovery

The deployed observer watches only QQQUSDT/SPYUSDT. `factorlab-demo-execute.service` is the only account order owner; the observer has no credentials or order client. Independent `ACCEPTANCE` tasks do not certify original v69 signals or natural weekly execution.

The owner-side contract is F03 `a956d79`, including deployed Execute revision `201d76ad2a67257127bfb7f9a0f1e01a8d339230`. The observer is pinned to `5302407564be47893529079492163f6e0675d3bb`. Both use `/var/lib/factorlab-tradfi-observer/task-mailbox/tasks.sqlite3`: Execute's `FACTORLAB_DEMO_TASK_DB` and observer's `--task-db` agree. Execute has a write grant for only that mailbox subdirectory; observer access to credentials is hidden by systemd.

## Existing official acceptance

The F03 [online report](../v69-f03/REPORT.md) records two QQQ/SPY round trips. The second round entered through TaskMailbox, with immutable `ACCEPTANCE` identities `f04-mailbox-acceptance-open-20261124-01` and `f04-mailbox-acceptance-close-20261124-01`. Their completion timestamps are respectively 2026-11-24 15:53:42.732981 UTC and 15:55:33.260531 UTC. They are not scheduled tasks or original v69 trades.

QQQ mailbox BUY/SELL order IDs are 65897174 / 65897750; SPY IDs are 65797009 / 65797260, quantity 0.01 each. The owner recovered held positions across process restart using the original journaled IDs. Portfolio fill fees, concurrent Grid changes and wallet/NAV reconciliation belong to F03's owner report, not to an independent F04 trading ledger. No additional orders are required to update this deployment evidence.

## Runtime inspection

[Read-only cloud capture](cloud-runtime.json), 2026-11-24 16:11:30 UTC:

- Execute PID 335200, active since 16:01:30 UTC, RUNNING with desired RUNNING, account_recovered=true, reason=null, UNKNOWN=0. Its original NVDA BUY reservation remains visible; no second portfolio execution process is configured.
- Observer PID 333727, active since 15:57:06 UTC. Persistent starts=3 and 88 samples beginning 15:27:42 UTC survived its process changes; disconnects/recoveries are both zero in this captured observation window.
- Both services and both existing health timers are enabled for boot. Automatic restart counts for the two active services are zero. The observer remains credential-free and reads account projections from the Execute owner.
- The mailbox retains exactly two tasks, both ACCEPTANCE, both COMPLETED. Receipt sequence 1–3 belongs to OPEN and 4–6 to CLOSE; each sequence is ACCEPTED, RECONCILING, COMPLETED. The observer exposes the terminal acceptance timestamp separately from `last_successful_rebalance=null` and `NO_STRATEGY_CONFIGURED`.
- The XNYS natural-week window remains November 30, 14:30–21:00 UTC, with history through November 27. No daily signal, forced buy, extra symbol or scheduled-completion claim is introduced.

At 16:13 UTC the [existing health commands](health-checks.json) returned observer healthy=true, market_fresh=true and heartbeat age 0.995 seconds. Both existing health services last exited successfully with status 0. The Eastern-date daily report had 94 samples and execution_status=COMPLETED, identifying `factorlab-demo-execute.service` as owner. The mailbox parent directory is private 0700 and belongs to the existing service identity.

## Execution restart evidence

The existing F03 [held-position restart export](../v69-f03/mailbox-restart-held.jsonl) records recovered Execute PID 332681, account_recovered=true, UNKNOWN=0 and both 0.01 QQQ/SPY positions still held. The original filled OPEN client IDs remain `pfc3c7eb670091b34cb65b827cc95560` and `pf3f334626f754b5cac7957d3436cf88`, each with one recorded POST dispatch. The original COMPLETED OPEN receipt is preserved rather than republished as a new task or order. The later CLOSE receipt and current empty portfolio/account inventory follow in F03's owner evidence. The current read-only inspection neither restarted Execute nor created tasks.

The shared mailbox must accompany account journals during an offline coherent backup; account-root-only backups do not include this external subdirectory. Ordinary process recovery uses the surviving original files, not an old database restore.

## Merge dependency

F01 #241 is already in main. Merge #239 before #242. F04 integrates main `6c4df82` and F03 `a956d79` with identical shared TaskMailbox/WeeklySchedule contracts; the dependency merge completed without textual conflicts. #239 owns account execution, static symbol ownership, shared admission and journal recovery. #242 owns the observer, scheduling/producer boundary and observer units. No automatic GitHub merge is performed.
