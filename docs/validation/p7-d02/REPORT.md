# P7-D02 — Demo execution offline engineering acceptance

**P7_D02_DEMO_EXECUTION_ENGINEERING_ACCEPTED**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

Baseline: `ef109bd3f2b9b1c93c367241e0072beddd7a9a15`, the main merge of P7-D01
PR #121. This acceptance covers offline execution, isolation, state transitions
and recovery only. No network order placement or cancellation was performed.

## Execution boundary

`DemoExecutionTransport` accepts only Demo-tagged requests and an offline delegate.
Its host identity is fixed to `https://demo-fapi.binance.com`; it contains no
network implementation. Writes require an explicit nonserializable process-local
`DemoExecutionAuthorization`. Only order POST/DELETE and existing allowlisted GETs
are supported. Production credentials and requests reject before dispatch.
The ordinary read-only network transport and original adapter offline-write guards
remain intact; no production write or fallback route was added.

`DemoFuturesAdapter` and `DemoExecutionGateway` reuse FuturesAdapter persistence,
FuturesPaperGateway admission/replay and the existing TradingController. NVDAUSDT
MARKET/LIMIT BUY/SELL, specified-order GET, openOrders GET and specified-order
DELETE are verified against injected responses. Demo exchangeInfo and brackets
are fetched through the same Demo client, with rules bound to adapter account
identity. Unknown mode, available margin, configuration or risk limits block entry.
All account/rule examples in these tests are fixtures, not online account evidence.

## Recovery evidence

The executable evidence is in `tests/integrations/test_binance_demo_execution.py`:

- `test_persist_before_dispatch_and_lost_cancel_reply`: observes durable UNKNOWN
  identity from inside POST dispatch; a lost DELETE reply is resolved by GET,
  with exactly one injected cancellation.
- `test_submit_rejection_and_unknown_recovery`: explicit rejection is terminal;
  lost submit reply prevents repeat submission, then query-only recovery confirms
  the accepted order without another POST.
- `test_cancel_failure_requires_query`: ORDER_NOT_FOUND cancellation failure and
  server-ambiguous cancellation both require reconciliation.
- `test_partial_duplicate_terminal_and_restart`: repeated partial-fill report is
  idempotent; disconnected final fill restores exactly two units and fee-adjusted
  assets without replacement; repeated recovery leaves assets unchanged.
- `test_cross_process_identity_and_unknown_query`: a separate Python process
  reopens the protocol store, observes reconciliation-required identity and
  restores order status using GET only, with no write authorization.
- `test_partial_cancel_and_regressing_fill`: canceled partial execution preserves
  cumulative fill; a regressing quantity rejects and reopens reconciliation.
- `test_stale_duplicate_does_not_regress_terminal_state`: old duplicate events do
  not roll back a final fill.

Two existing recovery defects were fixed in place: identical REST confirmations
now clear UNKNOWN when they confirm the same durable state, and locally confirmed
rejections with no exchange order ID are not queried as accepted exchange orders.
Uncertain orders retain the existing fail-closed query requirement.

The existing environment suite verifies cross-environment and changed-key store
identity rejection. Existing Futures tests cover shared risk, signature construction,
fee completeness, orphan recovery and unconditional network-write rejection.

## Targeted result

2026-11-12: **80 passed in 4.91s**.

```console
python -m pytest tests/integrations/test_binance_demo_execution.py tests/integrations/test_binance_futures_trading.py tests/integrations/test_binance_environments.py -q
```

Only these directly related suites were executed. Tests use synthetic credentials,
in-memory or small temporary SQLite stores and one separate-process restore.

## Online work remaining

No Demo online submit/cancel/fill claim is made. Actual Demo position mode,
account-specific leverage brackets, symbol configuration and fresh executable
margin must be verified together before any later live Demo admission. P7-D01
public exchangeInfo and read success do not supply these missing conditions.

[P7-D03 minimum steps and execution contract](../../binance-demo-execution.md#p7-d03-minimum-online-integration)
require independent user authorization, a separately reviewed Demo-only network
capability, current account/rule checks, bounded order limits, query/cancel cleanup
and restart reconciliation. Production remains outside that authorization.
