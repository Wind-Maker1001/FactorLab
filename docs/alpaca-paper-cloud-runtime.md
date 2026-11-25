# Alpaca Paper cloud runtime

The Paper service is independent of Binance Demo: user `factorlab-alpaca`, code `/opt/factorlab-alpaca/releases/<commit>`, persistent state `/var/lib/factorlab-alpaca-paper`, credential `/etc/factorlab-alpaca-paper/paper-secret.json`, and unit `factorlab-alpaca-paper.service`. Never point it at Binance state or credentials.

The initial deployment is `OBSERVE`: its HTTP transport refuses non-GET requests. It authenticates the official Paper account, REST-recovers account facts, maintains trade_updates and explicit IEX market subscriptions, stores REST bar gap repairs and stream events, and publishes separate account, market, strategy and execution states. No request can target a configurable production trading host. Without native execution enabled or an explicit instance configuration, strategy scheduling remains `NOT_ENABLED` and no strategy orders are submitted. The flag selects the original F02 strategy and shared E03 execution journal; it is not a substitute-strategy switch.

## Installation

Build and deploy a committed version with `tools/deploy_alpaca_paper_runtime.py --repo <checkout> --commit <full-sha> --host <host> --key <private-key-path>`. `--credential-file <Windows-DPAPI-file>` optionally decrypts the existing private credential in memory and transfers its JSON representation through SSH stdin, never argv or a local plaintext file. The destination must not already exist. systemd LoadCredential supplies the root-owned 0600 secret to the service. No credentials belong in Git, shell history, environment files, reports or PRs.

The installer is extracted from the selected Git archive, never taken from an uncommitted working tree. It creates an isolated venv using `requirements/alpaca-paper-linux-py313.lock.txt` and saves installed dependency versions. Releases are root-owned and not service-writable. It enables boot startup but starts only when the dedicated credential exists. A release contains code, never account state. Configure persistent host journald retention for health events.

`systemctl restart factorlab-alpaca-paper` preserves all SQLite databases. SIGTERM closes the stream and databases; systemd restarts unexpected failures after 15 seconds without a finite restart burst cap. Configuration/authentication/database refusals exit 78 for explicit correction. Transport interruptions use bounded exponential delay without order resubmission.

## Ownership and upstream activation

A local flock excludes two cloud workers on this state root, not an independently running Windows worker. Before enabling E03 execution, stop the old account owner and transfer both its E01 journal and shared E03 PortfolioStore. Restore existing client IDs before accepting new work. UNKNOWN plus REST 404 is not permission to resubmit. All acceptance and strategy tasks must go through that one owner.

The calendar comes from the official Paper calendar endpoint. The weekly window identifies the week's first scheduled session and its previous completed session, including holidays and early closes. Enabled execution prepares the original eight-symbol F02 strategy from 252 prior sessions, persists its checkpoint and adjusted-share deltas in the E03 task, and resumes the same task after restart. A naturally flat decision completes without forced orders. Nonfirst sessions recover existing tasks but never generate a new signal. Flat completed acceptance sleeves transfer ownership only after official inventory and pending-order reconciliation.

Execution-day adjustment factors use matched official raw/all-adjusted completed minute bars; these prices never enter the preceding-session signal matrix. Corporate receipts are retained and reconciled before task recovery on every cycle, including nonfirst sessions, from the earliest owned strategy session so late receipts are not lost at weekly cutoff changes. Unambiguous settled splits and dividends post idempotently; missing allocation or chronology evidence blocks dependent inputs rather than inventing terms. Retained child securities remain outside the eight-symbol tradable universe. `asof` is a symbol-mapping date, not a frozen adjustment-knowledge date.

Cloud synthetic acceptance uses `acceptance --phase open|recover|rotate|replay|cleanup`, the same owner lock, credential and journals. Stop the observer while the transient acceptance unit owns the account. Its campaign is `ALP-E04 CLOUD TEST`; fills are not original v69 signals. Cash reconciliation includes only durable order identities belonging to that campaign, even when the shared strategy report contains predecessor fills.

## Instance execution

Instance allocations are cash-funded sleeves, not independent broker accounts. Register funding only after reconciling official cash and explicitly attributed inventory; the sum of sleeve funding cannot reuse the same account capital. Multiple sleeves may own the same security, but one sleeve cannot sell another's shares. Broker inventory remains the authority for aggregate reconciliation.

Freeze each scheduled event before invoking strategy code. A target quantity is compared with that event's sleeve inventory, never the inventory observed after some children have filled. Package and event identities distinguish successive decisions; replay resumes the existing outbox. An omitted target leaves that security unchanged, while an explicit zero closes the sleeve's position. Direct-order acceptance acknowledges durable intents, not fills.

Schedules use official regular sessions. Intraday intervals start at session open and require complete contiguous closed minutes; early-close stubs and overnight gaps do not form bars. Daily and natural-week signals end at the previous official session close. Quotes acquired for execution retain their actual feed and current acquisition time; they do not move the historical signal cutoff.

Account corporate receipts are retained once and economically posted to proven owners together. Cash allocations preserve the exact official total. A receipt whose entitlement crosses unresolved execution chronology blocks that security rather than assigning proceeds from post-action positions.

The generic entrypoint is `run --instances <private-config.json> --feed iex|sip`. It uses the same owner lock and account journals as the legacy entrypoint; do not combine it with `--execute-v69`. Build, install and promote packages through shared strategy management before starting the configured instances. The configuration names the installed release directory, explicitly trusts Python strategy execution, and binds each instance to its allocation:

```json
{
  "releases": "/var/lib/factorlab-alpaca-paper/releases",
  "trusted_python": true,
  "instances": [{
    "binding": {
      "market": "ALPACA_US_EQUITY",
      "account_id": "<official-paper-account-id>",
      "strategy_id": "alpaca-balanced",
      "instance_id": "balanced",
      "allocation_id": "balanced-usd"
    },
    "funding": {"capital": "100", "initial_cash": "100"}
  }]
}
```

Funding registration is immutable and does not adopt broker positions. The package's configurable universe and schedule determine historical acquisition and subscriptions. Selecting SIP makes real SIP permission failures visible; it never falls back to IEX. The `alpaca_balanced` example keeps ten percent cash and supports daily or closed intraday schedules.

The original `--execute-v69` entrypoint builds the native wrapper with the release's full `--revision` commit, adopts the existing checkpoint and journal into shared deployment storage, and continues using the original plan constructor. Existing frozen preparations remain authoritative. A native event is frozen before the shared decision receipt can create its plan; interruption between those steps reuses that event rather than acquiring new signal inputs.

To include native V69 beside generic packages, register `native_v69: true` with strategy and instance `V69_CAUSAL` and allocation `v69-original`. It borrows the same account runner and journal. Explicit allocation changes V69's inventory and cash inputs to that sleeve only; other owners' shares and corporate child securities do not enter its signal. Without an allocation, the original entrypoint retains its full-account cash semantics.

Package promotion uses `wait_flat`: cancel and reconcile the instance's original children, then wait for its owned positions to be flat before migrating its checkpoint. It does not liquidate a sleeve automatically. Other instances continue independently. Active-version persistence and activation callbacks are separate durable steps; an interrupted callback is retried before that instance receives another event. Building a native wrapper for a newer code release does not promote it automatically: the original active native package remains selected until shared deployment promotion completes. Native releases are installed under `strategy-releases` and must retain the original V69 declaration.

For an already-traded journal, replace `initial_cash` in funding with `current_cash` and a stable `adoption_receipt_id`. Adoption derives the initial cash baseline from confirmed historical fills, fees and corporate cash, without replaying or modifying those records. Capital must equal the specified current sleeve cash plus its explicitly owned inventory at official marks. Reuse the same adoption receipt on restart; do not register current cash as initial cash.

## Health and reports

Run `sudo -u factorlab-alpaca /opt/factorlab-alpaca/current/venv/bin/python -m factorlab.integrations.alpaca.cloud_runtime health`. The minute timer sends the same JSON to journald. An active unit alone is not recovered account or strategy execution readiness. Reports are private daily JSON files and SQLite records, updated with current official equity, cash, positions, actual weights and official FILL activities. When an original strategy task exists, reports include its target weights, frozen signal provenance and execution-ledger accounting. Equity change has an explicit observation baseline; execution-ledger return has a separate capital denominator. Neither is labeled original strategy return, which remains null.

Health records the release, PID, persistent process-start count, heartbeat, recovery timestamp, trade stream, market stream/gap status, explicit IEX REST quote availability, weekly window and deferred execution state. Each REST cycle revokes previous quote availability before acquiring new quotes. A market-data failure does not erase an account recovery that succeeded in the same cycle; the aggregate remains unhealthy. A stale heartbeat fails the health command. Quiet closed-market feeds are not fabricated market events.

## Code rollback and recovery

Stop only `factorlab-alpaca-paper.service`. Select a retained release with the same database schema, atomically repoint `/opt/factorlab-alpaca/current` and restart. Never restore an older database to roll back code; keep runtime/account/market SQLite and sidecars together. The initial runtime schema is additive; a future incompatible schema change must provide explicit migration and rollback compatibility before deployment. Do not use an older release with an incompatible schema.

Validate process interruption and restart against the same state root, preserving start counts, account binding, journals and report baseline. Boot wiring can be inspected without rebooting the host; an actual host reboot must be coordinated with other running services. Network fault tests must affect only this service, not host-global routes/firewalls or Binance.
