# P7-M19-F01 — new Demo lifecycle evidence

NO LIVE MONEY.

The experiment baseline is `4926a60c9b81d4f83245f2f04faaf729c7e50836`. Historical evidence in [`../p7-m19/REPORT.md`](../p7-m19/REPORT.md) remains unchanged. Orders 191862895 / 191865793, trades 39911928 / 39913139, fees 0.00325240 USDT and FIFO net -0.02885240 USDT are historical results, not new experiment executions.

## Execution prerequisite

A read-only inspection of the persisted runtime on 2026-11-17 found continuous authorization `STOPPED` and no active campaign. The persisted policy retains 100 cumulative POSTs, 700 USDT cumulative submitted notional, 4 POSTs per 10800-second window and 1 USDT global loss limit. The runtime contains seven POST dispatch rows across all campaign types; this total must not be confused with continuous-policy consumption, which excludes smoke campaigns.

The existing `start_continuous` path rejects a saved authorization whose state is not RUNNING. Automatic window rotation also requires RUNNING. The explicit `resume-continuous` extension preserves the same authorization and all frozen limits, records the complete stopped authorization and stop epoch in the runtime audit, requires account GET reconciliation, and rejects a concurrent STOP, risk HALTED state or exhausted global budget. It does not delete STOP history or reset consumption/opening equity. Offline verification and constructor CLOCK_SKEW integration remain prerequisites for actual observation.

## Evidence classification

The new acceptance exporter selects POST identities absent from the frozen baseline and derives their receipts, fees and economic events from the existing account journal. Whole-account FIFO/NAV remains based on the original wallet baseline rather than resetting capital at experiment start. Historic account closure alone cannot establish any NEW_DEMO state.

The directed continuous-policy, receipt and fill-fault selection passed **55 tests in 172.83 seconds**. This includes explicit resume identity/consumption/STOP-audit preservation, rejection of unsafe resume states, partial-fill restart, UNKNOWN ACK with failed GET, partial fill during cancellation, and full fill during cancellation with either a FILLED response or a lost cancellation response. These results are **OFFLINE_FAULT_VERIFIED**, not exchange observations. The separate initial receipt/fault/closed-loop/continuous/FIFO selection passed **55 tests in 74.94 seconds**. Independent-process evidence discrimination is under additional directed verification.

No new real exchange ACK, fill, partial fill, UNKNOWN ACK recovery, cancellation race or independent-process recovery is claimed by this report.

| Result | Current state |
|---|---|
| REAL_DEMO_NEW_EXPERIMENT_STATUS | BLOCKED_BY_PERSISTED_STOP; observation not started |
| FIFO_NAV_RECONCILIATION_STATUS | HISTORICAL_ONLY; new economic result not observed |
| RESTART_RECOVERY_STATUS | NEW_REAL_ORDER_NOT_OBSERVED |
| LIFECYCLE_EVIDENCE_GAPS | New ACK/NEW/FILLED/fees/account closure; pending-order and post-fill process restart; cancellation; real partial fill, UNKNOWN ACK and cancel/fill race |

Special fault scenarios require separate OFFLINE_FAULT_VERIFIED and REAL_EXCHANGE_NOT_OBSERVED outcomes. Full DEMO_LIFECYCLE_VERIFIED retains all existing M19 necessary conditions; unfilled orders at an actual observation deadline will be recorded as right-censored, not repriced to manufacture a fill.
