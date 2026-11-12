# P7-D03A Demo network admission engineering evidence

**P7_D03A_DEMO_NETWORK_ADMISSION_ENGINEERING_ACCEPTED**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

Engineering acceptance covers the bounded network boundary and offline verification, not successful online placement, cancellation or execution. Actual execution admission remains **BLOCKED**.

## Execution boundary

The only execution host is `https://demo-fapi.binance.com`. HTTP redirects and host fallback are disabled. Production network writes remain unconditionally blocked; Production and Demo credentials cannot cross client environments. The original offline injection transport remains available with its separate authorization.

Network writes require an independent, nonserializable process-local capability. Environment variables and PaperStore cannot restore it. The required execution plan binds the credential-derived Demo account, NVDAUSDT, MARKET/LIMIT types, quantity, notional, total/open order counts, duration, expiry, virtual loss, margin occupancy, client-ID prefix and cleanup/recovery policies. Real execution limits remain unset and unapproved; test values are synthetic.

Admission traverses the existing TradingController and gateway risk path, same-client Demo rules and fresh account evidence. Missing trading eligibility or lock evidence blocks admission. Existing mode, leverage, margin, brackets and exchange filters remain enforced. A one-use exact-parameter transport admission follows durable order identity creation. No additional economic ledger or automatic trading process is introduced.

UNKNOWN / RECONCILE_REQUIRED uses GET reconciliation without blind resubmission. Cleanup is explicit: reconcile uncertain identities, then cancel known scoped orders while authorization remains active. Expiry or revocation also blocks cancellation; further cleanup writes require a newly reviewed bounded approval. Loss and margin bounds gate submissions rather than guaranteeing continuous liquidation or loss stops.

## Real read-only observation

Observed at `2026-11-12T08:04:01.772159+00:00`; all nine requests used GET and returned HTTP 200:

- `/fapi/v1/time`
- `/fapi/v1/exchangeInfo`
- `/fapi/v3/account`
- `/fapi/v3/positionRisk`
- `/fapi/v1/positionSide/dual`
- `/fapi/v1/symbolConfig` for NVDAUSDT
- `/fapi/v1/leverageBracket` for NVDAUSDT
- `/fapi/v1/openOrders`
- `/fapi/v1/apiTradingStatus` for NVDAUSDT

The sanitized observation is in [demo-readonly-preflight.json](demo-readonly-preflight.json). Private account bodies, balances, positions, order bodies, credentials, signatures and signed URLs are not retained.

NVDAUSDT was returned as TRADING / TRADIFI_PERPETUAL. Demo evidence confirmed one-way mode, known leverage and margin mode, known brackets and available margin, positive available margin, and no existing positions or orders. Production leverage, margin and risk tiers were not reused.

Two required facts remain UNKNOWN:

- `account_can_trade`
- `quantitative_trading_unlocked`

HTTP success does not establish trading eligibility. These UNKNOWNs and the unset real plan independently prevent execution admission.

## Offline verification and no-write evidence

Targeted command:

```sh
PYTHONPATH=src python -m pytest tests/integrations/test_binance_demo_network.py tests/integrations/test_binance_demo_execution.py tests/integrations/test_binance_futures_trading.py tests/integrations/test_binance_environments.py tests/trading/test_paper_control.py -q
```

Result: **157 passed in 7.20s**, exit code 0.

The HTTP contract tests replace the network opener with an in-process fixture and assert the literal Demo URL and disabled redirect handler. The ordinary protocol connector is replaced with a failure sentinel. POST/DELETE examples therefore exercise request construction and response handling without network writes. Coverage includes MARKET/LIMIT BUY/SELL, scoped cancellation, shared-controller bypass rejection, default-denied authorization, Production writes, environment/account mismatch, plan/filter/risk rejection, timeout/5xx UNKNOWN, 429 without retries, lost cancellation replies, repeated fill evidence, restart GET recovery, and nonrestorable authorization.

The real preflight constructs only ReadOnlyNetworkTransport and issues GET requests; its sanitized report records `network_writes: 0`. No real Demo POST/DELETE, Production write, automatic order/cancellation, leverage/margin adjustment, transfer, withdrawal or real-money test was performed.

## P7-D03B prerequisites

1. Resolve both UNKNOWN eligibility facts using authoritative Demo evidence; do not infer eligibility from successful reads.
2. Explicitly approve every real plan field, allowed order types, unique prefix and expiry. Choose quantity from current Demo lot/min-notional filters and price, then cap notional, margin, loss, count and duration within the approved budget. Synthetic fixture limits are not a proposal for online execution.
3. Refresh Demo rules, brackets, configuration, balances, positions, open orders and mark data in the authorized session. Bind the existing controller and create a new process-local capability only after approval.
4. Execute only the separately approved bounded sequence, query by the same persisted IDs, and reconcile UNKNOWN without replacement submissions.
5. Stop new submissions, GET-reconcile, explicitly cancel known scoped orders within active authorization, and verify fills/fees and restart recovery. If authorization expires first, obtain new bounded cleanup approval before any further write.

P7-D03B online write acceptance is not part of this engineering evidence.
