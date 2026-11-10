# Synthetic grid comparison

Run from the repository root with the project environment installed:

```sh
python -m examples.research.grid_synthetic_demo --output /path/to/new-grid-evidence
python -m examples.research.grid_synthetic_demo --replay /path/to/new-grid-evidence/manifest.json --output /path/to/new-replay-evidence
```

The experiment fixes four 96-session paths: oscillating range, continuous rise,
continuous decline, and downward gap. Each compares arithmetic, geometric,
and completed-bar ATR grids with whole-unit buy-and-hold. Every arm starts with
10,000 cash and 20 units marked at 100. Grid committed capital includes inventory
acquisition cost plus reserved buy cash; its 10,000 limit therefore leaves
capacity for grid purchases. ATR rebuild threshold is fixed ex ante at two ATR
grid spacings, not selected from scenario outcomes. Buy-and-hold invests its remaining cash
at the first observation, including entry costs, and does not liquidate at the
end. All arms use symmetric 3-bps commission and 5-bps cash slippage, without
minimum commission or exchange-specific taxes. Synthetic volume is ample;
these paths do not establish real-world liquidity or capacity.

`manifest.json` preserves the complete input events, resolved grid configs, and
fee assumptions. Replay uses these saved values rather than regenerating the
paths. Output directories must be new because experiment records are immutable.
For each path, snapshots, order/audit evidence, strict-JSON evaluation results,
and a comparison table are saved. `summary.csv` collects portfolio metrics, realized grid and total realized PnL,
unrealized PnL, fees, slippage, fills, turnover, maximum inventory and committed
capital, final OPEN (including partial), CANCELLED and REJECTED order counts, and
rebuild counts; `tracker/` stores ExperimentTracker records and artifact receipts.
Replay reproduces economic results; tracker timestamps and absolute artifact
paths naturally identify the new execution.

ResearchEvaluation normalizes equity to the common starting endowment, includes
first-observation entry costs in the first return, and compares each grid against
the same costed buy-and-hold NAV. Snapshot fees, slippage, and turnover are cumulative and are differenced into
daily costs and turnover. Turnover is gross traded notional divided by initial equity. Missing trade-ledger
metrics retain their explicit not-computable status rather than being inferred
from order counts. Comparison identities are shared only within one price path;
no ranking or cross-path performance comparison is implied.

## Order and accounting semantics

Arithmetic levels divide the configured range into `levels` equal intervals;
geometric levels divide it into equal ratios. Levels below the observed price
receive buy orders and levels above it receive sell orders. A partial buy creates
an equal-quantity sell one level higher; a partial sell creates a buy one level
lower. These replacement orders cannot execute on their creation event. Submitted
orders transition through ACCEPTED/OPEN or REJECTED; OPEN orders can partially
fill, fill, cancel or expire. Terminal states cannot fill again. Cancellation is
idempotent. Input event identity deduplicates the complete event; conflicting
payloads with the same identity fail. A replayable explicit cancellation uses an
input event with `action="cancel"`, `order_id`, and zero `volume`.

Observed-price events consume a shared whole-unit volume budget in submission
order. A crossed limit fills at its limit without favorable gap improvement.
OHLC events simulate only the close; possible earlier extrema touches are
recorded UNKNOWN. They do not prove an exchange fill or queue priority. ATR is
the rolling arithmetic mean of true ranges, including previous-close gaps.
Completed bars enter this history after execution. ATR warmup creates no orders;
thereafter rebuilding centers on the last completed close. Rebuilding cancels
all old orders before reserving replacements, keeps the existing inventory and
cost basis, and respects the event-count minimum interval. A breakout before
that interval pauses rather than leaving a stale active range.

`PositionBook` supplies integer inventory and lot consumption with immediate
spot sellability; this adapter does not impose or change A-share T+1. Cash changes
only on fills through the existing `FeeSchedule`. Reservations are projections
of active orders, not a second account: cash covers outstanding buys and stock
covers outstanding sells. Buy reservations budget the one-unit fee for every
remaining unit, covering separately charged partial-fill minimum commissions.
Date-effective fee increases that cannot be funded cancel the order before a
fill. All fees and slippage are cash costs; execution prices are not shifted a
second time. Monetary PnL uses average acquisition cost including buy costs;
`realized_grid_pnl` excludes market risk exits, while `realized_pnl` includes them.
The reconciliation is equity minus starting equity equals total realized plus
unrealized PnL, not realized grid PnL alone.

`max_cash_committed` bounds inventory acquisition cost plus buy reservations;
`max_turnover` bounds cumulative gross notional divided by starting equity.
Risk liquidation is exempt from the turnover cap and persists until filled,
including across zero-volume events; ordinary orders expire by event count.
Breakouts can pause, liquidate at subsequent observed prices, or rebuild.
Drawdown pauses and cancels orders without silently liquidating inventory.
Prices/quantities below order minima and spreads consumed by estimated round-trip
costs are rejected. These spot mechanics neither implement real order-book
priority nor imply live trading support.

DataHub remains the explicit-source routing boundary for external research data.
This fixture uses no external adapter, inferred source priority, adjusted-price
substitution, or PIT claim. Synthetic completed bars provide high/low/close for
ATR updates after matching; extrema are not executable touches. The evidence is
SOFTWARE_ONLY and SYNTHETIC: NO LIVE MONEY, PIT_UNVERIFIED, HFQ_UNVERIFIED, and
FORMAL_ADMISSION_BLOCKED. It is a replayable mechanics experiment, not financial
validation or trading authorization.
