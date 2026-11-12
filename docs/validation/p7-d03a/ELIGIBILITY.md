# Demo eligibility and quantitative risk observation

Formal financial admission and Production writes remain **BLOCKED**. Bounded manual official Demo integration uses the existing execution plan, account isolation and risk gates; quantitative reporting is not a universal unlocked certificate.

## Official contract

The Binance-owned connector at revision `f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179` declares `canTrade` as an optional strict boolean in the [V2 response](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/models/account_information_v2_response.py), described as whether trading is enabled. The [V3 response](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/models/account_information_v3_response.py) does not declare that field. HTTP success and margin cannot substitute for the explicit boolean.

The [official account API](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/api/account_api.py) specifies signed GET `/fapi/v2/account` and GET `/fapi/v1/apiTradingStatus`. The latter has an optional symbol parameter, weight 1 with a symbol and 10 without one. The observation omits symbol for account-wide reporting.

The [indicator container](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/models/futures_trading_quantitative_rules_indicators_response_indicators.py) declares symbol lists and `ACCOUNT` directly beneath `indicators`, with indicator names and strict-boolean `isLocked` fields in each row. These materials neither promise an exhaustive list nor define omitted indicators as unlocked. The relocated documentation page returned HTTP 202 with a WAF challenge and no document body; the accessible official source is cited instead.

## Admission before and after

| Evidence | Previous behavior | Current bounded Demo behavior |
| --- | --- | --- |
| Valid response, no applicable indicators | UNKNOWN global unlocked boolean blocks entry indefinitely | NOT_REPORTED; not an independent blocker |
| Reported `isLocked=false` | Attempts to prove every indicator unlocked | Corresponding named indicator is NOT_LOCKED only |
| ACCOUNT or order-symbol `isLocked=true` | Global boolean denial | Applicable concrete lock blocks submission |
| Another symbol locked | Could incorrectly block all orders | Does not block the target symbol |
| Invalid shape, nonboolean field or conflicting duplicate | UNKNOWN global boolean | UNKNOWN with error category; refresh fails and existing reconciliation gate closes |
| Authentication/signature, transport/server or rate-limit failure | No usable unlocked evidence | UNKNOWN; existing classified exception, cooldown and recovery apply, without automatic retry |

There is no required `quantitative_trading_unlocked` boolean and no VERIFIED_TRUE claim for universal unlocked coverage. Unreturned named indicators are NOT_REPORTED. New indicator names with valid booleans remain observable and an applicable true still blocks. Missing `indicators` or an invalid container is malformed, unlike an explicitly returned empty object/list.

Strict `canTrade=true` is still required; Demo account refresh reads V2 when V3 omits that field. Exchange filters, freshness, mode, configuration, margin, risk limits, order identity, account isolation, bounded authorization and uncertain-outcome recovery are unchanged. No new approval process is introduced.

## Actual read-only evidence

The [historical sanitized observation](demo-eligibility.json), at `2026-11-12T10:36:22.714272+00:00`, used three GETs: time, V2 account and apiTradingStatus without symbol; network writes: **0**.

- `account_can_trade`: **VERIFIED_TRUE**, from actual V2 boolean `canTrade=true`.
- NVDAUSDT and ACCOUNT lists were not returned. Under the corrected policy these scopes are **NOT_REPORTED**, not an unlocked assertion and not a standalone Demo blocker. The historical JSON retains its original interpretation rather than being rewritten as a new observation.

## Remaining real blockers

The prior observation did not establish an applicable lock, and missing scopes are no longer a blocker. The real bounded plan limits remain unset/unapproved; no active process-local Demo write capability has been established. A future authorized manual session must obtain fresh account/rules/market evidence through the existing path. Actual orders, cancellations, fills and restart recovery have not yet been exercised online. Formal financial admission and Production writes remain blocked independently.

## Verification

Final focused run: **135 tests passed in 4.00s** across preflight, Demo network admission, environment isolation and Demo execution/recovery. Coverage includes controller-path admission for NOT_REPORTED/NOT_LOCKED, applicable lock rejection, unrelated-symbol isolation, malformed/conflicting fields, authentication/signature/rate-limit/server/timeout failures, V2 canTrade fallback and account refresh with the observed empty indicator shape. Tests use in-process HTTP fixtures, not real order writes.
