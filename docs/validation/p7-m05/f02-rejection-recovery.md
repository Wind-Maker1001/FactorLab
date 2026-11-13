# P7-M05 F02 rejection recovery

**P7_M05_BLOCKED** — `EXCHANGE_ERROR_CODE_NOT_RETAINED`.

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

Baseline: `ab0b307a076bb08eaebfabe1121c0e10b6184fb8`.

The original Demo POST for `m05-b1beecba538d4dca-01` was HTTP 400 at `2026-11-13T18:28:35.980891Z`. Its local event `reject:m05-b1beecba538d4dca-01` at `18:28:36.496725Z` records REJECTED, zero filled quantity, zero commission, and no exchange order ID. The private execution receipt, retained shell output and protocol order/event records contain no original Binance error JSON. The transport consumed the HTTP body, the protocol discarded `msg`, and the adapter did not persist the caught error code/category. No retained response cache was identified in those artifacts. The original Binance code, message and specific rejection classification cannot be reconstructed; HTTP 400 alone does not diagnose the cause.

## Independent exact-order observation

GET `/fapi/v1/order`, symbol `NVDAUSDT`, origClientOrderId `m05-b1beecba538d4dca-01`:

- HTTP **400**; code **−2013**; msg **Order does not exist.**; category **ORDER_NOT_FOUND**.
- This is a new query response, not the original POST error.
- It establishes absence in the current query scope, not lifetime absence or a complete historical audit.
- The full credential-derived account binding matched the original private policy; display suffix `660da077`. This is not an independently returned exchange UID.

The run completed at `2026-11-13T18:39:14.173576Z`: **GET=8, POST=0, DELETE=0, account-configuration writes=0**. It performed one server-time synchronization, one exact query, and one each of openOrders, allOrders, userTrades, income, positionRisk and account. No polling or retry.

All-account open orders and nonzero positions were empty. NVDAUSDT recent orders, trades and income were empty for `2026-11-13T18:28:00Z` through `18:39:11.115Z`, limit 100 per endpoint; no result reached that limit. Wallet and available balance were both **5000 USDT**, initial margin and unrealized PnL zero. These observations agree with the local zero-fill rejection; the original rejection event remains unchanged. `LOCAL_HISTORY_INCOMPLETE` remains applicable.

Private originals are retained locally under `D:/Code/FactorLab-final-evidence/p7-m05-f02-20261113T183847Z/private/`, with a separate sanitized review receipt. Raw account responses and complete account bindings are not published.

## Diagnostic repair and verification

Future definitive rejection events retain a bounded, sanitized HTTP status/code/message/category record linked to the client ID and local event in the same transaction. Exception strings still exclude remote messages. Credential/signature echoes, URLs, nonprintable or oversized messages are suppressed, not persisted verbatim.

An independent exact-ID evidence function and GET-only command support explicit online verification without changing order state or the existing terminal-REJECTED reconciliation skip. A successful account reconciliation is not online order acceptance. Missing/unknown query results do not become fills or successful admission.

Focused protocol and rejection-evidence tests: **63 passed**. Coverage includes rejection diagnostic persistence across restart, original REJECTED/zero-fill preservation, bounded secret-safe messages, exact query absence/errors/identity mismatch, and no query-path writes.

The original POST opportunity remains consumed. No second order, retry, replacement ID, cancellation or configuration write is permitted by this recovery. A second online order requires a new independent policy and authorization. Missing original code/message prevents `P7_M05_REJECTION_DIAGNOSED`; neither this recovery nor rejection reconciliation qualifies as `P7_M05_DEMO_ONLINE_ACCEPTED`.
