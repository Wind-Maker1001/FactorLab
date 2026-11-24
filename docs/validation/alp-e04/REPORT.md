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

TODO(ALP-E04): transfer predecessor broker/portfolio journals, run cloud portfolio acceptance through the sole owner, record service-scoped network recovery and code rollback evidence, and enable original natural-week scheduling after its execution-day adjustment basis and ownership transition are implemented. Observation success is not portfolio execution or original v69 execution evidence.
