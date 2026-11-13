# Official Demo Futures execution contract

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

The P7-D02 contract supports offline construction and response injection for
official Demo Trading. P7-D03A adds a separate fixed-host network boundary, disabled
without independent process-local authorization and complete bounded-plan admission.
No network order execution is authorized by the engineering acceptance.
`DemoExecutionTransport` is an `OfflineTransport`: its delegate must also be an
offline transport. Its fixed host identity is `https://demo-fapi.binance.com`,
but it never constructs a URL, opens a socket or follows a redirect.

## Construction and admission

1. Bind `Credentials` and `FuturesClient` to `Environment.DEMO`.
2. Inject an offline delegate into `DemoExecutionTransport`. Writes are disabled
   unless a new process-local `DemoExecutionAuthorization()` is supplied. This
   object grants only injected writes and cannot be pickled. Neither environment
   variables nor stored account state can create it.
3. Construct `DemoFuturesAdapter` with the existing `PaperStore` protocol journal.
   The inherited identity check binds Demo, account alias and API-key digest.
4. Acquire rules with `observe_demo_rules(adapter, source_version=...)`. Both
   exchangeInfo and account leverage brackets use that same Demo client. Production
   observations and PAPER_SIMULATION account assumptions are not used.
5. Construct `DemoExecutionGateway` with that account-bound observation and inject
   it into the existing `TradingController`. Use explicit shared risk limits and
   the existing quote and account reconciliation paths before submission.

The supported instrument is NVDAUSDT, with MARKET/LIMIT BUY/SELL, client-ID order
query, open-order query and cancellation. Existing Decimal validation, quantity,
price and notional filters, brackets, available margin, one-way mode, leverage
configuration, five-second account freshness and shared risk limits remain active.
Missing required evidence rejects admission. The test fixtures represent injected
Demo responses, not verified online account conditions. A custom human-readable
source label cannot substitute for querying the Demo client.

Direct Demo adapter submission is blocked outside gateway admission. The original
FuturesAdapter submit/cancel offline-transport checks are retained, with an exact-type
DemoNetworkTransport extension requiring independent active authorization and a
one-use approved request. The ordinary
read-only network transport still rejects POST/DELETE/PUT, including in Demo.
Production requests cannot pass the Demo transport, and production credentials
cannot construct its client. The authorization is never a production capability.

## State and recovery

The existing adapter journal persists clientOrderId before dispatch. A lost submit
or cancel response leaves UNKNOWN / RECONCILE_REQUIRED. The controller and adapter
refuse blind resubmission; recovery uses GET order queries, account reconciliation
and cumulative trade/fee evidence. The existing gateway imports confirmed orphan
identities and replays only unbooked fill deltas into the shared controller ledger.
No second economic ledger is added.

Identical REST confirmations now restore an UNKNOWN record as well as deduplicate
its event. An explicit locally recorded rejection with no exchange order ID is
not queried as if it were a previously accepted order. Uncertain orders are still
queried and never inferred to be rejected from ORDER_NOT_FOUND.

## P7-D03A network admission

`DemoNetworkTransport` is separate from the offline delegate contract. Its URL is
constructed from the literal official Demo host, and `_NoRedirect` disables HTTP
redirects. Only the existing order POST and specified-order DELETE may pass the
write boundary. Account configuration, transfers and withdrawals remain absent.

`DemoExecutionPlan` requires a Demo credential-bound account, NVDAUSDT, immutable
MARKET/LIMIT type set, positive Decimal quantity/notional/loss/margin limits,
positive order/open-order/time limits, UTC expiry, client-ID prefix and explicit
cleanup/recovery policies. There are no real-plan defaults or CLI write switches.
Synthetic test values are not approved online limits.

`DemoNetworkAuthorization(plan)` is independent of offline authorization and
cannot be serialized. It expires by both wall-clock deadline and monotonic elapsed
time. Restart may reconstruct the plan as data but cannot restore approval.
Bind the existing controller using `gateway.bind_controller(controller)`; its
process-local admitted intent proves the shared risk path was traversed. Rules
must be the current process's account-bound observation from the same Demo client.

Before dispatch, admission requires explicit canTrade and observes quantitative
lock events: ACCOUNT or order-symbol isLocked=true blocks, false records only
that named indicator as NOT_LOCKED, and valid omitted indicators are NOT_REPORTED
without independently blocking Demo. No global unlocked certificate is required.
Failed, malformed or conflicting observations remain UNKNOWN and use existing
classified errors, cooldown and reconciliation; failure is not an absent lock.
Admission also checks fresh one-way account evidence, Demo mark price, percent-price
bands, position-risk restriction, order count, conservative aggregate collateral,
virtual loss and every required plan bound. Existing exchange filters, brackets,
mode/configuration and shared risk checks still apply. The adapter supplies a
one-use exact-parameter approval only after these checks; its existing journal
commits order identity before HTTP dispatch. The transport checks the signed key's
account digest and exact approved request before opening a connection.

Cleanup is not automatic: stop new orders, reconcile unknown IDs via GET, then
cancel only explicitly known plan orders while the capability remains active.
After expiry or revocation, GET recovery remains available but all writes,
including cleanup cancellations, need a newly reviewed bounded approval. Loss and
margin limits gate new submissions; they are not guaranteed loss stops between
observations. No background liquidation or automatic closing order is introduced.

## Closed-bar strategy composition

`DemoStrategyRuntime(controller, strategy, client_id_prefix=...)` composes an
existing Demo gateway/controller with `DualMAStrategy` or `FrozenModelStrategy`.
Call `recover()` before execution, feed confirmed `closed_bar()` observations,
then feed a strictly later `Quote` through `quote()`. Bar prices never generate
fills. The runtime stores deployment identity, history, pending signal and the
signal-to-client-order mapping in the existing controller transaction and journal.
A restored deployment must match its persisted identity; the caller must recreate
any separately approved capability in the new process.

Target sizing shares the Paper runner's pure long-only calculation, using Demo
wallet equity, reported available balance, confirmed position and exchange lot
constraints. Shared admission remains conservative about local reservations.
REST reconciliation updates the shared cash/position projection from exchange
facts, including external account changes, after missing trade deltas have been
booked. Actual confirmed fees are journaled independently of the local fee budget.
Synthetic `fill()` and `reject()` are blocked on the Demo gateway.

`disconnect()` pauses new orders; `recover()` queries existing identities and
books missing deltas without replacement POSTs. `kill()` changes only local state
and blocks every strategy-generated order, including reductions. It neither
cancels nor liquidates. Explicit cancellation still requires the existing
independent write authorization. A bar-sequence gap pauses execution.

Offline execution evidence is retained under `docs/validation/p7-m02a/`.
For an online session, supply actual account identity, strategy/model bundle and
source/interval binding, client-ID prefix, allowed order types, quantity,
notional, total/open-order, loss, margin and time limits, UTC expiry, and explicit
cleanup/recovery policies. Review those values and grant an independent bounded
Demo network authorization; offline execution approval is not that authorization.

## P7-D03B minimum online integration

A separate authorization is required before online writes:

1. Review the Demo-only boundary and approve every plan limit, unique ID prefix,
   expiry, cleanup and recovery policy. Create a new process-local network capability
   only in that authorized session. Do not reuse the offline delegate as a network
   client; production writes remain unconditionally blocked.
2. In one bounded session, load existing Demo credentials securely, synchronize
   time and fetch Demo NVDAUSDT exchangeInfo, position mode, symbol configuration,
   leverage brackets, available margin and existing orders. UNKNOWN blocks entry.
3. Confirm test quantity, price, notional ceiling, maximum order count and cleanup
   plan with the user. Start with one LIMIT order and query/cancel by the same ID;
   only perform MARKET execution under the separately authorized bounded plan.
4. Verify partial/final fills and fees from Demo order/trade reads, then restart
   and reconcile without replacement orders. Retain capability/status evidence
   only, never raw private payloads, credentials or signed URLs.

P7-D02 engineering acceptance does not claim an online Demo placement, cancellation,
fill, account-specific permission check or financial admission.

## P7-M02B account and plan review

Run the one-shot official Demo preflight with the existing Demo environment
variables. Add `--credential-dialog` to enter an existing pair into the local
masked window. The report includes selected account facts for the operator.

```console
python -m factorlab.integrations.binance_futures_trading.demo_preflight --credential-dialog
```

The preflight synchronizes time, reads exchange rules and authenticated account
facts, and observes the Demo mark and top of book. A successful signed GET verifies
USER_DATA access. Account `canTrade` and the key's TRADE permission are separate.
The latter is UNKNOWN through these reads. Omitted quantitative indicators in a
valid response are NOT_REPORTED. The report distinguishes an applicable lock from
an unknown or conflicting response. See the
[P7-M02B observation and plan worksheet](validation/p7-m02b/README.md).

For a complete proposal, construct the existing `DemoExecutionPlan` with the
reviewed limits and pass it to `inspect_demo(credential, plan=plan, limit_price=...)`.
The first-order estimate uses the observed Demo ask for a proposed opening BUY.
LIMIT needs an explicit price for review. The checker uses the exchange quantity
grid and minimum notional, then checks the larger reservation used by the gateway
against the plan. It also checks configured leverage against the applicable tier.
The controller will apply its own risk limits and fee reserve at execution.

### GET recovery with the existing journals

Use the original protocol and controller `PaperStore` instances and the same
Demo credential pair. After time sync, construct the adapter with
`DemoNetworkTransport()` and acquire a fresh `observe_demo_rules(adapter, ...)`.
Reopen the gateway and controller with the persisted account, instrument mapping
and fee rate. Then call `gateway.recover(controller)`. The shared store normally
contains the last quote. Supply a fresh Demo quote through `controller.quote()`
if recovery must import an orphan protocol order and the shared quote is absent.

Recovery queries the journaled client order IDs and the trades that explain any
new cumulative fill. It books missing quantity and actual fees into the shared
ledger and refreshes the account projection. An unknown order remains subject to
reconciliation. An authorized Demo cancellation goes through
`controller.cancel(client_order_id)` after the known order has been reconciled.
