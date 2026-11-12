# Grid external execution seam

## Status

BLOCKED — implementation candidate; not P7_G01_GRID_SEAM_ACCEPTED or
P7_G01_GRID_PAPER_INTEGRATED.

The legacy simulator remains unchanged. `GridExecution` exposes Decimal order
intents and confirmed-order updates without owning cash, positions, costs or
matching. `GridAccount` must be supplied by the account authority: equity is
marked equity, available excludes broker reservations, committed includes entry
capital and pending opening orders, and turnover is confirmed gross notional.
Its quantity unit explicitly maps a legacy grid unit to a base/contract quantity.

## Authority and behavior

Legacy grid range construction is reused, including geometric and ATR ranges.
The initial target-price sequence is compared separately from executable tick
prices. Buys round down and sells round up; quantities round down to step size.
Confirmation processing reuses the unified order transition machine. Pending
cancellations retain their orders until confirmation; reconstruction waits for
all old orders to terminate. UNKNOWN blocks subsequent observations. An ordered
in-memory command journal can reconstruct the seam into a fresh deployment.
It is not an atomic persistent runtime and must not be treated as one.

Legacy matching uses observed close, an integer shared volume budget, no
same-event fills, limit-price fills without gap improvement, and per-partial-fill
FeeSchedule commission/minimum fee/slippage. Unified PaperBroker accepts explicit
Decimal fills, uses proportional fees, and does not automatically match or
expire orders. Comparing their economic totals without aligning these assumptions
is not a strategy equivalence test.

## Remaining acceptance gaps

- Legacy pre-fill turnover clipping cannot be enforced through existing controller
  submission checks. A minimal core extension would admit each proposed fill
  against a strategy-provided remaining turnover limit inside the fill transaction.
- Legacy minimum commissions and cash-equivalent slippage are not represented by
  the candidate's proportional fee erosion check. No equivalence claim is made
  for those schedules.
- Confirmation-time drawdown and turnover stopping is implemented; dedicated
  cross-engine post-fill risk equivalence tests are still required.
- Input ordering/validation and durable, atomic journal recovery need completion.
- Account projection from the broker is explicit caller input, not an implemented
  spot/futures valuation adapter. No futures wallet is treated as spot cash.
- Direct controller/PaperBroker tests, late-fill-during-cancel cases, rejection
  coverage, and full legacy decision sequence comparisons remain necessary.

Do not deploy this candidate as a complete paper trading strategy. The minimum
next step is to complete the confirmation seam and its account projection before
adding a core pre-fill hook in a separately authorized change. No exchange API,
network orders or second economic ledger are introduced.

## Targeted validation

Command:

```sh
PYTHONPATH=src python -m pytest tests/research/test_grid.py tests/research/test_grid_execution.py tests/research/test_grid_experiment.py -q
```

First run: 40 passed, 1 failed. The geometric comparison incorrectly compared
legal tick prices with legacy target prices; the assertion now compares targets.
Final run, including confirmation-time risk handling: **41 passed in 7.04s**.
Intermediate rerun after the target assertion fix: 41 passed in 7.68s.
