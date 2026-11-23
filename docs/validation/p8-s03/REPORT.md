# P8-S03 — Azure StrategyPackage deployment

Implementation and validation in progress. This report does not yet claim an Azure deployment or a live strategy switch.

The controller and offline adapter are independent of the unfinished S01/S02 contracts. The initial envelope is `factorlab.demo.evaluate/v1`; candidate admission requires an evidence-bound PROMOTE, qualified=true and strictly_better=true. This compatibility envelope is not itself evidence that a research candidate passed S02.

The existing Grid DemoSupervisor keeps the account session, SQLite journals, authorization, cumulative budgets and systemd identity. A ReleaseConsumer routes closed bars to a trusted strategy subprocess; only the existing DemoSession can submit. Packages are immutable content-addressed zip inventories with source commit, file hashes, pinned dependencies, configuration, interface/state versions, research evidence and an explicit migration policy.

Switch state and events are durable in `strategy-deployment.sqlite3`. REQUESTED → DRAINING → READY_RESTART (optional) → SUCCEEDED. Rollback uses ROLLBACK → ROLLED_BACK and current signed GET facts. Unknown orders, foreign/untracked orders, remaining old orders or positions prevent activation. No SQLite snapshot is restored. Failed rollback remains pending and blocks new openings across restarts.

Offline validation includes an injected network transport, real DemoSession/guard/budget composition, restart recovery, partial fill, original-identity cancellation, UNKNOWN, candidate rejection, worker code integrity and failed rollback. Official Demo verification and 24h/72h/7d conclusions will be recorded separately below.
