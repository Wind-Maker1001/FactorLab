# Binance Stocks: underlying shares, paper execution only

`NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED`

## Protocol authority

The official [Stocks SDK](https://github.com/binance/binance-connector-python/tree/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks) is pinned to `f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179`, OpenAPI 1.0.0. [Trade definitions](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks/src/binance_sdk_stocks/rest_api/api/trade_api.py) and [official reference](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/trade#place-equity-order) define the request matrix. SDK snake_case parameters serialize to camelCase through `common/src/binance_common/utils.py`; signatures cover the URL-encoded payload using HMAC-SHA256, timestamp and recvWindow.

| Capability | Official interface | Local boundary |
|---|---|---|
| Rules, minimums, fractional increments | GET `/sapi/v1/equity/market/exchangeInfo` | Existing Equity reader; `Rules.from_exchange_info` |
| Quote / calendar | Existing Equity REST and quote/calendar streams | Existing normalized rows, source time and receive time retained |
| Place | POST `/sapi/v1/equity/order/place` | Offline template/signature only; live call always rejects |
| Cancel | POST `/sapi/v1/equity/order/cancel`, `orderId` | Offline signature only; live call always rejects |
| Detail | GET `/sapi/v1/equity/order/detail` | Injected signed read by exchange or client ID |
| Open orders | GET `/sapi/v1/equity/order/open-orders` | Injected signed read |
| Order history | GET `/sapi/v1/equity/order/history` | Explicit startTime/endTime; current/size pagination |
| Executions | GET `/sapi/v1/equity/trade/history` | Exact execution quantity, price, notional and timestamps |
| Order reports | POST `/sapi/v1/equity/listenKey`, then `<listenKey>@orderReport` | Decoder only; no listen-key network creation or new collector |
| Cash / holdings / sellable / settlement | No corresponding read in this pinned Stocks SDK | Explicitly unavailable; no invented endpoint or real balance |

MARKET BUY requires `notional` and forbids quantity; MARKET SELL requires quantity and forbids notional. Both forbid price/tradingSession and use DAY. LIMIT requires price/quantity/tradingSession. Fractional GTC requires EXTENDED or 24H. Ordinary underlying-stock requests always include `tokenize=false`; no mint/redeem, disclaimer acceptance, transfer, cancel-all or tokenized asset operations are implemented.

### USD is not USDC

Instrument valuation identity is `STOCK_SPOT / USD / SHARES`, not perpetual `USDT / CONTRACT_QUANTITY`. The official order endpoint defaults quoteAsset to USDC and leaves its allowed set server-defined. **USD payment eligibility remains unverified.** USD request templates are offline artifacts, not approved executable exchange requests. Real placement remains unconditionally disabled; the paper path explicitly models synthetic USD, never a USDC balance relabeled USD.

### Account and execution boundaries

`ReadClient` has no default network implementation. Its injected transport accepts a `SignedRequest` and returns decoded objects; decode numeric WebSocket payloads with `json.loads(..., parse_float=Decimal)` to avoid float precision loss. Authentication is loaded through `Credentials.from_environment()` using `BINANCE_EQUITY_API_KEY` and `BINANCE_EQUITY_API_SECRET`. Do not log signed query strings or headers. Transport errors and authentication echoes are replaced with sanitized diagnostics.

Order detail supplies cumulative filledQty/filledTotal/fee. Delta prices use differences of cumulative notional and quantity, not cumulative VWAP. Order-report streams provide no commission field: `tc` is cumulative buy cost, not a generic sell fee. Stream fees and all settlement states remain unknown. History fees are also unknown. Unknown statuses, inconsistent query versions or cumulative regressions block recovery; an existing or uncertain identity can only be queried, never automatically replaced. A late FILLED report after CANCELED can reconcile the cancel/fill race. `contract_event` exports only known-fee fixed-share-compatible reports; actual variable-share notional fills require a future compatible public contract, not mutation of the frozen one.

## Shared core integration

`paper.py` requires P7-T01 `factorlab.trading` v1 (frozen contract commit `c8cf06a`, exercised with `334eb2a`). Protocol imports work independently before that dependency merges. Integration tests skip explicitly if the shared package is absent. Do not copy its contracts into this module.

`StockPaperBroker` extends the shared `PaperBroker`; `TradingController` owns risk admission, the bounded paper store, event deduplication, reservations and atomic account projection. `StockAdapter` exposes v1 submit/cancel/query/account methods. Bind each new intent to explicit `Observations` before submission. Both quote source time and receive time must be within five seconds. Missing quote source time rejects (including timestamp-less REST quotes). Rules and phase require source time as well; no local calendar opens trading. Rules refresh within 24 hours and phase observations within 12 hours are explicit conservative simulation policies, not exchange guarantees. Feed disconnection must call `disconnect`; recovery never silently clears the kill switch.

Supported simulation sessions normalize price `mp` codes O/PRE/POST/ON/C separately from Calendar MARKET_OPEN/PRE_MARKET/POST_MARKET/OVERNIGHT/CLOSED and independently check order RTH/EXTENDED/24H plus symbol capabilities. Market orders outside regular hours and overnight fractional orders are blocked pending verified eligibility. Exchange min/max quantity/notional, step, maximum open orders and explicit reference-price multipliers apply. Supply the exchange-approved reference price separately; the bid/ask is not silently substituted as the price-band reference.

MARKET BUY simulation requires explicit notional equal to the fixed simulated quote times public intent.quantity; subsequent fills must use that fixed price. This restriction preserves both notional and v1's fixed-share semantics without claiming real execution equivalence. Shared store restart retains identities and blocks execution without lost protocol context; restored orders are query/cancel-only. `synthetic_holdings()` subtracts pending sells and preserves unknown settlement; it does not assert real settled/sellable inventory. No T+1, 100-share lots, A-share tax or price limits are imported.

FeeSchedule currently expresses A-share research charges including a CNY minimum; applying it to USD stocks would import the wrong fee contract. Paper fees instead use the existing shared core's explicitly synthetic decimal fee rate. No standalone fee engine is added. ExperimentTracker is not instantiated: this quote-only executable demonstration has no research experiment consumer or history artifacts to register.

## Minimal offline demonstration

After integrating P7-T01:

```sh
PYTHONPATH=src python -m factorlab.integrations.binance_equity_trading.demo
PYTHONPATH=src python -m pytest tests/integrations/test_binance_equity_trading.py tests/integrations/test_binance_equity_trading_core.py tests/integrations/test_binance_equity.py -q
```

The example uses synthetic NVDA messages through the existing Equity normalizer, an ask-threshold signal, public risk limits, stock request validation, shared fake execution, partial fill events and USD reconciliation. It writes no market data or experiment bookkeeping files. Its paper store is in-memory. Expected purchase: 0.5 shares at USD 150, synthetic fee USD 0.075, remaining cash USD 924.925, zero reserved funds, settlement unknown.

Unverified real boundaries remain USD payment eligibility, account Stocks permissions, real cash/holdings/sellable/settlement endpoints, actual fees and executions, nonregular market-order eligibility, and overnight fractional eligibility. Permission failure is reported, not bypassed.
