# Grid external execution seam

## Status

P7_G01_GRID_SEAM_ACCEPTED

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

## Authority and recovery

For a dedicated single-instrument Paper account, Grid observation, submission
and confirmation use the existing TradingController transaction. The quote,
order journal, account and Grid checkpoint commit together. Grid publishes its
staged decisions after commit. The transaction reloads authority before changing
it and checks that the caller holds the current checkpoint revision. A Grid
proposal canceled before submission can be discarded locally. Once submitted,
its reservation stays with the authority until a terminal report.

The checkpoint binds account, instrument, market, deployment, strategy and
controller configuration. It binds the journal position and the confirmed orders,
balances, positions and marks into a digest. It detects a damaged checkpoint or
an independently advanced account before accepting commands. Restore reads
working order status and cumulative fills from authority. Paper restart overlays
UNKNOWN on unfinished orders while preserving their confirmed quantities and
reservations. Local reconciliation can resolve a stored confirmation. A
submission timeout with no stored confirmation requires an explicit authority
report before the account can reconcile.

Grid uses the shared OrderEvent state machine for valid confirmations. Before
applying a new report, the seam checks the authoritative terminal record.
Conflicting status, cumulative fills, fees or exchange identity commit a
persistent Grid block while leaving the last booked authority intact. The
block covers existing unsubmitted proposals and survives restart and local
Paper reconciliation. A conflicting execution needs authoritative resolution
before the deployment can resume. A late fill during a cancellation request
still updates the account and confirmed quantity, with no counter-order.

## Checkpoint size and ordering

PaperStore holds one Grid checkpoint for the dedicated account. It contains the
current grid, risk state, working order metadata, pending bar or rebuild, a
rolling ATR window and counters. Accepted order data come from PaperStore.
The issued-order counter keeps client order IDs unique after terminal orders
are dropped from Grid memory. The checkpoint and staged Grid state grow with
the working set and configured window. There are at most 1,000 working orders,
1,000 grid prices and 4,096 ATR samples. The existing PaperStore caps govern
the authority journal and order lifetime.

Deliveries are consecutive across observations, submissions, confirmations and
ATR close commands within a deployment. Retrying the latest delivery with the
same normalized payload is idempotent. An earlier delivery, a gap or a changed
payload at the current delivery is rejected. Observation IDs are source labels.
Paper OrderEvent IDs are account-wide and a matching journal entry makes an
exact retransmission idempotent even at a new delivery. The caller orders the
feeds by delivery. Observation timestamps remain timezone-free and monotonic.
OrderEvent timestamps remain UTC and monotonic per order.

## Account projection and turnover

The dedicated quote-currency balance supplies available funds and reservations.
Equity positions add marked inventory to cash and include entry notional in
committed capital. USD-M keeps entry notional as collateral, with unrealized
mark-minus-entry PnL added to wallet equity. UNKNOWN preserves reservations
and blocks new decisions until reconciliation.

Gross turnover sums the incremental confirmed quantity at each journal fill
price. Projection reads the snapshot and journal together. Grid begins counting
at the deployment's journal baseline, adds each new confirmed fill delta in
the same transaction, and checks the resulting scalar against the authoritative
journal on restore. The account lifecycle fixture checks both USD-M and equity
with fees. Two partial opening fills at 90 and 98, a reduction at 110, and a
close at 100 produce gross turnover of 398. Cancellation releases the remaining
opening reservation. PaperStore is the sole source of economic records.

Grid's submission estimates continue to use the effective FeeSchedule,
including minimum fees and cash-equivalent slippage per executable quantity
step. The fee bound assumes the executor respects that quantity step. Paper
fees and cash postings follow the Paper authority's configured fee model.

## Decision comparison

The tests compare GridStrategy and the seam at matching observation and
confirmation boundaries. They cover initial arithmetic/geometric levels and a
ten-observation crossing sequence, then compare the active orders and decision
state through a touched user cancellation, a touched expiry, and a later ATR
rebuild with a fill at the same observation. ATR closes explicitly after the
execution feedback for that observation. A breakout rebuild uses the prior
ATR spacing even when its cancellation confirmations arrive after the close.

The breakout exit fixture restarts with cancellations pending, reconciles the
working orders and compares the market exit quantity, flat position and
EXITED state. Post-fill drawdown and turnover fixtures compare the settled
PAUSED decision. Legacy Grid can briefly propose and cancel a counter-order
within the risk-stop observation. The seam uses the confirmed account risk
state to suppress that proposal.

## Paper integration

P7_G02_GRID_PAPER_INTEGRATED

The optional Paper fill admission function receives the freshly reloaded broker,
order snapshot, proposed incremental Decimal quantity and executable price.
It returns an admitted quantity, with zero meaning no simulated execution. The
function runs before event allocation and economic posting. Without it the
original Paper execution contract is unchanged. Confirmed report replay bypasses
new-execution admission and retains the existing event-identity semantics.

Grid binds this function to its dedicated account. The saved deployment and
checkpoint identify the strategy, account, instrument, order, trading state and
reconciliation state. Every candidate reconstructs confirmed gross turnover from
the transaction's authoritative event log after the deployment baseline and
checks it against the checkpoint. Remaining capacity is initial equity times
max_turnover minus confirmed turnover. Ordinary fills are capped by this capacity
at the executable price and by the remaining order quantity, rounded down to the
quantity step, then checked against minimum quantity and notional. Market SELL
orders in EXITING state are exempt only when reduce-only and within the actual
long position; these exits still add to reported gross turnover.

Grid fill and cancellation commands reuse the controller's transaction and
cancellation lifecycle. Broker-generated confirmations update Grid decision state
and its checkpoint before the same commit as cash, position, fees and the order
journal. A repeated committed delivery does not simulate another execution and
returns no new event. Cancellation-pending orders retain their reservation and
may receive admitted fills before terminal cancellation. A failed commit publishes
neither the staged Grid state nor the updated broker state.

The shared quote-participation candidate calculation supplies side-dependent
adverse slippage and limit crossing for both realtime Paper and the Grid research
entry point. It is not a second Grid matcher. A rejected economic posting remains
an atomic rejection; clipping for strategy turnover does not guarantee funds at
an arbitrary execution price. Paper collateral checks remain authoritative.

## G01 validation

`PYTHONPATH=src python -m pytest tests/research/test_grid.py tests/research/test_grid_execution.py tests/research/test_grid_experiment.py -q`

**72 passed in 23.53s.** The transaction fixture denies SQLite COMMIT after the
event, account and checkpoint have been written, verifies rollback, then
retries and restarts with a partial fill and an unresolved submission. The
recovery tests also cover stale writers, authority ahead of the checkpoint,
checkpoint damage and configuration drift. The boundedness fixture runs
480 deliveries and checks that the checkpoint grows by at most 32 bytes after
warmup, with two retained ATR samples and zero Grid command records in the
Paper journal. The terminal-conflict fixture checks the booked account before
and after the late report and after restart.

## Reproducible Paper research

The entry point consumes completed daily bars. Date labels are timezone-free;
Paper quotes and executions are timestamped at the end of that UTC day. Orders
may execute only on a later observation than submission. Each bar supplies one
shared synthetic quote-participation budget. The close is both bid and ask;
OHLC is used for ATR, not to infer an intrabar path or a touched-limit fill.
Unfilled final orders retain their real reservations; no end-of-window
liquidation or cancellation is invented to improve NAV.

```sh
PYTHONPATH=src python -m examples.research.grid_paper_demo --output <fresh-output>
```

For other daily data, pass `--bars <csv> --config <GridConfig.json>
--rules <GridRules.json> --capital <amount> --start <date> --end <date>`.
CSV columns are `id,time,price,volume,completed_bar,high,low,close`; dates are
normalized daily labels and volume is an integer Grid observation quantity.
`--market` selects an existing Paper accounting contract for one dedicated
instrument, identified by `--instrument-id`. There is no random execution state.
The Python `run_experiment` entry also accepts a separate budget `fee_estimate`.

Outputs are `daily.csv`, `trades.csv`, `result.json`, `evaluation.json`,
`manifest.json` and the authoritative `paper.sqlite`; the four-arm comparison
adds `comparison.csv`. The manifest records input identity/window and checkout
provenance without copying historical bars into JSON. Trade receipts retain
incremental and cumulative quantities/fees, event identity and UTC time, direction,
price and grid level. Order receipts retain deployment, target and reduce-only
identity. ResearchEvaluation consumes daily NAV, incremental turnover and the
execution receipts. A clipped candidate contributes only its actually confirmed
quantity to fees, volume usage, turnover and evaluation.

### Cost contract and decision differences

The actual Paper contract is explicitly proportional `fee_rate` per incremental
fill. It does not implement FeeSchedule minimum commission, stamp duty or a
historical exchange fee tier. An optional FeeSchedule is only the conservative
Grid submission budget, including its minimum-fee assumptions; its rules and the
estimate-minus-actual difference are reported separately. Passing a budget
schedule does not change broker cash postings. Supporting those schedules as
actual costs requires a future compatible Paper fee contract, not substituting
estimates for economic results.

Adverse `--slippage` is included in execution price; the reported slippage
attribution is not debited again. Limits still bind after slippage. Thus a quote
exactly at a BUY limit can remain unfilled, and favorable gap improvement differs
from legacy Grid's limit-price matching. Shared participation and legal Decimal
steps govern partial fills. The broker retains its funding/collateral rejection;
expected economic rejection is recorded without retaining the failed event or
partially publishing Grid state. Unified submission refusals are reported, with
unsubmitted proposals remaining unconfirmed rather than consuming reservations.

The fixed five-observation integration case 100,90,100,110,100 uses one-unit
orders, two endowed units, initial equity 1,000, 10 bps actual commission and no
slippage. Its confirmed sequence is BUY 90 (level 1), SELL 100 (level 2), SELL 110
(level 3), BUY 100 (level 2). Final cash is 819.6, inventory 2, equity 1,019.6,
fees 0.4 and gross turnover 400. This is an engineering fixture, not a selected
historical return.

### Existing NVDAUSDT window

```sh
PYTHONPATH=src python -m examples.research.grid_paper_demo \
  --output <fresh-nvda-output> \
  --snapshot docs/validation/p6-b01/nvda/runs/real \
  --config configs/grid_paper_nvda.json --rules configs/grid_paper_nvda_rules.json \
  --market BINANCE_USDM --instrument-id NVDAUSDT \
  --fee-rate .0004 --slippage .0006 --participation .01
```

The existing complete snapshot contains ten daily bars, 2026-10-31 through
2026-11-09, data version
`59f806b0bb04df2e4f12de805779cdab1e029d71629c446b2c43111c729cb49c`.
The adapter preserves daily close/high/low and floors observed aggregate volume
to the Grid integer observation contract. It neither contacts Binance nor adds
order-book observations. Configuration fixes the range at ±20% of the first
reference close, eight levels and 0.1-unit orders; the four arms share starting
capital, endowment and execution assumptions. No parameter search is performed.

The run uses the existing USD-M fully collateralized Paper wallet and position
projection. The 4 bps fee, 6 bps adverse slippage and 1% daily participation are
conditional research inputs, not verified historical execution costs/depth.
Funding is not simulated. The snapshot's recorded filters provide quantity/tick
and minimum-notional settings, not a claim of complete historical rules coverage.

| Arm | Final equity | Final wallet cash | Position | Fills | Fees | Gross turnover | Max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No trade | 1000 | 1000 | 0 | 0 | 0 | 0 | 0% |
| Arithmetic | 999.82978731104 | 999.98550971104 | 0.2 | 2 | 0.01449028896 | 36.2257224 | 0.169356% |
| Geometric | 999.85479099296 | 999.97830859296 | 0.3 | 3 | 0.02169140704 | 54.2285176 | 0.171153% |
| ATR | 999.65957462208 | 999.97101942208 | 0.4 | 4 | 0.02898057792 | 72.4514448 | 0.338226% |

For example, the arithmetic arm confirms two distinct 0.1 BUY fills on November 5,
levels 2 and 3, each at 181.128612. Each has notional 18.1128612 and actual fee
0.00724514448. Final equity includes marked unrealized loss; wallet cash alone
is not NAV. Full-window research and historical cost/funding/depth validation
remain P7-R02 work. The existing PaperStore lifetime limits still bound an
account to 1,000 orders and 10,000 events; this integration does not add archival
rotation or unbounded execution history.

## G02 validation

```sh
python -m pytest tests/research/test_grid.py tests/research/test_grid_execution.py \
  tests/research/test_grid_experiment.py tests/research/test_grid_paper.py \
  tests/research/test_grid_paper_experiment.py tests/trading/test_fill_admission.py \
  tests/trading/test_paper_control.py tests/trading/test_realtime.py -q
```

The combined targeted run passed 168 tests. After adding final UTC/cumulative
receipt fields, an explicit USD-M endowment case and an oversized partial-fill
candidate, the affected new integration suite passed 24 tests:

```sh
python -m pytest tests/research/test_grid_paper.py \
  tests/research/test_grid_paper_experiment.py tests/trading/test_fill_admission.py -q
```

The COMMIT fault is injected after journal, account and checkpoint writes. The
failed transaction preserves both in-memory authorities and the saved snapshot,
allocates no durable fill identity, and leaves fees/turnover/inventory unchanged.
Retry books one partial increment; restart overlays UNKNOWN, explicit
reconciliation resumes the order, and the second increment completes it without
repeating the first fee or notional.

Cancellation-pending fills, zero admission, step/minimum constraints, remaining
order quantity, exhausted-turnover risk exits and terminal conflicts have direct
integration cases. The 64-observation ATR run rebuilds repeatedly, restarts at the
midpoint and reconciles before resuming. It checks a two-sample ATR window,
checkpoints below 12,000 serialized characters, and cash/inventory/NAV/turnover
against authoritative incremental fills. Existing G01 boundedness and recovery
counterexamples remain in the regression suite. The non-Grid tests cover default
execution, latest-state policy inputs, replay idempotency and posting rollback.
