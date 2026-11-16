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

At initial recovery the runtime had no active Campaign. A continuous execution window was subsequently established, and the integrated execute command was attempted with the unchanged strategy. It failed during initial account time validation before entering the observation loop. No new M19 POST, DELETE or trade was produced; the historical reconciled roundtrip is not a new M19 execution.

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

## Integrated bounded observation definition

The integrated runtime combines M18 `9ff7078a3145ffd0df8757bdbfd0680faec9a75e` with M19 `cfd2b67` in a private source snapshot, without adding Campaign implementation to this independent PR. The combined receipt/M16 partial-fill/fault selection passed **17 tests in 43.13s**, including the deterministic fixture clock correction. Before execution, the observation is fixed to the existing supervisor with `--interval 1m --poll-seconds 60 --seconds 180 --execute`. Existing deployment, prices, quantity/risk budgets and residual-inventory safeguards remain unchanged. No new-deployment option is permitted. The observation ends at that duration, whether or not a natural fill occurs; post-observation recovery records actual orders/trades and any remaining inventory or order.

The integrated pre-observation recovery succeeded. At **2026-11-16 15:13:15.943 UTC**, the actual top of book was bid **159.17**, ask **159.94**. Current filters confirmed tick **0.01000**, quantity step/minimum **0.01**, minimum notional **5 USDT**, and PERCENT_PRICE multipliers **0.9700–1.0300**. The frozen 0.04 BUY at 159.47 has notional **6.3788 USDT**; the frozen 0.04 SELL at 162.71 has notional **6.5084 USDT**. Both fit the observed tick/lot/min-notional and unchanged per-order quantity/notional budgets; final dispatch remains subject to current exchange/runtime checks.

The continuous authorization was RUNNING with no STOP latch, 4 historical ordinary POSTs / **25.5692000 USDT** consumed, global caps 100 POSTs / 700 USDT, and maximum loss **1 USDT** from **4999.97114760 USDT**. The existing execution window had 4 unused POSTs and expires naturally at **2026-11-16 18:08:20.524841 UTC**. The fixed 180-second execute command exited during initial account construction: `/fapi/v3/account` returned **HTTP 400 / -1021 CLOCK_SKEW** before the observation loop began. No observation deadline or new order lifecycle is claimed from that failed startup. A separate GET-only diagnostic measured `/time` RTT **630.49 ms**, offset **-1765 ms**, midpoint uncertainty **315.24 ms**, and an immediate signed account GET that returned 200 with the unchanged **5000 ms recvWindow**. That sample neither reproduces nor diagnoses the earlier failure, and is not counted as a successful supervisor startup. No time-calibration change, automatic retry loop, pricing change or authorization expansion was introduced. Constructor-time CLOCK_SKEW remains an explicit deferred diagnostic TODO.

## Final recovery and acceptance boundary

The final separate-process recovery at **2026-11-16 15:16:44 UTC** succeeded. All eight before/after comparisons passed: same account, same historical intents, unchanged economic events, unchanged dispatch count, unchanged wallet and fees, reconciled account and GET-only operation. Wallet/equity remain **4999.97114760 USDT**, with no inventory, open orders or UNKNOWN requests. The experiment added **0 POST, 0 DELETE, 0 trades**. Account execution ownership was handed back after all M19 account processes ended; subsequent continuous-window/STOP observations are outside this experiment's snapshot.

[`integrated-observation.json`](integrated-observation.json) retains the frozen definition/current rules, measured clock diagnostic, startup failure, final actual trade mappings/FIFO, recovery comparison, and **separate historical versus new-experiment acceptance states**. Observed real lifecycle states are NEW, FILLED and CANCELED; actual PARTIALLY_FILLED, UNKNOWN-ACK resolution and cancellation/fill race are not established. Completed-order repeat GET is established.

The independent Draft PR is [#196](https://github.com/Wind-Maker1001/FactorLab/pull/196). The historical actual-fill/fee/position/account/restart reconciliation is verified; **the complete new M19 real-order lifecycle acceptance is not complete**.

## Remaining completion conditions

- Diagnose the constructor-time CLOCK_SKEW startup failure before a new execution attempt; neither a larger recvWindow nor repeated order attempts is an accepted substitute for diagnosis.
- Complete the original frozen-strategy observation in a serialized integrated execution window. The failed constructor did not observe 180 seconds and is not an unfilled-order deadline result.
- Obtain new actual entry and confirmed-inventory reduceOnly exit receipts, or retain genuine unfilled-order/deadline evidence without repricing or synthetic fills.
- Demonstrate pending-order and new-fill process reopening with no duplicate booking/POST. Actual PARTIALLY_FILLED, UNKNOWN ACK and cancellation/fill race evidence remain distinct from the passing offline tests.

