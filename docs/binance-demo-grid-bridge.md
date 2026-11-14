# Binance Demo serial Grid bridge

`DemoGridBridge` composes confirmation-driven `GridExecution` with
`DemoExecutionGateway` and its existing `TradingController`. Offline injection
continues to use `DemoExecutionTransport`. Network delivery uses a bound
`DemoSession` inside `open_grid_session`; the bridge itself grants no authority.
No CLI command or market-data driver is changed.

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

The recorded Demo [rule observation](validation/p7-d03a/demo-readonly-preflight.json)
at 2026-11-12T08:04:01.772159Z has N=5 USDT, s=m=0.01 NVDA,
tick=0.01 USDT, price range 0.01–20000 and mark multipliers 0.97–1.03.
Public Demo GETs attempted on 2026-11-14 failed to connect; these recorded rules
are dated evidence, not a fresh trading authorization. Every network session
observes exchange rules again and rejects stale evidence at submission.

With those recorded filters and an illustrative fresh book/mark of 150, limits
146/154 require q_min=0.04 on each leg. The price band is 145.50–154.50;
both limits are tick-aligned and inside it. BUY notional is 5.84, SELL notional
6.16; conservative order reservations are 6.00 and 6.16 USDT, leaving room under
7 for the configured fee reserve. A one-interval arithmetic or geometric grid
can therefore fit the unchanged limits in the offline network fixture. This is
conditional on the observed account and rules also passing admission.

At price 100, q_min=0.05 exceeds the inventory cap, so that layer is illegal.
For q=0.04, the opening minimum requires limit >=125 and the notional budget
requires max(limit, book, mark)<=175 before fee reserves or other commitments.
Every layer and its adjacent reverse leg must separately satisfy these bounds
and the live percent-price band. In particular, 145/155 at mark 150 meets the
quantity/notional bounds but violates the recorded 3% price band; it is not a
legal network pair. No limit is automatically increased to make a layer fit.

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

## Authorized network composition

`open_grid_session(root, credential_store, grid_factory)` owns the Windows operator
lock and opens the existing persistent Demo campaign and account journals.
`grid_factory(session, deployment_id)` constructs a fresh Grid using the observed
rules and account identity. The context yields a bridge; call `recover()` before
observing events or dispatching. It does not enable a campaign or submit on entry.
The supplied deployment identity is derived from the persisted campaign prefix;
the deployment binding and command journal must match on reopen. Changing a
campaign or deployment requires explicit operator disposition, not silently
adopting another deployment's inventory.

Network delivery calls `DemoSession.submit_intent` with the original LIMIT intent.
Fresh book and mark observations inform risk only: they never replace the Grid
limit price. The existing `submit_limit` inside-market API remains available to
its existing callers. Persistent dispatch admission binds the Grid's exact price,
identity, side, quantity and reduce-only flag before network I/O. POST consumption,
expiry, loss limits, account binding, fresh evidence and Demo-only authorization
remain enforced. Confirmed known cancellations use `DemoSession.cancel`, without
running new-order notional admission when market movements exceed the budget.

A caller must supply ordered Grid observations and recover after disconnect; no
new market-data driver is included. A lost write response stops the campaign via
the existing receipt path. GET recovery establishes order/account facts but does
not restore stopped authorization. An unresolved order continues occupying the
single slot. Reauthorization is an explicit existing operator action.

Below-minimum residual inventory remains in the confirmed account ledger. The
LIMIT-only bridge rejects MARKET risk exits rather than converting them into
market orders. Pause and inspect the reported inventory; use the existing
manual authorized LIMIT/cancellation path only when exchange rules and budgets
permit. No synthetic fill, automatic top-up or limit increase is performed.

The HTTP Stub integration tests intercept actual `DemoNetworkTransport` requests
through the production Session, controller, gateway, adapter and persistent guard.
Offline tests also retain ATR pending-bar replay and fragmented reverse-quantity
accumulation across restart.

### Intercepted request evidence

`test_original_limit_and_confirmed_reduce_only_round_trip` inspects the actual
URL query received by the HTTP opener stub, with fresh bid/ask/mark all 150.
The signed-request fields asserted by that test are:

| Field | Opening POST `/fapi/v1/order` | Reversal POST `/fapi/v1/order` |
| --- | --- | --- |
| symbol | NVDAUSDT | NVDAUSDT |
| side | BUY | SELL |
| type / timeInForce | LIMIT / GTC | LIMIT / GTC |
| quantity | 0.04 | 0.04 |
| price | **146** | **154** |
| positionSide | BOTH | BOTH |
| reduceOnly | false | true |

The second request is admitted only after authenticated GET reconciliation of
the first fill. The 150 risk reference does not appear as either order's price.
Client IDs derive from the persisted campaign prefix and Grid sequence; the
restart test asserts the same identity and no extra POST. Timestamp, signature
and credential headers are intentionally absent from this evidence.

Run the focused network suite with:

```sh
PYTHONPATH=src python -m pytest tests/integrations/test_binance_demo_grid_network.py -q
```
