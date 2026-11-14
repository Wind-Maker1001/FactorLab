# Binance Demo continuous Grid supervisor

**NO LIVE MONEY.** This worker only accepts the official Binance USD-M Demo environment and the dedicated long-only `NVDAUSDT` account binding. It composes the existing market feed, Grid execution bridge, session, authorization runtime and journals.

## Start and control

From an installed checkout, start a GET-only preview:

```console
factorlab-demo grid-supervisor start
```

For a bounded observation:

```console
factorlab-demo grid-supervisor start --seconds 65 --poll-seconds 5
```

In another terminal:

```console
factorlab-demo grid-supervisor status
factorlab-demo grid-supervisor pause
factorlab-demo grid-supervisor resume
factorlab-demo grid-supervisor stop
```

When running from source, use `python -m factorlab.integrations.binance_futures_trading.demo_cli` in place of `factorlab-demo`, with `src` on `PYTHONPATH`. Python 3.13 or 3.14 is required.

`pause` blocks new submissions while GET monitoring continues. `resume` requires reconciliation and a new eligible closed bar. `stop`, Ctrl+C, process termination and duration expiry **do not cancel orders or close positions**. Use existing explicit order-management commands for separately authorized cancellation or reduction. A stopped process no longer monitors resting orders.

Control commands update desired state; status also exposes the worker's last observed state. The worker applies controls at its next polling/submission boundary. A control request cannot undo an HTTP request already dispatched. After duration expiry, `observed.status` is `EXITED` even if desired state remains `RUNNING`; `resume` does not launch a new process—use `start` to restart it.

## Modes and state isolation

Preview is the default. It may use saved Demo credentials for signed account GETs, but requires **no persistent write authorization**. Its control database, feed cursors, runtime and trading journals live under `%LOCALAPPDATA%/FactorLab/BinanceDemo/grid-preview`. Its lock is in that preview directory, not the real execution directory. Preview cannot POST or DELETE; proposals are displayed as local reservations, not simulated fills.

Execution is explicit:

```console
factorlab-demo grid-supervisor start --execute
factorlab-demo grid-supervisor status --execute
factorlab-demo grid-supervisor pause --execute
factorlab-demo grid-supervisor resume --execute
factorlab-demo grid-supervisor stop --execute
```

These commands use an **already existing**, unexpired persistent Demo campaign. Starting the worker neither creates nor renews authorization. Execute shares the existing account-wide operator lock and real journals with other Demo strategies, so two execution workers cannot own the account simultaneously. The campaign must match the saved deployment; a different campaign does not silently replace it.

The unchanged admission limits are at most **0.04 NVDA**, **7 USDT committed/notional**, and **one active exchange order**. Pending/UNKNOWN submissions also block replacement. Each BUY is checked against current inventory and commitment. SELL is reduce-only and cannot exceed GET-confirmed inventory. Exchange filters can make a proposal inadmissible; quantity and LIMIT price are never increased or repriced to bypass them.

## Configuration and market timing

Options include `--interval` (default `1m`), `--max-backfill` (default 120), `--poll-seconds` and optional positive `--seconds`. An ATR history window must fit the backfill bound.

`--config path.json` accepts the existing `GridConfig` fields. On first deployment without this option, the worker fixes a one-level arithmetic range approximately 1% below/above the observed ask, rounded to the exchange tick, with a 0.04 quantity unit, maximum inventory of one unit, 7 USDT commitment and pause-on-breakout. Subsequent starts restore the original deployment, not a range derived from a new quote. Restart with the same interval, backfill and configuration arguments; conflicts are rejected.

Only confirmed closed bars enter the Grid observation timeline. Completed-bar high/low/close update ATR and other bar decisions; none is treated as evidence that a resting order traded. Quotes update market reference and the fresh dispatch checks, but cannot produce inventory or stand in for a bar.

At startup, resume and reconnect, GET reconciliation precedes any submission. Initial historical bars and delayed catchup bars restore strategy state without submitting old signals. Only a newly closed, current-interval bar after the recovery boundary is eligible to unlock dispatch. Local proposals from historical/paused processing are withdrawn with their identities retained; they are not exchange rejections. A subsequent eligible bar may produce a new reservation. Existing submitted orders remain distinct and are never withdrawn locally.

Source event identity and its Grid command commit together before feed acknowledgement. A crash between that commit and ACK therefore cannot apply the same bar twice. A repeated identity with different OHLCV is rejected.

## Order confirmation and recovery

Execute uses the original Grid LIMIT intent through `DemoSession.submit_intent`. Complete-looking POST/DELETE receipts still require GET readback; fills, partial fills and terminal state use the existing authoritative ledger. An explicit POST rejection requires GET confirmation of the order or its absence before local rejection is accepted.

GET cumulative fills contribute only their new delta. A confirmed BUY fill can create a reduce-only SELL proposal, but neither a crossing quote nor historical high/low can do so. Partial fills retain their accumulated inventory and any below-filter reversal remainder.

A disconnect closes the dispatch gate. Recovery restores the saved deployment, replays Grid commands and reconciles existing order identities before accepting new work. UNKNOWN never triggers another POST for the same identity. If GET cannot resolve it, submission remains blocked; inspect and resolve that identity rather than starting a replacement deployment. Authorization expiry or exhaustion stops further submissions without creating another campaign.

Status reports mode, effective state/reason, deployment, last bar, feed watermarks, confirmed inventory, unsubmitted reservations, exchange orders/UNKNOWN and request counters. JSON output may contain private account state; keep raw reports out of public evidence.

## Continuous history

The supervisor explicitly enables continuous capacity for its protocol/controller stores and Grid execution, including restored deployments without changing their saved binding. It retains order identities and event history rather than rolling over accounts or deleting records. Each journal persists its continuous-capacity policy, so manual recovery and cancellation retain that capacity after the supervisor exits, independently of current authorization. Journals without that policy and ordinary Grid callers keep their bounded defaults. Continuous capacity is not an authorization extension: the independent expiring campaign and POST budget still apply.

Database/disk use grows with retained history. Recovery replays the deployment's history; routine bridge event projection uses incremental journal cursors. Operational storage must have space for the intended deployment lifetime.

## Focused validation

Offline tests cover consecutive closed bars, ATR, historical suppression, source replay/restart, unchanged LIMIT prices, partial-fill inventory, GET-only confirmation, reduce-only reversal, pause/resume/stop, disconnect/reconciliation, UNKNOWN, authorization loss, preview/execution lock isolation, CLI controls and explicit storage capacity beyond the ordinary lifetime limits.

A bounded official Demo GET-only preview was observed with two consecutive closed-bar watermarks and a retained preview deployment: **103 GET, 0 POST, 0 DELETE**. It displayed a local BUY reservation without creating an exchange order. No online write campaign was enabled for that observation.
