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

Record the account's continuous execution policy once before starting the worker:

```console
factorlab-demo start-continuous --total-orders 100 --total-notional 700 --window-seconds 10800 --window-orders 4
```

The cumulative allowance includes historical non-smoke POST attempts, including canceled and unconfirmed writes. It is never replenished by another window or restart. The loss limit is 1 USDT against the retained opening-equity baseline. Repeating the command cannot enlarge a frozen budget or clear STOP.

With continuous execution RUNNING, the worker GET-reconciles before automatically creating a missing window or rotating an expired/exhausted window. The deployment, order identities and global budget remain unchanged; old-window orders remain recoverable and cancellable. Execute shares the existing account-wide operator lock and real journals with other Demo strategies, so two execution workers cannot own the account simultaneously. Legacy finite campaigns remain supported without automatic renewal.

`factorlab-demo stop` (also `kill`/`disable-demo`) latches account-wide STOP independently of the worker lock. Restart, resume and automatic window creation cannot clear it. Worker `grid-supervisor stop` only stops that worker; it does not revoke account-wide continuous permission.

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

A disconnect closes the dispatch gate. Recovery restores the saved deployment, replays Grid commands and reconciles existing order identities before accepting new work. UNKNOWN never triggers another POST for the same identity. If GET cannot resolve it, submission remains blocked; inspect and resolve that identity rather than starting a replacement deployment. Continuous windows rotate after GET reconciliation; global trading-budget exhaustion or loss protection still blocks new submissions. Technical write uncertainty closes admission until GET resolves the existing identity, without revoking continuous permission.

Status reports mode, effective state/reason, deployment, last bar, feed watermarks, confirmed inventory, unsubmitted reservations, exchange orders/UNKNOWN and request counters. JSON output may contain private account state; keep raw reports out of public evidence.

## Continuous history

The supervisor explicitly enables continuous capacity for its protocol/controller stores and Grid execution, including restored deployments without changing their saved binding. It retains order identities and event history rather than rolling over accounts or deleting records. Each journal persists its continuous-capacity policy, so manual recovery and cancellation retain that capacity after the supervisor exits, independently of current authorization. Journals without that policy and ordinary Grid callers keep their bounded defaults. Storage capacity grants no trading permission: continuous authorization, window counters and the nonrenewable global trading/loss budgets remain independently enforced.

Database/disk use grows with retained history. Recovery replays the deployment's history; routine bridge event projection uses incremental journal cursors. Operational storage must have space for the intended deployment lifetime.

## Runtime metrics

Grid and strategy workers add `observed.metrics` without changing existing fields or commands:

- `api.endpoints`: process-local request counts, last/maximum/total duration in seconds, error categories and error rate, grouped by phase, source, method and endpoint. `initialization` covers session construction; `recovery` covers the first poll and polls after a failed iteration; `poll` covers subsequent healthy iterations, including their account reconciliation. `source` distinguishes signed session work from public feed work. Counts represent client request attempts, including locally blocked attempts; the existing `requests` counters retain their previous meanings. Durations include signing/transport/response parsing, not downstream account-shape validation. Individual samples are not retained.
- `api.observed_weight_headers`: latest `X-MBX-USED-WEIGHT*` headers by source. These are Binance's cumulative weight counters, not per-request charges or worker-exclusive totals. Missing headers mean no weight observation. Do not sum cumulative values or equate GET count with request weight.
- `last_valid_market_at`, `last_reconciled_at`, `last_recovery_seconds`, `unknown_count`: accepted market watermark, most recent successful account reconciliation, latest recovery duration and unresolved order count. Existing effective state/reason and feed reason remain authoritative.
- `journals`: controller/protocol SQLite database and sidecar bytes, allocated bytes, indexed event high-water marks, transaction counts and last transaction duration. High-water mark is not a payload-byte count. Counters reset on process start; persisted status is the last worker observation.
- `campaign`: current authorization expiry, POST budget/consumption and existing campaign limits. Reporting grants no authority.

Request aggregates contain no signed query, credential, response body or account identifier. Metrics are included in the existing control report, not a new journal, database or service.

## Offline performance analysis (2026-11-15)

The reproducible HTTP-stubbed benchmark is `tests/integrations/test_demo_observability.py`. Run with `PYTHONPATH=src python -m pytest tests/integrations/test_demo_observability.py -q -s`. Measurements below used Windows and CPython 3.12.7 with the existing test dependencies. The available CPython 3.13 runtime returned `No module named pytest`; it did not execute tests. Measurements are local fixture costs, not Binance network latency.

Twenty idle Grid recoveries originally issued **500 GETs**: 80 each to `/fapi/v3/account`, `/fapi/v3/positionRisk`, `/fapi/v1/positionSide/dual`, `/fapi/v1/openOrders`, `/fapi/v1/symbolConfig` and `/fapi/v1/apiTradingStatus`, plus 20 book queries. Returning the already validated facts from each adapter reconciliation reduces this to **260 GETs (48% fewer)**: 40 for each account endpoint and the unchanged 20 book queries. Each adapter reconciliation fetches the six account endpoints once instead of twice; the outer recovery still reconciles before and after execution-delta replay. No cache survives a reconciliation or replaces submission-time account/quote freshness checks.

In the first before/after local runs, twenty iterations took 3.059 s versus 2.827 s. Both added zero journal events and committed 40 controller transactions. Peak traced Python allocations were approximately 1.25 MB versus 0.72 MB; these single-run allocation/timing observations are not steady-state RSS or exchange-latency guarantees. Request-count reduction is the deterministic improvement. Restart recovery preserved the economic account state and issued no POST.

Session construction in this fixture issues 9 GETs: one time synchronization, one of each of the six account endpoints, exchange rules and leverage brackets. Credential binding is a separate 7-GET preflight. Neither is part of the 20-poll comparison. Public feed requests are measured separately in normal worker output. The previous mixed **184 GET** observation has no endpoint/phase attribution; it cannot be retroactively treated as 184 steady-state polling requests. The new instrumentation supplies that attribution for subsequent runs.

At 100 versus 1,000 synthetic retained events, recovery measured 0.130 versus 0.134 s and 100 primary-key event lookups took 0.0048 versus 0.0093 s. No index or database migration is justified by this sample. Idle event growth is zero; disk growth is driven by retained trading/source history, not these idle recoveries. The final run occupied 114,688 SQLite bytes in the idle fixture, 126,976/233,472 bytes for 100/1,000 synthetic input events, and 258,048/1,552,384 bytes for 100/1,000 retained canceled orders. These totals include the runtime and nested controller/protocol databases; journal-sidecar bytes are reported separately in status.

Historical order polling is a real remaining scaling cost: isolated adapter reconciliation of 100 versus 1,000 canceled orders issued 100 versus 1,000 order GETs plus six account GETs and took 3.10 versus 33.50 s locally. Recovery scans/deserializes retained protocol orders and controller execution events; order lookup uses the existing primary key, while full reconciliation is linear in lifetime history and retains per-confirmation durability. **TODO:** define an authoritative incremental reconciliation/retention contract before replacing lifetime polling. In particular, the existing order state machine permits canceled orders to advance to filled; skipping them or deleting history would silently change economic recovery semantics. This change therefore retains those reads and transactions rather than introducing a terminal-order cache or weakening commit durability.

Official endpoint references are available through Binance's [published connector](https://github.com/binance/binance-futures-connector-python/blob/main/binance/um_futures/account.py). Its retrieved docstrings do not specify request weights; no numerical weight table is inferred from them. Actual response weight headers are captured when supplied, with error classification and cooldown behavior unchanged.

## Focused validation

Offline tests cover consecutive closed bars, ATR, historical suppression, source replay/restart, unchanged LIMIT prices, partial-fill inventory, GET-only confirmation, reduce-only reversal, pause/resume/stop, disconnect/reconciliation, UNKNOWN, authorization loss, preview/execution lock isolation, CLI controls and explicit storage capacity beyond the ordinary lifetime limits.

A bounded official Demo GET-only preview was observed with two consecutive closed-bar watermarks and a retained preview deployment: **103 GET, 0 POST, 0 DELETE**. It displayed a local BUY reservation without creating an exchange order. No online write campaign was enabled for that observation.
