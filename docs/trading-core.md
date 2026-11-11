# Paper trading control v1

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

This package does not change financial admission. It contains no exchange
transport, credentials, live switch or real-account execution implementation.
`TradingController(..., live=True)` and non-paper execution adapters are rejected.

## Shared boundary

```python
from factorlab.trading import (
    CONTRACT_VERSION, Market, OrderIntent, OrderEvent, AccountSnapshot,
    RiskDecision, TradingAdapter, Side, OrderType, OrderStatus,
)
```

The versioned public contract is `src/factorlab/trading/contracts.py`;
`CONTRACT_VERSION` is `1`. The same types serve independently implemented
`BINANCE_USDM` and `BINANCE_EQUITY` adapters. One controller/store owns one market
and account. Instrument quote currencies are explicit; USD and USDT balances,
exposure budgets and fees are never converted or netted.

| Object | Fields |
| --- | --- |
| `OrderIntent` | `market: Market`, `instrument_id: str`, `side: Side`, `order_type: OrderType`, `quantity: Decimal`, `limit_price: Decimal \| None`, `client_order_id: str`, `time_in_force: str = "GTC"`, `position_side: str \| None = None`, `reduce_only: bool = False` |
| `OrderEvent` | `client_order_id: str`, `exchange_order_id: str \| None`, `status: OrderStatus`, `cumulative_filled_quantity: Decimal`, `fill_price: Decimal \| None`, `event_time: datetime`, `source: str`, `event_id: str`, `cumulative_fee: Decimal = Decimal("0")` |
| `AccountSnapshot` | `market: Market`, `account_id: str`, `assets: tuple[AssetBalance, ...]`, `positions: tuple[Position, ...]`, `open_orders: tuple[OrderSnapshot, ...]`, `observed_at: datetime`, `reconciled: bool = False` |
| `RiskDecision` | `approved: bool`, `reason: str`, `limits_snapshot: dict[str, str \| int \| bool]` |
| `TradingAdapter` | `submit_order(intent) -> OrderEvent`, `cancel_order(client_order_id) -> OrderEvent`, `query_order(client_order_id) -> OrderEvent`, `read_account() -> AccountSnapshot` |

All monetary, price and quantity values are Decimal, without float coercion.
All timestamps require timezone-aware UTC. Only GTC and one-way `None`/`BOTH`
positions are supported. `AssetBalance` exposes currency, total, reserved and
computed available. `Position` carries instrument ID, signed quantity, entry
price, mark price and quote currency. `OrderSnapshot` carries its intent, status,
cumulative quantity/fee, exchange ID and `reconcile_required`.

`fill_price` is the price of the **new quantity delta**, not a cumulative average.
An adapter receiving cumulative average prices must derive the incremental
notional/price before emitting an event. Fees are cumulative in the instrument's
explicit quote currency. A producer must supply an account-wide stable event ID.
Reusing the event ID with a different payload is an error. A new event ID with
unchanged cumulative quantities/fees does not book another fill.

Client order IDs are unique for the lifetime of a stored account, including
terminal orders. Repeating an identical confirmed intent returns its existing
confirmation; changing the intent under the same ID fails. Unknown orders cannot
be resubmitted. Reports cannot decrease cumulative quantity/fees or exceed the
intent quantity. A confirmed cancel may include additional partial fills; a late
full-fill report may amend CANCELED to FILLED. Cancel requests and failures alone
do not confirm cancellation.

## Paper execution and controls

```python
from factorlab.trading.control import TradingController, RiskLimits, RiskRejected
from factorlab.trading.paper import PaperBroker, Instrument, Quote
from factorlab.trading.storage import PaperStore
```

Construct `PaperBroker` with a market, account ID, explicit instrument/quote
currency definitions, balances and a Decimal fee rate (default `0.001`, a
synthetic rate, not an exchange fee assertion). Inject it into a controller with
an existing/new `PaperStore`, configured `RiskLimits` and optional UTC clock.
Set quotes with `controller.quote()`, submit with `controller.submit()`, and
inject fills explicitly with `controller.fill(order_id, quantity, price=None)`.
The default fill price is the observed quote; a limit-violating fill is refused.
Submission never assumes or automatically generates a fill. `reject`, `cancel`
and `replay` model the other confirmed outcomes. Controller methods provide the
transaction boundary; do not call mutating broker methods directly.

Equity simulation uses cash purchases, fractional Decimal quantities and owned
inventory sales; no short equity. USD-M simulation uses linear one-way signed
positions, realized PnL and **100% entry-notional collateral**, without crediting
sale principal. It is not a leverage, liquidation, funding, corporate-action,
order-book or contract-filter simulator. Explicit synthetic fills do not claim
exchange queue realism. Mark prices are retained for gross exposure valuation.

Risk checks include per-order notional, gross instrument exposure (positions plus
outstanding quantities), gross per-currency account budget, rolling 60-second
submission count, quote freshness, available funds/inventory, reconciliation and
kill switch. Amount/rate caps default to zero; missing configuration cannot open
risk. Quote age defaults to five seconds. Pending opposing orders do not reduce
risk exposure before execution. Rejections carry a `RiskDecision` in
`RiskRejected.decision`; risk rejection does not submit an exchange order.

`set_kill_switch(True)` blocks new risk but allows cancellation and verified,
explicit `reduce_only` closes. Such closes bypass opening caps and rate limits,
not quote freshness, inventory validation or reconciliation. Closing fees are
funded from sale proceeds/released collateral. Pending closes consume their
owned closing quantity only once. The switch is persistent. Disable it only
explicitly. Unreconciled accounts block submissions, including closes whose
quantity cannot be trusted; cancellation of a known confirmed order remains
available.

`TimeoutError` from an injected paper submit/cancel marks the order UNKNOWN and
`RECONCILE_REQUIRED`, preserving confirmed quantities and funds. A cancel that
lost a race to a fill cannot release that fill's accounting. `mark_timeout()`
provides direct deterministic timeout injection. Restart marks every active order
unknown and the account unreconciled. Call `reconcile()` to query the durable
paper authority. If there is no authoritative confirmation, reconciliation stays
blocked; neither restart nor reconciliation automatically sends a replacement.
This local paper authority is not a substitute for a future exchange query.

## Persistence

SQLite stores one current account snapshot and an event-ID journal. Confirmed
events, cumulative order state, positions, cash, reservations, submission times
and kill switch commit atomically. Failed event validation or insufficient funds
rolls back both accounting and event identity insertion. Write transactions load
the current durable projection before mutation. File/schema/account mismatches
fail rather than silently starting a new account. Use a private local SQLite
path; close the store with `store.close()` when finished.

Lifetime defaults cap a store at 1,000 orders and 10,000 events, with an 8 KiB
per-event limit and a 32 MiB snapshot limit. Configured count caps may be smaller,
not larger. Reaching capacity refuses the mutation and preserves prior state;
identity history is never silently discarded. Only bounded synthetic order
metadata/account projections are persisted, not raw market archives or duplicate
content indexes. No external database/service is required.

## Synthetic end-to-end demonstration

From an environment with FactorLab installed, or with `PYTHONPATH=src`:

```console
python -m factorlab.trading.demo
```

Both markets run `strategy intent → risk → paper broker → partial/full fill →
account reconciliation`, restart from SQLite, then close under the kill switch.
Assertions check funds, collateral, position quantities, entry prices, fees,
duplicate fill handling and final order states. Each starts with 1,000 quote
currency units, buys 2 at 100, sells 2 at 110, pays 0.42 fees and ends with 1,019.58,
no positions, no open orders and FILLED order states. Temporary demo stores are
removed on exit.

Targeted validation:

```console
python -m pytest tests/trading/test_paper_control.py tests/research/test_execution_risk_integration.py tests/research/test_grid.py -q
```

Simulation success conveys no live-money qualification.
