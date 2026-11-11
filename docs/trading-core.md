# Paper trading control v1

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

This package does not change financial admission. The realtime service permits
explicitly enabled market-data connections only; Stocks market data requires its
API key. It has no live switch or real-account execution path. `TradingController(..., live=True)`
and non-paper execution adapters are rejected.

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

## Realtime paper service

```console
python -m factorlab.trading.realtime_service --state-dir ./paper-state --replay examples/realtime_paper_replay.jsonl --paper
python -m factorlab.trading.realtime_service --state-dir ./paper-observation --public-market-data --seconds 30 --reconnects 2 --paper
```

Without `--paper`, observations do not run the strategy. Without
`--public-market-data`, no network is opened. Install the `binance-equity` extra
for WebSocket support. A public observation is bounded to at most 86,400 seconds
and ten reconnects per market; another invocation resumes the same account.
Ctrl-C closes both stores after saving STOPPED. Separate state directories create
separate simulations, not copies of an existing account.

The offline fixture is explicitly synthetic and contains 19 events, not historical
market evidence. Replay time is each event's UTC receipt time. JSONL records use
`symbol`, `kind`, source `time`, and `received`; exact prices/quantities are decimal
strings. `trade` records have price, size, and aggregate-trade sequence. `quote`
records have bid, ask, bid_size, and ask_size. `mark` is risk reference only;
`session` contains the normalized Stocks phase. `rules` records carry an existing
Stocks exchangeInfo row or a Futures symbol/bracket pair plus source provenance.
Rules are observations, never retroactively applied historical rules. Rule data
expires after five minutes, executable observations after five seconds, and Stocks
calendar observations after five minutes. Missing/unknown calendar or rule information
blocks orders; no Futures price can substitute for a Stocks quote.

The strategy compares two consecutively accepted executable observations. A rise
of at least 0.1% while flat requests 0.1 shares/contracts; a fall of at least 0.1%
while holding requests a close. Only one order may be active. Stocks uses GTC
EXTENDED limit orders rounded to cents; Futures uses market orders. Source time
must not exceed receipt time, and receipt time must not exceed the runner clock.
Klines are not strategy inputs. Duplicate/older source watermarks do not rerun the
strategy. Stocks source time and server emission time form an ordering watermark,
not a unique quote sequence: up to 128 content identities at that watermark are
retained for deduplication, including across restart. Exceeding this bound pauses
that account instead of growing a quote cache. Trade sequence gaps and connection loss pause only that market; explicit
`control('start')` is required to resume. Disconnect clears the previous strategy
price, preventing a signal spanning an unobserved interval.

A later executable source event, strictly newer than the order's last confirmation,
provides a hypothetical FIFO liquidity budget of 10% of its trade size (Futures) or
minimum bid/ask displayed size (Stocks). The budget is shared across pending orders.
Market fills use adverse 1 bp slippage; limit fills occur only when that slipped
price satisfies the limit. Fees are the PaperBroker synthetic 0.1% rate. Fractional
stock fills respect the observed quantity step. Stocks market buys retain the
existing fixed admitted-price/notional convention, rather than claiming unknown
share quantities from a notional exchange order. Quotes are never exchanged for
real execution confirmations: only the local simulator creates paper events.
The model does not infer intrabar ordering, queue position, leverage liquidation,
funding, or real stock settlement (UNKNOWN).

`RealtimeAccount.control()` exposes start/pause/stop and kill/unkill;
`health()` is read-only, including signal/order/fill/rejection counters and the
account projection. `tick()` detects silent stale feeds. `cancel()` and `replay()`
use the existing broker/state machine. All mutations, strategy counters and source
watermarks commit in the same PaperStore transaction as account and order changes.
No raw feed history is saved. WebSocket queues are limited to eight messages and
64 KiB per message; existing order/event/storage lifetime caps remain in force.

The **PaperStore snapshot and confirmed-event journal** are the sole paper
execution authority. Restart verifies the snapshot checkpoint and each last
confirmation against its journal entry, then reconciles only locally confirmed
orders. An order without a confirmation stays UNKNOWN and cannot be resubmitted.
Cash, positions, fees, kill switch, source watermarks and strategy state survive a
complete process exit. Runtime configuration changes are rejected for an existing
account. Concurrent/stale writers stop on a projection conflict instead of
replacing another writer's state. A checkpoint or confirmation conflict rejects
startup with PAUSED and preserves the conflicting store for inspection; it does
not invent an empty replacement account. A checkpoint detects accidental state
conflict, not adversarial database tampering.

Public sessions subscribe only to unsigned Futures aggregate trades/marks and the
existing Stocks quote/calendar streams. They never instantiate signed trading or
account clients. Offline/public mode and risk/simulation settings are persisted;
opening an offline account in public mode is rejected. `--refresh-rules` performs
one bounded official exchangeInfo GET per market, without retries. Stocks requires
`BINANCE_EQUITY_API_KEY` for market data. Embedders may call
`RealtimeAccount.refresh_rules(network=True)` on demand. The reader records the
endpoint, protocol version, request/fetch times, raw response, parsed rules and
five-minute expiry in the existing PaperStore snapshot. Fetch time is explicitly
local acquisition time, not an invented exchange effective time. A failed refresh
invalidates the previous rule authorization. Each market refresh fails independently.

`--observations FILE` and `event()` remain unverified inputs: neither a URL in
`source` nor serialized evidence can authenticate their provenance. Only the
current process's reader path authorizes an acquired observation. Rules and
sessions are invalidated on every restart, independently of persisted watermarks.
Public calendar events retain their original payload and endpoint separately from
rules and quote watermarks. A quiet calendar stream is not a heartbeat; after
five minutes it must be re-observed rather than extrapolated from a local clock.

Public Futures exchangeInfo does not prove account-specific leverage brackets;
public Stocks metadata does not prove account permission or payment/settlement
eligibility. These remain UNKNOWN and block public-mode orders. Offline injected
fixtures can exercise the same simulator without being promoted to exchange or
account evidence. See [rule and read-only evidence](binance-rule-observations.md)
for verified protocol coverage and the unresolved acceptance conditions. Output distinguishes
received messages, accepted events, strategy signals, simulated orders/fills and
paper reconciliation. NO_TRADE is a valid observation, not a fabricated fill.
