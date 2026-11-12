# P7-M02A offline Demo execution

P7_M02A_DEMO_OFFLINE_INTEGRATED

P7_M02A_ENGINEERING_ACCEPTED

All execution evidence is deterministic OfflineTransport injection, not official
Demo online execution or financial admission. No real network order is involved.

## Architecture

ClosedBar → existing DualMA/FrozenModelStrategy → StrategySignal → shared pure
target sizing → existing TradingController → DemoExecutionGateway →
DemoFuturesAdapter → injected protocol confirmation → shared account and PaperStore.

`strategy_runtime.py` owns only strategy composition/checkpoints. The existing
controller owns transactions and risk admission; the adapter owns exchange order
identity and query reconciliation. `target.py` extracts the existing calculation
from RealtimeAccount without changing its Paper execution semantics.

The Demo gateway books actual confirmed fees and realized P&L using the existing
order state machine and account-fill machinery. Account reads publish this shared
wallet and position projection immediately; cached REST evidence bounds available
funds, not wallet totals. Strategy target sizing consumes this same projection,
including the zero-delta path. Protocol/shared order identity, status, cumulative
quantity and fee disagreements prevent a reconciled projection and new submissions.
Execution recovery imports a fresh REST cash/position baseline after replaying
missing confirmed deltas. Synthetic fill/rejection entry points are blocked.
The shared recovery scan distinguishes strategy metadata from order events.

## Execution results

Full structured logs, including signal/model identity, target/client-order mapping,
confirmations, journal, account and protocol method/path sequence, are in
`offline-execution.log`. JUnit results are in `targeted.xml`.

| Strategy | Order | Quantity | Fill price | Fee USDT | Wallet USDT | Available USDT | Position | Reconciled |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| DualMA | sg3d4b0906841f6a0b0fa7e9abb76b96 | 0.99 | 100 | 0.09900 | 999.90100 | 900.90100 | 0.99 | true |
| Frozen Ridge | sg6c4657c67d55b810268ffdce338ac0 | 0.99 | 100 | 0.09900 | 999.90100 | 900.90100 | 0.99 | true |

Both orders reach FILLED, with no remaining open order. Ridge uses the existing
real bundle fit/load fixture; the strategy runs frozen inference and preserves the
bundle content identity in signal metadata. Test fixture training does not change
research models or algorithms.

## Recovery and safety evidence

The targeted suite covers partial and duplicate confirmations, stale terminal
confirmation handling, cancellation/rejection, accepted-order timeout UNKNOWN
followed by GET recovery without another POST, persisted partial-fill restart,
bar/signal deduplication, external wallet/position changes, actual fee different
from local estimated rate, account/quantity/notional/rate/order-count bounds,
quantitative locks and NOT_REPORTED, local kill, Production write isolation and
unauthorized Demo write denial. Kill performs no cancellation or liquidation.

Command:

```sh
PYTHONPATH=src python -m pytest tests/integrations/test_binance_demo_strategy.py tests/integrations/test_binance_demo_execution.py tests/integrations/test_binance_demo_network.py tests/trading/test_strategy_pipeline.py tests/trading/test_strategy_quantity.py -q -s --junitxml=docs/validation/p7-m02a/targeted.xml
```

Result: **158 passed in 65.41 seconds**, no skips or failures.

## P7-M02A-R account consistency regression

The two new reproductions failed before the fix: a confirmed 0.99 BUY was sized
as current=0 for a subsequent zero target, and a 0.099 USDT fee left the published
wallet at 1000 instead of 999.901 USDT.

Offline regression results:

- BUY 0.99 at 100, then zero target without refreshing REST: SELL 0.99 reduce-only,
  flat after confirmation, wallet 999.802 USDT; exactly two POSTs. A subsequent
  zero target records TARGET_ALREADY_MET without an extra order.
- Confirmed fee 0.099 USDT: wallet 999.901 and available 900.901 USDT before REST;
  duplicate confirmation and repeated REST recovery preserve those balances.
- BUY 1 at 100, SELL 1 at 90: realized P&L -10 and total fees 0.19 USDT,
  wallet 989.81 USDT before and after repeated reconciliation, with no position.
- Partial confirmations 0.2 + 0.3, duplicates, cancellation and restart retain
  current=0.5; the next order is target minus 0.5 and fills exactly to target.
- A 0.05 USDT plan loss ceiling rejects the next order after the confirmed
  0.099 fee while the cached REST wallet remains 1000 USDT.
- Disconnection, UNKNOWN and Kill Switch preserve a pending signal without POST;
  an unbooked protocol/shared identity disagreement also blocks zero-delta signal
  consumption. Existing Production/Demo authorization, canTrade, quantitative-lock,
  quantity, margin and order-count tests remain in the targeted suite.

Final revision result: **99 passed in 9.66 seconds**, no skips or failures.

The revision suite runs only the three Demo integration test files listed above,
using the existing Python 3.12.7 environment. The original logs and JUnit file
remain evidence for the earlier 158-test run, not this revision.

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

## Independent online approval

Supply actual Demo account identity, strategy/version or frozen model/version/bundle
identity, source and bar interval, order types, quantity and notional limits, total
and open-order limits, margin and loss ceilings, duration and UTC expiry, unique
client-ID prefix, and explicit cleanup/recovery policies. Review live Demo rules,
account eligibility, one-way mode, leverage/configuration, margin and existing
orders. Only a separately reviewed process-local DemoNetworkAuthorization permits
network writes; restart does not restore it. Production writes stay disabled.
