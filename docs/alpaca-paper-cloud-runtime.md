# Alpaca Paper cloud runtime

The Paper service is independent of Binance Demo: user `factorlab-alpaca`, code `/opt/factorlab-alpaca/releases/<commit>`, persistent state `/var/lib/factorlab-alpaca-paper`, credential `/etc/factorlab-alpaca-paper/paper-secret.json`, and unit `factorlab-alpaca-paper.service`. Never point it at Binance state or credentials.

The initial deployment is `OBSERVE`: its HTTP transport refuses non-GET requests. It authenticates the official Paper account, REST-recovers account facts, maintains trade_updates and explicit IEX market subscriptions, stores REST bar gap repairs and stream events, and publishes separate account, market, strategy and execution states. No request can target a configurable production trading host. Execution remains `WAITING_E03_HANDOVER`; original strategy scheduling remains `WAITING_F02`. These are dependency states, not successful execution.

## Installation

Build and deploy a committed version with `tools/deploy_alpaca_paper_runtime.py --repo <checkout> --commit <full-sha> --host <host> --key <private-key-path>`. `--credential-file <Windows-DPAPI-file>` optionally decrypts the existing private credential in memory and transfers its JSON representation through SSH stdin, never argv or a local plaintext file. The destination must not already exist. systemd LoadCredential supplies the root-owned 0600 secret to the service. No credentials belong in Git, shell history, environment files, reports or PRs.

The installer creates an isolated venv and saves installed dependency versions. Releases are root-owned and not service-writable. It enables boot startup but starts only when the dedicated credential exists. A release contains code, never account state. Configure persistent host journald retention for health events.

`systemctl restart factorlab-alpaca-paper` preserves all SQLite databases. SIGTERM closes the stream and databases; systemd restarts unexpected failures after 15 seconds without a finite restart burst cap. Configuration/authentication/database refusals exit 78 for explicit correction. Transport interruptions use bounded exponential delay without order resubmission.

## Ownership and upstream activation

A local flock excludes two cloud workers on this state root, not an independently running Windows worker. Before enabling E03 execution, stop the old account owner and transfer both its E01 journal and shared E03 PortfolioStore. Restore existing client IDs before accepting new work. UNKNOWN plus REST 404 is not permission to resubmit. All acceptance and strategy tasks must go through that one owner.

The calendar comes from the official Paper calendar endpoint. The weekly window identifies the week's first scheduled session and its previous completed session, including holidays and early closes. The initial observer records that window but does not create strategy tasks. Original v69 must be connected to the F02 implementation and E03 persisted task identity before activation; empty weights remain valid. Acceptance task fills must retain their acceptance source and must not be labeled original v69.

## Health and reports

Run `sudo -u factorlab-alpaca /opt/factorlab-alpaca/current/venv/bin/python -m factorlab.integrations.alpaca.cloud_runtime health`. The minute timer sends the same JSON to journald. An active unit alone is not recovered account or strategy execution readiness. Reports are private daily JSON files and SQLite records, updated with current official equity, cash, positions, actual weights and official FILL activities. Target weights and strategy returns are null until real strategy execution is connected. Equity change has an explicit observation baseline and is not claimed as strategy performance.

Health records the release, PID, persistent process-start count, heartbeat, recovery timestamp, trade stream, market stream/gap status, explicit IEX REST quote availability, weekly window and deferred execution state. A stale heartbeat fails the health command. Quiet closed-market feeds are not fabricated market events.

## Code rollback and recovery

Stop only `factorlab-alpaca-paper.service`. Select a retained release with the same database schema, atomically repoint `/opt/factorlab-alpaca/current` and restart. Never restore an older database to roll back code; keep runtime/account/market SQLite and sidecars together. The initial runtime schema is additive; a future incompatible schema change must provide explicit migration and rollback compatibility before deployment. Do not use an older release with an incompatible schema.

Validate process interruption and restart against the same state root, preserving start counts, account binding, journals and report baseline. Boot wiring can be inspected without rebooting the host; an actual host reboot must be coordinated with other running services. Network fault tests must affect only this service, not host-global routes/firewalls or Binance.
