# Current rules and read-only account evidence

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

## Official protocol evidence

The following official SDK sources were retrieved through GitHub on 2026-11-12,
at revision `f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179`, OpenAPI 1.0.0.
These establish protocol contracts, not successful exchange/account access.

| Source | Verified contract |
| --- | --- |
| [Futures market API](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/api/market_data_api.py) | Public `GET /fapi/v1/exchangeInfo` |
| [Futures symbol model](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/models/exchange_information_response_symbols_inner.py) | Status, margin/quote assets, contract dates, order types, filters; `pricePrecision` is not tick size and `quantityPrecision` is not step size |
| [Futures account API](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/api/account_api.py) | Signed USER_DATA `GET /fapi/v3/account`, `/fapi/v1/leverageBracket`, `/fapi/v1/symbolConfig`, `/fapi/v1/positionSide/dual` |
| [Futures trade API](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/api/trade_api.py) | Signed position-risk and order queries; reading the protocol does not authorize mutation |
| [Stocks market API](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks/src/binance_sdk_stocks/rest_api/api/market_data_api.py) | `GET /sapi/v1/equity/market/exchangeInfo`, `/sapi/v1/equity/market/quote`; API-key market data |
| [Stocks symbol model](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks/src/binance_sdk_stocks/rest_api/models/exchange_info_response_symbols_inner.py) | BUY_SELL/BUY/SELL/NONE tradability; quantity step/bounds, USD notional bounds, reference-price multipliers, fractional/extended/overnight flags and open-order limit |
| [Stocks market streams](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks/src/binance_sdk_stocks/websocket_streams/streams/market_streams_api.py) | `calendar` broadcasts phase transitions, not periodic heartbeats; no locally inferred exchange calendar |
| [Stocks account API](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks/src/binance_sdk_stocks/rest_api/api/account_api.py) and [model inventory](https://github.com/binance/binance-connector-python/tree/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks/src/binance_sdk_stocks/rest_api/models) | Account API contains disclaimer signing only. No funds, security holdings, sellable-quantity or settlement read contract was found in this pinned SDK |
| [Stocks trade API](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks/src/binance_sdk_stocks/rest_api/api/trade_api.py) | MARKET/LIMIT request matrix, two-decimal limit price, RTH/EXTENDED/24H sessions. `quoteAsset` defaults to USDC and must belong to the server-allowed set. BUY wallet defaults to CARD; SELL settles to CARD |

## Effective coverage and UNKNOWNs

Futures parsing uses exact decimal filter values for LOT_SIZE, MARKET_LOT_SIZE,
PRICE_FILTER and MIN_NOTIONAL, separately retaining order types and trading
status. Public exchangeInfo cannot prove account-specific leverage brackets,
notional coefficients, margin mode, balances, position mode or account permission.
`TRADING` is current status, not evidence of a complete future trading schedule.
Contract dates and any additional exchange fields remain in the raw receipt.
Missing fields do not supply permission or numeric defaults.

Stocks observations preserve symbol tradability and session/fractional flags,
quantity/notional bounds and price multipliers. A phase transition establishes
only the reported phase, not holiday schedules or future opening/closing times.
Its original source timestamp and payload remain separate from quote and rule
watermarks. Lack of an initial calendar message leaves the phase UNKNOWN.
Account permission, funds, holdings, sellable quantity, settlement timing and
account-specific payment assets remain UNKNOWN.

USD notional units do **not** establish USD payment eligibility. The documented
USDC default likewise does not prove the account's accepted asset set, available
USDC or permission to spend it. The USD paper ledger is not converted into USDC,
and neither is combined with Futures USDT. Without explicit Paper Simulation
Policy, public-mode Stocks admission remains blocked. Simulation eligibility
never makes the `quoteAsset=USD` request fixture live payment evidence.

## Acquisition and restart contract

```console
python -m factorlab.trading.realtime_service --state-dir ./paper-observation --public-market-data --refresh-rules --seconds 30 --reconnects 0 --paper
```

This opts into market-data connections, not private-account access. Rule refresh
performs one GET per market, with ten-second HTTP timeout and no retry. Stocks
uses the existing market-data key requirement. Current rules, parsed values and
symbol-scoped response receipts use the existing PaperStore snapshot, not a new
market database. Receipts retain only the requested symbol and explicitly record
the returned versus retained symbol counts; unrelated instruments are not copied
through every market-event transaction. Each receipt identifies endpoint, pinned protocol version, request time,
fetch time, original server time when present and local five-minute expiry.
Local fetch time is not a historical rule effective date.

A source URL or serialized `verified` flag cannot grant provenance. Runtime trust
is established by the current process's fixed-host reader path. Injected transports
are explicitly OFFLINE_INJECTED and cannot authenticate public-mode rules. Failed
refresh invalidates previous rule approval. Restart clears rule/session approval
while retaining independent source watermarks and paper accounting. Old JSONL
bootstrap files cannot restore trust. Quote age remains five seconds; rules and
sessions expire after five minutes. Calendar silence never extends that expiry.

## Account read-only verification

Default tests use injected responses and synthetic credentials. Signed GET
contracts cover account, position risk, leverage brackets and order reads,
including timestamp, recvWindow, API-key header and HMAC-SHA256 query signature.
The existing adapter's account reconciliation and order-read capabilities remain
available. Credentials and signed URLs are excluded from representations/errors.
Network transport requires explicit account-read permission, fixed allowlisted
GET paths and disabled redirects. Network POST/DELETE/PUT, order placement,
cancellation, margin-mode changes, transfers and withdrawals are blocked before
opening a connection.

No real-account access was authorized or performed for this change. Offline
contract validation is not real-account integration approval. A future authorized
check must have an explicit request/time budget and publish only capability
outcomes, never credentials, signatures or private account payloads.

## Explicit Paper Simulation Policy

```console
python -m factorlab.trading.realtime_service --state-dir ./new-paper-simulation --public-market-data --refresh-rules --paper --paper-simulation --acceptance-signal --seconds 60 --reconnects 2
```

`--paper-simulation` permits hypothetical account eligibility only in the local
paper runner. It does not authorize an exchange request. Every reported account,
fill, fee, slippage and risk outcome is simulated. The accounts start separately
with 1,000 USDT for NVDAUSDT and 1,000 USD for NVDA. Fees default to a synthetic
0.001 of fill notional and adverse slippage to 0.0001 of the executable quote;
quote participation is hypothetical liquidity, not a reported exchange execution.

Futures use full entry-notional collateral and a synthetic 1,000-USDT capital
ceiling, not an exchange risk bracket. Real account brackets remain UNKNOWN.
There is no leveraged liquidation or funding model. Mark price remains separate
from the executable trade price. Stocks assume paper permission and, if the
public response has no reference price, use the current quote as an explicitly
synthetic price-band reference. Payment eligibility and settlement remain UNKNOWN;
no custody or real settlement mechanism is simulated.

Official current public rules, valid public session evidence, freshness, symbol
filters and shared controller risk limits are still required in public mode.
An unavailable Stocks market-data key can therefore yield NO_TRADE even though
paper permission is assumed. Synthetic sessions belong only in offline scenarios.
Restart requires the same explicit simulation configuration, clears public
rule/session approval and preserves order identities; UNKNOWN orders remain
query-only. A serialized policy or source label cannot restore public provenance
or enable live execution.

`--acceptance-signal` is an opt-in test trigger, not a predictive investment
strategy. A valid executable quote at least 30 source-time seconds after the
first quote can generate one BUY signal if no natural strategy signal occurred.
It cannot fill on its trigger event. Subsequent liquidity, price, rule and risk
conditions still determine whether any simulated fill occurs. Without this flag,
the existing price-change strategy is unchanged. The simulation CLI bounds each
market to at most 60 seconds and two reconnects.

Report offline deterministic closure, online data, online orders, online fills
and account reconciliation separately. A missing observation is NOT_VERIFIED,
not evidence of a successful online trade.
