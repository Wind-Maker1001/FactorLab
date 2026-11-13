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

## Separate Paper integration dependency

Inside the authoritative fill transaction, the future turnover admission hook
needs deployment identity, the proposed incremental quantity, the executable
fill price and whether the order is a risk liquidation. It reads the confirmed
gross turnover, clips an ordinary fill to the remaining notional allowance
at the executable price and rounds down to the legal quantity step. It blocks
a zero-quantity result before posting. Risk liquidation uses the existing
reduce-only admission and the legacy turnover exemption.

## Validation

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
