# Grid external execution seam

## Status

P7_G01_GRID_SEAM_PARTIAL

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

## Revision behavior

Observe, confirm, UNKNOWN and batch replay stage state before publication. Invalid
commands cannot partially publish journal entries, orders or decision state.
Observation time is explicitly timezone-free, matching legacy Grid; confirmations
use the unified UTC OrderEvent contract. IDs, prices, volume, completed-bar/OHLC,
account identity and cumulative turnover are validated. Confirmation transitions
reuse trading.state.advance, including per-order confirmation ordering.

Fee estimates reuse the effective FeeSchedule. Reservation estimates assume one
minimum-fee execution per quantity step, with commission, cash-equivalent slippage
and side costs charged each time. The external executor must guarantee this
minimum execution quantum for the bound to hold. Two one-unit fills at 90 with
minimum commission 1 and slippage 10 bps cost 2.18, not the single-fill 1.18.
These are strategy estimates, never cash postings. PaperBroker's proportional
fees are not represented as equivalent to this schedule.

Cancellation requests retain remaining quantity until a terminal confirmation.
Late partial or full fills while cancellation is pending update confirmed quantity
without creating reversal orders. Rebuild waits until all prior orders terminate.
UNKNOWN retains orders and blocks observations until explicit confirmation.

project_paper_account is a read-only dedicated-instrument projection. USD-M
wallet cash is not reduced by entry principal: entry notional is collateral,
equity includes mark-minus-entry PnL. A tested partial opening leaves wallet 1000,
available 800, committed 200 and equity 1000. Gross turnover must be supplied from
the authority's journal because snapshots do not retain historical fill prices.
No second economic ledger is created.

## Persistence protocol

checkpoint writes only deployment metadata and decision commands into the existing
PaperStore runtime, inside an existing account transaction. Confirmed strategy
orders must match saved authority. Callers save authoritative state first, then
checkpoint; no network orders are replayed. Restore verifies configuration and
identity, replays decision commands atomically, and checks authority before use.
The tests cover unsubmitted proposals, deterministic IDs, deployment drift and
transaction requirements.

## Decision comparison evidence

Arithmetic/geometric initialization compares target prices separately from legal
ticks. ATR warmup/rebuild compares legacy ranges. A ten-observation fixture
100,90,90,100,110,110,100,80,80,90 feeds the legacy simulator's confirmed one-unit
fills to the seam and compares all accepted proposal directions, target prices,
quantities, levels, grid ranges and strategy states after every observation.
This shares execution assumptions deliberately; it does not compare different
matching engines' economic outcomes.

## Remaining SEAM acceptance gaps

- Persistence tests do not yet cover authority advancing without a corresponding
  checkpoint, corrupted checkpoints, rollback after checkpoint failure, or a full
  controller restart with filled and UNKNOWN orders. An atomic wrapper coordinating
  in-memory publication with account transaction completion is still required.
- Replay copies accumulated state and history for staging; this is not a bounded
  incremental persistence protocol. Long-running deployments need a bounded
  checkpoint/command representation rather than increasingly large JSON payloads.
- Dedicated-instrument projection is tested only on a USD-M partial opening.
  Historical turnover remains caller-provided, not validated against the authority
  journal. Multi-instrument accounts are explicitly rejected.
- Explicit external account identity remains optional for legacy candidate callers.
  Cross-stream observation/confirmation ordering is not fully specified/validated.
- Full sequence equivalence still needs expiry, user cancellation, ATR subsequent
  rebuild, breakout pause/exit, post-fill drawdown/turnover and restart sequences.
- CANCELED followed by late FILLED (after cancellation has already been confirmed)
  needs coverage and reversal policy. Current tests cover fills after a cancellation
  request, not that later reconciliation case.

These gaps prevent SEAM_ACCEPTED. Legacy Grid and prohibited trading core files
remain unchanged.

## Separate Paper integration dependency

The unified controller must check remaining strategy turnover inside the fill
transaction, before posting a fill. Legacy clips an otherwise executable fill to
remaining turnover capacity; submission-only risk checks cannot guarantee this.
No Grid-specific matcher, fill interceptor or broker bypass is introduced. This
separate dependency blocks GRID_PAPER_INTEGRATED, not SEAM acceptance itself.

## Tests

```sh
PYTHONPATH=src python -m pytest tests/research/test_grid.py tests/research/test_grid_execution.py tests/research/test_grid_experiment.py -q
```

- Original baseline: 41 passed in 7.04s.
- Initial revision focused run: 10 passed in 0.82s.
- Expanded focused run: 16 passed, 9 failed; failure assertions compared deep-copied
  FeeSchedule object identity, fixed to preserve that immutable schedule reference.
- Three-file regression before adding crossing fixture: 56 passed in 7.66s.
- Final focused run including crossing fixture: 26 passed in 1.24s.
- Previous three-file regression: 57 passed in 8.07s.
- Risk-stop/reconciliation regressions: **63 passed in 7.77s**. Risk stops
  discard asynchronous rebuilds; late cancellations cannot resume a paused grid.
  Proposal and settlement paths share reconciliation/UNKNOWN admission, while
  valid fills remain recorded. Tests include normal resumption after reconciliation
  and reduce-only exit after risk cancellation. Status remains SEAM_PARTIAL.
