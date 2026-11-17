# P7-M19-F01 — new Demo lifecycle evidence

NO LIVE MONEY.

The experiment baseline is `4926a60c9b81d4f83245f2f04faaf729c7e50836`. Historical evidence in [`../p7-m19/REPORT.md`](../p7-m19/REPORT.md) remains unchanged. Orders 191862895 / 191865793, trades 39911928 / 39913139, fees 0.00325240 USDT and FIFO net -0.02885240 USDT are historical results, not new experiment executions.

## Execution prerequisite

A read-only inspection of the persisted runtime on 2026-11-17 found continuous authorization `STOPPED` and no active campaign. The persisted policy retains 100 cumulative POSTs, 700 USDT cumulative submitted notional, 4 POSTs per 10800-second window and 1 USDT global loss limit. The runtime contains seven POST dispatch rows across all campaign types; this total must not be confused with continuous-policy consumption, which excludes smoke campaigns.

The existing `start_continuous` path rejects a saved authorization whose state is not RUNNING. Automatic window rotation also requires RUNNING. The explicit `resume-continuous` extension preserves the same authorization and all frozen limits, records the complete stopped authorization and stop epoch in the runtime audit, requires account GET reconciliation, and rejects a concurrent STOP, risk HALTED state or exhausted global budget. It does not delete STOP history or reset consumption/opening equity. Constructor CLOCK_SKEW recovery is integrated as `3163e9b`. The combined constructor/continuous-policy/receipt selection passed **75 tests in 123.84 seconds** before account authorization recovery or execution. The actual GET-only baseline capture made **38 GET, 0 POST, 0 DELETE**, confirmed the saved deployment and reconciled historical account, and retained continuous consumption of **4 POST / 25.5692000 USDT**. [`frozen-definition.json`](frozen-definition.json) records the non-sensitive observation inputs; raw binding, baseline and timestamped process window remain in private evidence.

## Evidence classification

The new acceptance exporter selects POST identities absent from the frozen baseline and derives their receipts, fees and economic events from the existing account journal. Whole-account FIFO/NAV remains based on the original wallet baseline rather than resetting capital at experiment start. Historic account closure alone cannot establish any NEW_DEMO state.

The directed continuous-policy, receipt and fill-fault selection passed **55 tests in 172.83 seconds**. This includes explicit resume identity/consumption/STOP-audit preservation, rejection of unsafe resume states, partial-fill restart, UNKNOWN ACK with failed GET, partial fill during cancellation, and full fill during cancellation with either a FILLED response or a lost cancellation response. These results are **OFFLINE_FAULT_VERIFIED**, not exchange observations. The separate initial receipt/fault/closed-loop/continuous/FIFO selection passed **55 tests in 74.94 seconds**. The receipt/process-discrimination selection passed **21 tests in 66.97 seconds**; M14 fee-conflict/runtime/FIFO passed **24 tests in 15.19 seconds**. A new experiment additionally requires a later capture from a different process, with the new order identities already present before that restart.

The bounded execute process exited normally after the configured observation, with **106 GET, 0 POST, 0 DELETE**. Terminal admission reason was `FRESH_CLOSED_BAR_REQUIRED`; the last processed bar was **2026-11-15 14:50:00 UTC**, still historical. Consequently no new order was submitted. This is not an unfilled resting order and is not claimed as natural-price right censoring. Two subsequent independent-process account captures each used **38 GET, 0 POST, 0 DELETE**. All existing recovery comparisons passed, wallet/equity remained **4999.97114760 USDT**, and no inventory, open order or UNKNOWN remained. This confirms historical account recovery and no duplicate dispatch/economic posting, but cannot establish pending-new-order or new-fill recovery without a new order. The execution worker has exited; continuous permission remains RUNNING with unchanged consumed budget. No automatic worker was launched. The network window was released after recovery.

Codex identified a clock-domain mismatch between local capture timestamps and exchange-adjusted receipt timestamps. New receipt selection now uses the frozen POST identity boundary alone, avoiding exclusion of genuine new-order receipts when the exchange clock correction is negative; a directed clock-offset regression passed.

No new real exchange ACK, fill, partial fill, UNKNOWN ACK recovery, cancellation race or new-order independent-process recovery is claimed by this report.

| Result | Current state |
|---|---|
| REAL_DEMO_NEW_EXPERIMENT_STATUS | OBSERVATION_COMPLETED_NO_NEW_ORDER; 106 GET / 0 POST / 0 DELETE; fresh closed bar admission not reached |
| FIFO_NAV_RECONCILIATION_STATUS | HISTORICAL_ONLY; new economic result not observed |
| RESTART_RECOVERY_STATUS | HISTORICAL_ACCOUNT_RECOVERY_VERIFIED; two independent GET-only captures; NEW_REAL_ORDER_NOT_OBSERVED |
| LIFECYCLE_EVIDENCE_GAPS | New ACK/NEW/FILLED/fees/account closure; pending-order and post-fill process restart; cancellation; real partial fill, UNKNOWN ACK and cancel/fill race |

Special fault scenarios require separate OFFLINE_FAULT_VERIFIED and REAL_EXCHANGE_NOT_OBSERVED outcomes. Full DEMO_LIFECYCLE_VERIFIED retains all existing M19 necessary conditions; unfilled orders at an actual observation deadline will be recorded as right-censored, not repriced to manufacture a fill.
