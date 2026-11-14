# Binance Demo serial Grid bridge

`DemoGridBridge` composes confirmation-driven `GridExecution` with
`DemoExecutionGateway` and its existing `TradingController`. It currently accepts
only `DemoExecutionTransport` (offline injection); it does not authorize network
orders. No CLI command or market-data driver is changed.

Construct a fresh Grid with the bound account identity and observed exchange
rules, construct the bridge, then call `recover()` before `observe(event)` or
`dispatch()`. Recovery queries the exchange through the existing gateway before
consuming missing confirmations. `dispatch()` submits at most one outstanding
proposal at its original limit price. Pending proposals remain Grid reservations;
they are not exchange orders. A known active exchange order occupies the slot
until an authoritative terminal report. Cancellation of an undispatched proposal
is recorded as local admission withdrawal, not an exchange cancellation.

The controller event journal stores deployment binding and replayable strategy
inputs. Existing protocol/controller ledgers remain authoritative for orders,
execution prices, fees and positions. A crash between submission and strategy
feedback is recovered from those journals without submitting again. A timeout
blocks further dispatch until GET recovery. Exchange EXPIRED and EXPIRED_IN_MATCH
use the adapter's existing CANCELED mapping. Contradictory or out-of-order reports
retain the underlying state machine's reconciliation requirement.

## Budget feasibility

Limits remain 0.04 NVDA, 7 USDT conservative committed notional, and one active
exchange order. Grid configuration may impose tighter limits. Leverage does not
multiply the Grid budget. BUY exposure includes existing inventory and opening
reservations. SELL requires confirmed long inventory and reduceOnly=true.

For limit price p, quantity step s, exchange minimum quantity m and minimum
notional N, the smallest permissible order is

`q_min(p) = ceil(max(m, N / p) / s) * s`.

A serial round trip needs a single step-aligned quantity q satisfying both leg
minimums, q <= 0.04, and q * max(limit, risk reference) <= 7 on each leg. Opening
fees and existing reservations also consume Grid available/committed capacity.
Rules must come from the bound Demo rule observation; constructor rejects
stepSize, tickSize, minimum quantity or minimum notional mismatches.

The offline fixture uses N=5, s=0.01, m=0.01. These are explicitly synthetic rules,
not a statement of current Binance filters. At prices 145/155 with risk reference
150, q_min is 0.04 on both legs: 5.8/6.2 USDT. A 0.04 order fits. At price 100,
q_min is 0.05 and the same budget cannot execute even one opening grid order.
The prior recorded Demo buy at 162.94 for 0.04 had notional 6.5176 USDT
([execution evidence](validation/binance-demo-persistent-execution.md)); that
historical execution is not an admission decision for a new grid configuration.

Two simultaneously executable minimum-size BUY layers at fixture prices 140 and
145 need at least 0.08 NVDA, 11.4 USDT limit notional, and two active-order slots.
At reference 150 their conservative reservation is 12 USDT plus fee reserves.
This is a minimum proposal for that example, not an applied budget change. For
arbitrary layers sum their step-rounded quantities and conservative reservations;
actual limits require the current exchange filters and risk reference. Multiple
simultaneous layers are not supported under the current one-order authorization.

Partial fills are posted exactly once. A 0.02 fill at 145 is inventory, but its
2.9 USDT notional is below the fixture minimum and cannot produce a reverse order.
GridExecution accumulates unproposed reverse quantities per side and adjacent
level. Later confirmations at the same level retry the combined quantity; a
0.02 + 0.02 fragmented fill therefore produces the same 0.04 reverse proposal as
one complete fill. Remainders are checkpointed, and discarded when risk pauses or
a range rebuild invalidates their targets. Inventory remaining below minimum
after cancellation still requires an explicit operator policy, not an illegal
order, synthetic fill, top-up or automatic budget increase.

The generic fix changes the Paper Grid checkpoint version from 3 to 4; existing
version-3 checkpoints are rejected rather than silently losing remainder state.

## Remaining real-Demo interfaces

- Network composition needs a separately authorized, persistent runtime entry
  accepting the exact Grid limit price and identity. `DemoSession.submit_limit`
  intentionally chooses the current inside-market price and is not that entry.
- Its persistent dispatch guard, campaign prefix, total POST budget, expiry,
  loss cap, fresh account/quote checks and operator lock must all remain enforced.
  The offline bridge deliberately refuses a network transport rather than bypass
  those controls.
- A market-data caller must supply ordered Grid observations and invoke recovery
  after disconnect; no new driver is included here.
- Below-minimum residual inventory after terminal cancellation requires an
  explicit operator policy. LIMIT-only runtime also needs an explicit operator
  policy for Grid MARKET risk exits, which this bridge refuses.

Offline tests exercise original-price submission, confirmed buy/sell inventory,
partial fills and cancellation, expiry, rejection, duplicate confirmation, lost
response recovery, restart with fills while disconnected, and rule/budget bounds.
