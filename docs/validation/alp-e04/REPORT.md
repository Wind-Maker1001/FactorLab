# ALP-E04 Azure Paper runtime

## Observation and crash recovery

On 2026-11-24 UTC, `factorlab-demo-01` was upgraded from `fa55458f4a6c194384a4a39b7fbcac8941a722f3` to `fb041b7384b7441732b1d2a1798cbe1a4d58cfac`. The earlier unit had exited 78 during configuration validation. The new release uses the existing dedicated root-owned credential through systemd LoadCredential.

At 18:37:17 UTC, PID 353242 reported account recovery successful, both streams connected, eight-symbol IEX quotes fresh with no missing symbols, and aggregate health true. The official calendar identified the first session as November 23 and the signal cutoff as November 20; November 24 was not a weekly execution day. Mode was OBSERVE, with no execution enabled.

At 18:38:11 UTC, SIGKILL was sent only to the Alpaca main process. systemd scheduled its automatic restart at 18:38:26. PID 354029 started at 18:38:28 with persistent start count 2. At 18:40:05, account recovery, fresh IEX quotes and both streams were again healthy. systemd reported `NRestarts=1`. The same persistent state directory and account database were retained. The service and health timer are enabled at boot; this test did not reboot the VM.

Binance Execute was active during deployment and was not operated by these deployment or fault-injection commands.

## Release reproducibility

An attempted acceptance upgrade exposed two deployment defects before service cutover: the old deployer copied an uncommitted installer beside the selected archive, and Git archive converted shell files to CRLF under Windows configuration. The former attempted to read a lockfile absent from its archived revision; the latter exited at `set -eu`. Neither attempt replaced the active observation release. The deployer now extracts the installer from its selected commit, Linux dependencies are pinned, and shell/systemd files have explicit LF attributes. Regression tests exercise committed-installer selection and archive generation with `core.autocrlf=true`.

## Integration status

The E01/E02/E03 mainline regression passed 88 tests. Cloud runtime regression passed 11 tests, including independent account/market failure reporting and the shared owner-lock boundary for synthetic acceptance.

E03 predecessor journals contain three COMPLETE plans and zero remaining test-owned AAPL/QQQ quantities. No local Python Alpaca execution process was found during handover preparation. The cloud acceptance entrypoint reuses E03 with the dedicated systemd credential, the account journal and shared portfolio store; its durable campaign is `ALP-E04 CLOUD TEST`, distinct from original strategy signals.

The predecessor broker and portfolio SQLite snapshots were transferred to the dedicated cloud state directory, retaining the former observation journal. The first `ALP-E04 CLOUD TEST` opening attempt exited 78 before order creation: the legacy identity guard treated any existing plan, including completed predecessor campaigns, as a collision. The guard now checks the requested campaign prefix; account-level ownership and reconciliation remain enforced. The correction was deployed as `82fc30b92b9800b837c010d49d8531e20132b1da`. The subsequent cloud opening filled AAPL 0.028384910 and QQQ 0.015687511 shares; both orders and task `2e35a267f344b7ee6376d4bcc3cf786c` are COMPLETE in the durable journal. Post-trade acceptance then exited 78 because cumulative predecessor fills were compared with the new campaign's cash baseline. Cash reconciliation is being scoped to the campaign's durable order identities; recovery must reuse the completed opening rather than create replacement buys. The observation service resumed successfully.

After campaign-scoped cash reconciliation was deployed, a separate `recover` process exited 0 and wrote `evidence-recover.json`. It recovered AAPL 0.028384910 and QQQ 0.015687511 shares, with official cash 99980.04 USD, equity 100000.07 USD, and cash difference 0.00640056108 USD (within the one-cent tolerance). The synthetic rotation also exited 0 and wrote `evidence-rotate.json`: AAPL decreased to 0.014183402 shares, QQQ increased to 0.023464282 shares, official cash was 99980.08 USD, equity was 100000.06 USD, and cash difference was 0.005848782322 USD. Historical opening replay exited 0: order identities and successor holdings remained unchanged, with AAPL 0.014183402 and QQQ 0.023464282 shares. Cleanup then exited 0, reducing both owned quantities to zero. Official equity and cash were both 100000.07 USD; the campaign cash difference was 0.004259428046 USD. Evidence is retained in `evidence-replay.json` and `evidence-cleanup.json`. The command restored the observation service after both phases.

The combined campaign-accounting, natural-week, runtime and portfolio-execution regression passed 63 tests. Coverage includes exclusion of predecessor and similarly prefixed campaign fills, rotation fees, persisted weekly checkpoints, preceding-session matrices, no nonfirst-session signals, action-day blocking, and flat ownership handover. Execution-day corporate actions currently block preparation explicitly rather than reuse a stale share basis.

Post-acceptance inspection confirmed the observation service active/running, an empty `IPAddressDeny` property, recovered account state, and fresh IEX quotes for every symbol. The startup health snapshot still had trade stream STARTING and aggregate health false; it does not establish full dual-stream recovery.

The existing rollback script successfully switched from `f274ba48b8618ff4480d98c6bbf9e1c497b9cbe6` to `82fc30b92b9800b837c010d49d8531e20132b1da`, then back to `f274ba48b8618ff4480d98c6bbf9e1c497b9cbe6`. Both invocations exited 0 and both resulting units were active. The script changes only the release symlink after its runtime-schema compatibility check; it retains the persistent databases.

At 19:31:06 UTC, after rollback restoration, the same persistent state reported aggregate health true, both market and trade streams CONNECTED, recovered account state, fresh eight-symbol IEX quotes and no IPAddressDeny restriction. This establishes post-restart dual-stream recovery, not uninterrupted same-process recovery from the earlier network injection.

TODO(ALP-E04): record same-process full service-scoped network recovery, implement execution-day corporate-action rebasing, and enable original natural-week scheduling. Observation success is not portfolio execution or original v69 execution evidence.
