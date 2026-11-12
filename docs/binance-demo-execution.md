# Official Demo Futures execution contract

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

This contract supports offline construction and response injection for official
Demo Trading, not network order execution. No Demo network writer exists here.
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
FuturesAdapter submit/cancel offline-transport checks are retained. The ordinary
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

## P7-D03 minimum online integration

A separate authorization and implementation step is required before online writes:

1. Review and authorize a Demo-only network writer with fixed host, no redirects,
   environment/key checks and an independent process-local network capability.
   Do not turn the offline delegate into a network transport or remove the base
   adapter's offline guard. Explicitly decide the narrow execution extension at
   that boundary; production writes must remain unconditionally blocked.
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
