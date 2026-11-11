# USD-M futures protocol adapter

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

This adapter does not change admission, enable real trading, or qualify a model
for live money. There is no live submit/cancel/configuration implementation.
Network access defaults off. Only an explicitly permitted fixed-host authenticated
GET transport exists. Do not pass account credentials to synthetic examples.

## Callable interfaces

```python
from factorlab.integrations.binance_futures_trading.protocol import (
    Credentials, FuturesClient, OfflineTransport, ReadOnlyNetworkTransport,
    SafetyBlocked, ExchangeError, OutcomeUnknown,
)
from factorlab.integrations.binance_futures_trading.adapter import FuturesAdapter
from factorlab.integrations.binance_futures_trading.rules import (
    RuleObservation, observe_rules, current_tier, order_parameters,
)
from factorlab.integrations.binance_futures_trading.stream import UserDataEvents
from factorlab.integrations.binance_futures_trading.gateway import FuturesPaperGateway
```

`FuturesAdapter` exposes the shared `submit_order`, `cancel_order`, `query_order`
and `read_account` contract. Additional reads are `read_facts`, `read_activity`,
`open_orders`, `all_orders`, `user_trades`, `income`; recovery uses `disconnect`,
`reconcile`, and `order(client_order_id)` for persisted status. Create it with
`FuturesClient`, a dedicated `factorlab.trading.storage.PaperStore`, an account ID
and an optional UTC clock. All mutations require an injected `OfflineTransport`.

`Credentials.from_env()` reads `BINANCE_API_KEY` and `BINANCE_API_SECRET`.
Representations redact credentials and signed query strings. Responses/errors do
not echo server messages or signed URLs. Credentials/signatures never enter the
order journal. `FuturesClient.build()` creates exact sorted URL-encoded HMAC-SHA256
requests with `timestamp` and `recvWindow`; `sync_time()` estimates server offset
using the midpoint of request start/end. It does not silently adjust time after
an error or automatically resend an order. Clock/signature/auth/filter/margin/
reduce-only errors are classified by code. HTTP 418/429 establish a Retry-After
cooldown (seconds or HTTP date); calls during cooldown fail without network IO.
No request is automatically retried. Transport failure, ambiguous server failure
or unusable order response becomes `OutcomeUnknown`.

The real read transport requires **separate explicit account-read permission**:
`ReadOnlyNetworkTransport(permit_account_read=True)`. Merely constructing it is
not permission to perform reads. A user must authorize account access first.
The transport pins the existing USD-M host, refuses redirects, and independently
refuses POST/DELETE/configuration/transfer paths. Client-level gates enforce the
same boundary. No account read permission is needed for the offline tests/demo.

## Verified official protocol source

Contracts were inspected in Binance's official connector at commit
[`f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179`](https://github.com/binance/binance-connector-python/tree/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures)
using the GitHub API. The developer-document site timed out; the pinned official
connector supplies the endpoint/parameter and model contracts used here.

- [Trade API](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/api/trade_api.py):
  POST/DELETE/GET `/fapi/v1/order`, GET `/fapi/v1/openOrders`,
  `/fapi/v1/allOrders`, `/fapi/v1/userTrades`, `/fapi/v3/positionRisk`.
  LIMIT requires quantity/price/timeInForce; MARKET requires quantity. `RESULT`
  can return an already-filled market order. `positionSide` is BOTH for one-way,
  LONG/SHORT for Hedge; `reduceOnly` cannot be sent in Hedge Mode. Client IDs must
  match the exchange's 1–36-character allowed format. Order IDs are per-symbol.
  A query may no longer find canceled/unfilled orders after three days or orders
  older than 90 days: absence is not proof that an uncertain order never traded.
- [Account API](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/api/account_api.py):
  GET `/fapi/v3/account`, `/fapi/v1/positionSide/dual`,
  `/fapi/v1/symbolConfig`, `/fapi/v1/leverageBracket`, `/fapi/v1/income`.
- [User-data models](https://github.com/binance/binance-connector-python/tree/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/websocket_streams/models):
  `order_trade_update_o.py` provides cumulative `z`, last quantity `l`, last
  price `L`, commission `n`/`N`, status `X`, execution type `x`, trade time `T`,
  position side `ps`; `account_update_a.py` identifies changed balances `B`,
  positions `P`, and update reason `m`.
- [Position-risk v3 model](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/models/position_information_v3_response_inner.py)
  supplies mark/notional/liquidation fields. Leverage and margin type are read
  separately from symbolConfig, not invented from the v3 position response.

This is source-contract verification, not a claim that authenticated calls or
account-specific exchange permissions have been tested. Actual account API-key
permissions, regional/TradFi eligibility, stock-contract agreement status and
current personalized trading filters/brackets were not queried. No agreement,
leverage, margin-mode or other account-setting mutation is allowlisted.

## Shared contract boundary and risk

This implementation depends on P7-T01 interface commit
`c8cf06a17b99c443f4274dfb1c9b487eda18b20b` and its paper implementation
`334eb2a8884bce091bfde94df2bb76687820a580`; shared objects are imported, not copied.

Shared v1 allows only one-way BOTH positions. `order_parameters(...,
hedge_mode=True, hedge_side='LONG'|'SHORT')` validates exchange Hedge combinations
without inventing a second OrderIntent. Actual shared execution/reconciliation
rejects Hedge accounts. Minimal future shared-contract change: permit LONG/SHORT
position identities and preserve that identity in shared Position/account state;
merely relaxing OrderIntent validation is insufficient. Until that shared change,
Hedge support is protocol-validation only. Public shared types are unchanged.

`AccountFacts` retains exchange assets, positions, risk, symbol configuration,
open orders and optional actual trade/income history. `usdt()` and
`position_details()` expose reported available balance, wallet balance/equity,
unrealized PnL, initial/maintenance margin, leverage, margin mode and liquidation
reference. Missing fields stay `None` (UNKNOWN). Incomplete one-way account data
cannot claim a reconciled shared snapshot. Liquidation reference is displayed
only from the exchange field, including an explicitly supplied zero; no local
liquidation calculation is certified. `current_tier` uses reported notional and
versioned observed bracket metadata, returning UNKNOWN if not available.

`RuleObservation` binds filters and brackets to source version and UTC observation
time. `observe_rules` reuses existing `binance.client.PublicClient.exchangeInfo`
and signed bracket GET; it never downloads historical bars. Quantity/price step,
min/max, min notional, leverage, bracket exposure and available-margin checks use
Decimal. Default max leverage is 1; unknown/stale rules, missing account values,
or unverified custom `notionalCoef` block admission. Snapshot rules are **current
observations, not historical rules**. Synthetic fixture values are explicitly
labeled and must not be used as actual NVDAUSDT trading rules.

`FuturesPaperGateway` subclasses the existing PaperBroker and is injected into
P7-T01 TradingController. The controller first enforces shared risk, budget,
quote freshness and kill switch; the gateway then verifies current exchange-rule
metadata and the reported one-way account/margin configuration before any fake
POST. This retains the existing fully collateralized paper account model, not a
new derivatives strategy/backtest engine. A distinct small PaperStore holds
protocol identities while the controller's store remains the sole paper funds/
position projection. Gateway fee consistency is deliberately limited to the
configured synthetic paper fee rate, not arbitrary real account commissions.

## Events, timeout and restart

Client IDs are lifetime-unique in the local journal, stricter than the exchange's
open-order uniqueness. The ID/intent is committed before fake POST. Repetition
queries an existing identity, never posts it twice. Unknown identities block new
submissions. No automatic retry, replacement, order clearing or status inference
occurs after timeout. A rejected cancel does not release paper reservations;
query/reconciliation establishes the actual order/fill status.

REST execution uses actual cumulative quantity and cumulative quote to derive
incremental fill price. Filled REST responses require matching account trades,
commission currency USDT and complete cumulative quantities/notional before fees
can project into shared OrderEvent. Missing/truncated history or non-USDT fees
blocks reconciliation rather than inventing zero fees. Reads are bounded to 1,000
trades/income records; a full page requires explicit pagination outside this
minimal adapter and is rejected as incomplete. The protocol store inherits the
shared 1,000-order / 10,000-event limits; no market archives are indexed.

`UserDataEvents.parse(message)` consumes authenticated user-data JSON, deduplicates
persistently, and rejects cumulative jumps/late/conflicting reports. Public mark
or funding messages are never accepted as execution evidence. ACCOUNT_UPDATE is
a partial delta, so it closes the gate and requires full account reread rather
than pretending the changed rows are a complete AccountSnapshot. `disconnected()`
and listen-key expiry also close the gate. No live WebSocket/listen-key lifecycle
is started. Binance provides no replay cursor here, so reconnect always performs
REST order/account reconciliation. Public historical funding **rates** remain in
the existing research reader; only authenticated income `FUNDING_FEE` records are
account funding **cash flows**.

Adapter restart marks active orders UNKNOWN and the account unreconciled.
`adapter.reconcile()` queries every stored identity, complete fills and the
account; missing orders, incomplete fields or unmanaged open orders leave the
gate closed. For the integrated paper gateway use `gateway.recover(controller)`:
it replays only REST-confirmed missing deltas, including an already-sent protocol
identity missing from the separate paper commit after a crash. It compares cash,
positions and identity sets; it never sends a new order. No assumed fill or
replacement is used. A mismatch remains blocked rather than being silently
normalized.

## Offline validation and example

```console
python -m pytest tests/integrations/test_binance_futures_trading.py tests/trading/test_paper_control.py -q
PYTHONPATH=src python -m factorlab.integrations.binance_futures_trading.demo
```

The example uses synthetic credentials, rules and transport, not the preserved
90 daily bars / 2,160 hourly bars / 275 public funding-rate observations. It sends
an NVDAUSDT signal through shared risk and fake signed order submission, consumes
0.75 + 1.25 actual-shaped trade updates, and reconciles quantity 2 at entry 100,
wallet 999.8 USDT, collateral 200 USDT, fee 0.2 USDT, FILLED status and no open
orders. Temporary metadata files are removed at exit. No history downloads,
new dependencies, CI workflows or real-account requests are needed.
