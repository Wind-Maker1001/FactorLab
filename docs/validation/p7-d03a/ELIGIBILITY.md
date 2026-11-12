# Demo eligibility evidence

Formal admission, execution admission and Production writes remain **BLOCKED**.

## Official contract

The Binance-owned connector at revision `f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179` declares `canTrade` as an optional strict boolean in the [V2 response](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/models/account_information_v2_response.py), described as whether trading is enabled. The [V3 response](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/models/account_information_v3_response.py) does not declare that field. Successful HTTP reads and account margin do not establish this boolean.

The [official account API implementation](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/api/account_api.py) specifies signed GET `/fapi/v2/account` and GET `/fapi/v1/apiTradingStatus`. The latter has an optional symbol parameter, weight 1 with a symbol and 10 without one. The bounded eligibility observation omits symbol to request account-wide coverage.

The [indicator container](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/models/futures_trading_quantitative_rules_indicators_response_indicators.py) declares symbol lists and an `ACCOUNT` list directly beneath `indicators`. Each row carries `indicator` and strict-boolean `isLocked` fields. The optional model declarations do not establish what omitted scopes or indicators mean, nor an exhaustive required indicator set. No missing-means-unlocked rule was found in these official materials.

The documentation site's relocated account page returned HTTP 202 with `x-amzn-waf-action: challenge` and no document body. Contract evidence above therefore cites the accessible Binance-owned generated source, not an unread document page.

## Interpretation

`account_can_trade` accepts explicit booleans only. Conflicting supplied account evidence yields UNKNOWN. Quantitative interpretation inspects both NVDAUSDT and ACCOUNT; incomplete, duplicate, conflicting, unrecognized or nonboolean evidence yields UNKNOWN. Consistent explicit locking in otherwise usable evidence yields VERIFIED_FALSE. All observed flags being false is insufficient to establish complete unlocked coverage.

TODO: Establish an authoritative exhaustive indicator coverage contract before allowing quantitative VERIFIED_TRUE; until then, all-clear observations remain UNKNOWN.

## Verification

Focused offline run: **61 passed in 0.60s**, covering `tests/integrations/test_binance_preflight.py` and `tests/integrations/test_binance_environments.py`, including unknown-indicator cases and environment boundaries.

Observed at `2026-11-12T10:36:22.714272+00:00`, using exactly three GETs: time synchronization, V2 account, and apiTradingStatus without a symbol parameter. All three reads succeeded; network writes: **0**. The [sanitized observation](demo-eligibility.json) retains field-presence flags, boolean conclusions and official sources, not private response bodies, credentials, signed URLs, balances, positions or orders.

- `account_can_trade`: **VERIFIED_TRUE**. V2 account contained `canTrade` with the actual boolean value `true`.
- `quantitative_trading_unlocked`: **UNKNOWN**. `indicators` was present as an object, but neither NVDAUSDT nor ACCOUNT provided a list. No explicit lock boolean was available for either scope. Read success does not resolve this missing evidence.

Formal admission, execution admission and Production writes remain **BLOCKED**.
