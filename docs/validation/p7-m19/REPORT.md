# P7-M19 — Binance Demo actual fills and authoritative account reconciliation

NO LIVE MONEY. Evidence separates historical exchange executions, this acceptance run, and offline fault scenarios.

## Authoritative starting point

Original `demo_cli recover` at 2026-11-16 14:46:36 UTC completed with **38 GET, 0 POST, 0 DELETE**. The persisted account binding was accepted; canTrade=true, ONE_WAY. Wallet, available balance and margin balance were **4999.97114760 USDT**; unrealized P/L was zero. No position, open order or UNKNOWN request remained. Existing journals were recovered rather than initialized or replaced.

Historical actual executions retained by recovery:

| clientOrderId | orderId | tradeId | Side | Quantity | Execution price | Actual USDT fee | Status |
|---|---|---|---|---:|---:|---:|---|
| fl-6134cc5d117d4d20-01 | 191862895 | 39911928 | BUY | 0.04 | 162.94000 | 0.00162940 | FILLED |
| fl-75475821429342b2-1000911ffbc2 | 191865793 | 39913139 | SELL reduceOnly | 0.04 | 162.30000 | 0.00162300 | FILLED |

Existing CANCELED orders 191944057, 191960373 and 192147238 also survived recovery. Historical exchange write capability and actual FILLED results are established facts, not a missing capability.

Complete signed GET export at **2026-11-16 14:59:51 UTC** independently associated both userTrades rows with their order queries. BUY quantity sum **0.04**, quote sum **6.5176000**; SELL quantity sum **0.04**, quote sum **6.4920000**. Both exactly equal their order cumulative quantities and notionals. Fees use the returned commissionAsset=USDT and commission values; no estimated fee rate is booked.

The existing FIFO engine attributed one fully consumed BUY lot to the SELL: gross realized P/L **-0.02560000 USDT**, actual fees **0.00325240 USDT**, net **-0.02885240 USDT**. Against the recorded initial flat wallet of 5000 USDT, final wallet and equity are **4999.97114760 USDT**, NAV **0.99999422952**, residual inventory **0**, unrealized P/L **0**, reconciliation residual **0**, and reported-equity difference **0**. The signed income response contained no funding-fee event; none was invented or booked. This establishes the historical receipt/fee/position/account reconciliation, separately from a new M19 execution lifecycle.

## Receipt and accounting implementation

The existing Adapter remains the only exchange confirmation entry point. Its existing checks independently sum actual userTrades quantities and quote quantities against order cumulative execution, use confirmed commission, and reject non-USDT commission. Validated raw order-query and trade business payloads now persist atomically alongside confirmation in `futures_receipts`; identical payloads do not append duplicate evidence. No authentication fields are stored by this receipt path.

`demo_fill_acceptance` exports order/trade identities and existing controller economic events, revalidates persisted mappings, and uses the existing `grid_fifo` engine through its extracted `attribute_events` interface. FIFO output is attribution of journal executions, not another broker or booking path. Execution prices serve as explicit attribution references; this does not measure market slippage.

## Current experiment state

The initial runtime has no active Campaign. Actual new execution awaits the integrated continuous execution window. No M19 POST or DELETE has been sent.

An earlier GET-only capture stopped on Binance **HTTP 400 / -1021 CLOCK_SKEW** during historical order reconciliation. It sent no replacement order or synthetic fill. The later full fresh recovery above succeeded and completed the actual receipt/FIFO export; the failed observation remains part of the execution history.

| Acceptance state | Current evidence |
|---|---|
| DEMO_ORDER_ACK_VERIFIED | Historical successful writes retained; new M19 ACK not yet obtained |
| DEMO_TRADE_RECEIPT_VERIFIED | Verified historical actual trade IDs 39911928 / 39913139 and cumulative quantities; new experiment pending |
| DEMO_FEE_RECONCILED | Verified historical actual USDT fees against protocol, controller and FIFO totals |
| DEMO_POSITION_RECONCILED | Verified zero exchange/local/FIFO residual inventory; new experiment pending |
| DEMO_ACCOUNT_RECONCILED | Verified historical receipt/FIFO wallet/equity/NAV with zero residual; new experiment pending |
| DEMO_RESTART_RECOVERY_VERIFIED | Verified separate-process historical recovery: same account/intents/economic events/fees/wallet, unchanged dispatch count, GET only; pending-order/new-fill restart pending |
| DEMO_LIFECYCLE_VERIFIED | Historical FILLED/CANCELED retained; full required lifecycle not established by this run |

The machine-readable business evidence is [`historical-reconciliation.json`](historical-reconciliation.json): actual order/trade payloads, persisted historical POST/ACK records, journal-based FIFO and the separate-process recovery comparison. The recovery used 38 GET and no POST/DELETE. All eight recovery comparisons were true; no new dispatch or economic posting occurred.

## Frozen strategy and directed verification

The persisted deployment is `fl-d7345f8795464a65-g`, arithmetic one-level grid, BUY boundary **159.47**, SELL boundary **162.71**, initial reference **161.09**, quantity unit **0.04**, max cash commitment **7 USDT**, breakout policy `pause`. Its persisted rule snapshot has quantity step **0.01**, price tick **0.01000**, minimum quantity **0.01**, minimum notional **5 USDT**. These are the immutable experiment inputs, not a replacement for rechecking current exchange rules. The strategy will not be rebuilt or repriced merely to obtain a fill.

Final M19 receipt/FIFO/M14 selection: **22 passed in 24.64s**:

```text
PYTHONPATH=src python -m pytest -q tests/integrations/test_demo_receipts_m19.py tests/integrations/test_demo_reconciliation_m14.py tests/test_grid_fifo.py
```

A broader runtime/M16/FIFO run reached **40 passed, 1 failed**: the pre-existing fixed-MS market fixture aged against the session wall clock and raised `STALE_QUOTE` inside the M16 partial-fill test. The earlier 29-test runtime/M16/FIFO selection passed. The deterministic fixture clock correction is part of the integrated runtime verification; the production freshness guard is not relaxed.

The new tests cover actual receipt persistence/repeated GET/reopening, exact trade-ID/quantity/fee mapping, rejection of unknown fee assets, sanitized POST/ACK fields, partially filled FIFO exports without booking, ACK-only rejection as account closure, complete fee/NAV attribution, unbooked funding blocking account acceptance, and duplicate-dispatch detection across recovery captures. All such intercepted HTTP tests remain offline evidence.

## Remaining completion conditions

- Freeze a legal NVDAUSDT LIMIT GTC experiment under integrated runtime authorization, quantity <=0.04 and order notional <=7 USDT; record a real rules conflict if minimum constraints make it infeasible.
- Observe actual entry and confirmed-inventory reduceOnly exit without repricing the frozen experiment. Retain a genuine observation deadline if no fill occurs.
- Record pending-order and post-fill process reopening plus repeated GET, unchanged economic posting counts and no duplicate POST. UNKNOWN ACK and cancellation/fill races must distinguish actual observations from offline tests.
- Publish final targeted test results, sanitized execution evidence, final HEAD and independent Draft PR.
