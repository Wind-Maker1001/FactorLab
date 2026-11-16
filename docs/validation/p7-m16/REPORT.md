# P7-M16 — Binance Demo Grid offline fill acceptance

**NO LIVE MONEY.** Baseline: `d9b608d01bd307b95e07030104a7620aa36b8d84`.

## Evidence boundary

All HTTP, including POST/DELETE, is intercepted by the existing `wired` transport fixture. It accepts only `https://demo-fapi.binance.com`; the ordinary connector is replaced with a failing stub. Credentials and authorization are synthetic and temporary. No real Demo write, Production request or durable operator authorization is part of this acceptance.

`REAL_DEMO_FILL_NOT_VERIFIED` remains applicable. #160, #161 and #167 establish real order acceptance, recovery, cancellation and observation, not natural BUY fills or a BUY→SELL economic cycle.

## Composed lifecycle

`tests/integrations/test_binance_demo_fill_closed_loop.py` uses the existing `network_grid` assembly: GridExecution → DemoGridBridge → DemoSession → TradingController → DemoExecutionGateway → DemoFuturesAdapter → intercepted HTTP, with existing protocol and PaperStore journals. No execution gateway, account database or trading state machine is added.

Injected rules: NVDAUSDT, one-way BOTH, USDT collateral, quantity step/minimum 0.01, price tick 0.01, minimum opening notional 5 USDT; one Grid unit is 0.04 NVDA. The fixed Grid BUY/SELL limits are 146/154, mark and book are 150. These are offline rules, not current exchange rule claims. Small execution increments are partial trades against a legal 0.04 order, not undersized opening orders.

The existing tests already exercise original-price round trips and partial restart. The new scenario connects them with per-event economic assertions and retained evidence:

| Confirmation | Increment | Cumulative side quantity | Increment fee (USDT) | Inventory | Gross realized PnL | Wallet |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| BUY ACK | 0 | 0 | 0 | 0 | 0 | 1000 |
| BUY trade 1 @146 | 0.01 | 0.01 | 0.00146 | 0.01 | 0 | 999.99854 |
| BUY trade 2 @146 | 0.02 | 0.03 | 0.00292 | 0.03 | 0 | 999.99562 |
| BUY trade 3 @146 | 0.01 | 0.04 | 0.00146 | 0.04 | 0 | 999.99416 |
| SELL trade 1 @154 | 0.02 | 0.02 | 0.00308 | 0.02 | 0.16 | 1000.15108 |
| SELL trade 2 @154 | 0.02 | 0.04 | 0.00308 | 0 | 0.32 | 1000.30800 |

No SELL is dispatched at BUY cumulative 0.01 or 0.03: those quantities remain below the original Grid reversal's minimum notional. The eventual SELL is 0.04, reduceOnly=true, against confirmed 0.04 inventory and at the original 154 limit. There are exactly two POSTs. The test restarts with SELL partially filled, then restarts again with both orders fully filled, preserving the same clientOrderIds/exchange IDs and exact economic event history. Repeated GET recovery at each BUY fill and after completion cannot change balances, turnover or event history.

### Fees, FIFO and conservation

BUY commissions total 0.00584; SELL commissions total 0.00616; total exchange-reported commission is 0.012. The test deliberately sets the local fee estimate to 0.7%, while the injected exchange reports 0.1%; actual charges must follow exchange evidence, not the local estimate. Local estimates affect reservation only.

FIFO attribution of the confirmed increments is `(BUY1, SELL1, .01)`, `(BUY2, SELL1, .01)`, `(BUY2, SELL2, .01)`, `(BUY3, SELL2, .01)`. Each pair allocates 0.00146 BUY fee and 0.00154 SELL fee, earns 0.08 gross and 0.077 net. After the first SELL, BUY2 retains 0.01 and BUY3 retains 0.01; after the second, no lot remains. This is a test-side attribution of authoritative journal deltas, not a second persisted ledger or a claim that the Futures position model stores FIFO batches. The existing Futures position model uses average entry; all entry fills here have the same price.

At every checkpoint:

- wallet = 1000 + gross realized PnL − confirmed commissions;
- unrealized PnL = inventory × (150 − 146), consistent with injected account and position-risk evidence;
- NAV = wallet + unrealized PnL;
- local reservation = inventory × 146 + unfilled BUY quantity × 150 × 1.007;
- available = wallet − reservation.

Final turnover is 12 USDT, wallet/NAV is 1000.308, inventory/unrealized PnL/reserved margin/open orders are zero. Gross buy/sell price difference is 0.32; limit slippage is zero because all injected trades execute at their original limits. No funding settlement is injected or booked; no income endpoint is consulted. Missing funding evidence is not replaced with an estimated cash flow.

## Exception injection

`tests/integrations/test_binance_demo_fill_faults.py` composes the same runtime/session/bridge and injects these distinct faults:

| Fault | Required recovery behavior |
| --- | --- |
| Missing account trade/fee history | Keep fee unknown, block dispatch and leave confirmed strategy inventory unchanged until authoritative history returns |
| BNB commission | Reject unsupported USDT projection rather than invent conversion or a zero fee |
| Trade notional conflicts with order cumulative quote | Block reconciliation; corrected history books the original fill exactly once |
| Order history side conflicts with submitted identity | Reject identity mismatch; restore the original identity before recovery |
| Duplicate ORDER_TRADE_UPDATE before and after reopen | Parse through UserDataEvents; no duplicate fee, quantity, turnover or journal event |
| Cumulative quantity regresses from 0.02 to zero | Preserve confirmed 0.02 inventory and its fee; block dispatch until consistent evidence returns |
| Accepted POST ACK lost, followed by order GET timeout | UNKNOWN remains non-dispatchable; recovered fill uses the original clientOrderId; exactly one POST |
| 0.02 fill races with DELETE | CANCELED retains the confirmed partial fill and fee; no fictitious flat account after restart |
| Grid confirmation metadata transaction interrupted | Roll back the Grid command; replay the independently committed economic confirmation on reopen |
| Accepted order committed in protocol journal, controller snapshot transaction interrupted | Recover the existing exchange ID into the controller, without resubmission |
| Fill committed in protocol journal, controller fill transaction interrupted | Restore and book the missing quantity/fee once, without another POST |
| reduce-only SELL 0.05 against confirmed 0.04 | Shared controller rejects INVALID_REDUCE_ONLY before POST |

Transaction failures are injected as exceptions after the relevant write but before SQLite transaction commit, then the session is closed and reopened against the same databases. They exercise journal rollback/replay boundaries, not a separate simulated account engine.

## Reproduction and result

Use Python 3.13 or 3.14 with the project test dependencies. From the checkout root:

```bash
FACTORLAB_M16_EVIDENCE=docs/validation/p7-m16/lifecycle.json python -m pytest -q tests/integrations/test_binance_demo_fill_closed_loop.py tests/integrations/test_binance_demo_fill_faults.py
python -m pytest -q tests/integrations/test_binance_demo_grid_network.py tests/integrations/test_binance_demo_execution.py tests/integrations/test_binance_demo_grid_bridge.py
```

**M16_OFFLINE_CLOSED_LOOP_VERIFIED** — **OFFLINE_FILL_CLOSED_LOOP_VERIFIED**.

Python 3.14 targeted results: lifecycle **1 passed in 7.44s**; exception injection **12 passed in 24.91s**; existing Grid network/bridge and confirmed-execution regressions **67 passed in 50.76s**. No production-code repair was necessary; no M14 adapter file was modified.

`lifecycle.json` retains raw injected execution messages, exchange order/trade confirmations at checkpoints, authoritative ordered journal events before/after restart, incremental quantities/notionals/fees, FIFO allocations, balances, positions, NAV and sanitized write parameters. The lifecycle, fault and regression XML result files retain individual test outcomes; `transaction-tests.xml` additionally retains the focused three-case transaction/duplicate-message run. Reproduction generates new temporary campaign identities and observation timestamps; economic assertions and original-price requirements remain fixed.

`REAL_DEMO_FILL_NOT_VERIFIED` remains unchanged.

## Real Demo evidence still needed

A future bounded real run would need account-specific authorization for symbol, quantity, original price, duration, POST/DELETE budget and residual-inventory cleanup risk before any write. Outstanding evidence is an actual BUY trade and authoritative fee, actual reduce-only SELL partial/full trades and fees, independent trade/order/account recovery across restart, and authoritative funding settlements if any. No such run is performed here.
