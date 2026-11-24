# Alpaca Paper execution

Only `https://paper-api.alpaca.markets` is accepted. There is no live-trading switch. Alpaca, not the local paper matcher, is the execution authority. Market identity is `Market.ALPACA_US_EQUITY`; Binance Equity and USD-M remain separate.

## Credentials and startup

Set `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, and optionally `APCA_API_BASE_URL` in the process environment. Never put credentials in source, evidence, a command-line argument, or a committed configuration file. `AlpacaConfig.from_file(path)` accepts an explicitly provisioned private JSON file with the same variable names; protect it with OS permissions and keep it outside the checkout. Environment variables are preferred.

```python
from decimal import Decimal
from factorlab.integrations.alpaca import AlpacaAdapter, AlpacaConfig, PaperHTTP
from factorlab.trading.control import RiskLimits

adapter = AlpacaAdapter(
    PaperHTTP(AlpacaConfig.from_env()),
    "private-runtime/alpaca.sqlite",
    limits=RiskLimits(Decimal("10"), Decimal("20"), Decimal("20"), 10),
)
account = adapter.recover()
```

Use one active execution owner per official account; separate processes/strategies coordinate account-wide reservations through their shared execution owner. A SQLite database must not be copied while active. Restart with the same database and credentials, and reconcile before submitting new orders.

## Orders and cash limits

`OrderIntent` uses Decimal quantities and explicit `DAY` for fractional equity orders. Whole-share market/limit orders accept `DAY`, `GTC`, `IOC`, `FOK`, `OPG`, and `CLS`; Alpaca marks the last four as permission-dependent. Fractional orders require a fractionable asset, `DAY`, and at most nine quantity decimal places. Limit prices use a cent tick at $1 or above and a $0.0001 tick below $1. Extended-hours and derivative flags are not submitted.

Use `submit_order(intent)` for limit orders. Quantity-based market orders require `reservation_price=Decimal(...)`, a caller-supplied conservative execution-price estimate; unlike a limit price, this is not a venue-enforced price bound. A price-bounded cash allocation should use a limit order. Buy admission uses actual USD cash less outstanding local/external order reservations, equity, and `RiskLimits`, never margin buying power. Sells cannot exceed holdings less pending sells. An external market buy without a known reservation prevents further buy admission until reconciled/finished.

The order identity is committed before HTTP POST. A timeout, connection loss, ambiguous error, or 5xx keeps it UNKNOWN. Query/reconcile the same identity; never generate a replacement client ID to escape an unknown result. Even a subsequent 404 is not proof that an in-flight POST cannot arrive. Definite HTTP rejection is durable and releases the reservation. `OrderNotFound` is distinct from transport uncertainty.

## Recovery and accounting APIs

- `read_account_details()` returns official equity, cash, buying power, status, block flags, and observation time. `read_account()` implements the common `AccountSnapshot` contract.
- `recover()` imports paginated official order history, queries unresolved/open local identities, imports paginated FILL activities, and checks cumulative fill quantities and amounts. Cash and holdings come from fresh official account/position reads; they are not reapplied from order responses.
- `query_order(id)` returns an `OrderEvent`. Its `fill_price` prices only the increment since **this adapter's** last persisted cumulative observation. It is not a replay cursor for another consumer.
- `read_order_details(id)` returns stable cumulative `filled_quantity`, `filled_avg_price`, and `cumulative_filled_notional`, independent of another consumer's commit point. The broker's average may be rounded.
- `read_order_executions(id)` returns official activity dictionaries with stable `id`, `order_id`, `qty`, `price`, `side`, `symbol`, and `transaction_time`. Consumers requiring execution-exact cash accounting persist their own activity-ID cursor atomically with their ledger.
- `ledger_summary()` reports deduplicated **fill** cash flow and position changes. It is not the account's total cash: deposits, fees, dividends and other non-fill activities remain separate official account facts.

Activity IDs are durable unique keys. Repeated queries and stream messages do not book another execution. Cumulative observations never substitute the latest cumulative average for an incremental execution price. Stale observations cannot regress a filled quantity or reopen a terminal order. Order-history pagination overlaps the oldest timestamp group to avoid skipping timestamp ties; Alpaca's 500-order timestamp resolution cannot represent 500 or more orders at one identical timestamp, and recovery stops instead of claiming completeness in that case.

`stream.trade_updates(adapter)` runs one authenticated Paper WebSocket session, reconciles before/after subscribing, handles events through authoritative REST, and reconciles after disconnect. A supervisor can invoke a new session after a failure; reconnect never assumes missing events will be replayed. REST alone is sufficient for execution and recovery. Install `factorlab[alpaca-paper]` to use the optional WebSocket path.

## Small official round-trip acceptance

The acceptance entry point uses QQQ and AAPL, 0.003 shares each, and $3 conservative reservation per buy. This exceeds $1 per order at the observed test-session quotes; recheck the minimum notional if prices change materially. It records its baseline holdings and only sells its own newly bought quantity. It does not cancel unrelated orders or liquidate existing holdings. Avoid simultaneous account tests by other execution owners.

Run from an environment containing the credentials:

```powershell
$env:PYTHONPATH = 'D:\Code\FactorLab-worktrees\alp-e01\src'
python -m factorlab.integrations.alpaca.acceptance `
  --state "$env:LOCALAPPDATA\FactorLab\alpaca-e01\state.sqlite" `
  --evidence "$env:LOCALAPPDATA\FactorLab\alpaca-e01\evidence.json" --watch
```

Alternatively add `--credentials "$env:LOCALAPPDATA\FactorLab\alpaca-paper-credentials.json"` for a private, user-provisioned file. No extra per-order approval is required. `--watch` advances on official trade updates without REST polling. Without it, each invocation performs one reconciliation/progression pass with the same durable identities.

`waiting_market_session` records the next official open. `waiting_official_orders` or `waiting_official_reconciliation` means the venue has not yet supplied all order/activity evidence. After both round trips finish, `awaiting_process_restart` requires running the command in a new process with the same state file. Only that separate process can produce `complete`, after comparing positions, cash, and activity count. Repeated invocation after completion does not place new test orders. Exit code 2 indicates pending/mismatch, not acceptance success.

The public JSON excludes account ID, credentials and authentication headers. Preserve actual official order/activity IDs and execution prices for audit. Cash reconciliation exposes the exact difference between official cash change and fill cash flow, with a one-cent acceptance tolerance for account cash rounding; a larger difference is a mismatch, not an inferred fee. Multiple execution activities are reported as such, not misrepresented as an observed `partially_filled` status.

Official order rules: https://docs.alpaca.markets/docs/trading/orders
