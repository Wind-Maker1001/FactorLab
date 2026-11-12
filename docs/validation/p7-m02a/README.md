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

The Demo gateway books actual confirmed fees using the existing order state
machine and account-fill machinery. It refreshes account cash and positions from
exchange-confirmed facts after execution recovery; local reservations remain a
conservative admission bound. Synthetic fill/rejection entry points are blocked.
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

## Independent online approval

Supply actual Demo account identity, strategy/version or frozen model/version/bundle
identity, source and bar interval, order types, quantity and notional limits, total
and open-order limits, margin and loss ceilings, duration and UTC expiry, unique
client-ID prefix, and explicit cleanup/recovery policies. Review live Demo rules,
account eligibility, one-way mode, leverage/configuration, margin and existing
orders. Only a separately reviewed process-local DemoNetworkAuthorization permits
network writes; restart does not restore it. Production writes stay disabled.
