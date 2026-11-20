# P7-M19-F03 — new-order lifecycle and economic evidence

NO LIVE MONEY.

Development baseline: `203d35dfc592c299455f7a253f04037effdf3cd3`.

## Evidence boundary

The historical BUY/SELL of 0.04 NVDAUSDT, actual fees of 0.00325240 USDT, net P/L of -0.02885240 USDT and wallet of 4999.97114760 USDT remain **HISTORICAL_ACTUAL_VERIFIED**, as recorded in [M19](../p7-m19/REPORT.md) and [F01](../p7-m19-f01/actual-evidence.json). F01 submitted no new order. These are not F03 results.

F03 is currently an offline engineering acceptance. Official Demo observation is **BLOCKED** pending M19-F02 integration, engineering acceptance, serial account ownership handoff and current risk/budget inspection. No F03 account process or exchange observation window has been started. Absence of a window is not NO_NEW_ORDER and is not RIGHT_CENSORED.

## Evidence-chain audit

| Concern | Existing authority and finding |
|---|---|
| New identity | `experiment_evidence` selects POST clientOrderIds absent from baseline dispatches, receipt mappings and controller economic events. It verifies account binding and preserved dispatch parameters. Exchange-adjusted timestamps are not an identity boundary. |
| Reservation → ACK → GET → trades | `DemoSession` reserves writes through the existing runtime; `receipt_mapping` checks orderId, clientOrderId, trade identity, quantity, quote and fee. F03 also requires a GET mapping with the same clientOrderId/orderId/symbol for NEW_DEMO_ACK_VERIFIED; a successful HTTP response alone is insufficient. |
| Partial-fill increments | `FuturesAdapter._rest_order` uses cumulative quantity/quote differences, and `fifo_evidence` uses differences between controller cumulative quantities/fees. Existing variable-sized partial-fill tests cover .01, .02 and .01 increments. |
| Actual fees | `_rest_order` sums Binance userTrades commission, checks cumulative quote/quantity and rejects non-USDT fees. Receipt export rechecks these facts. Estimated gateway fee rates are not final economic evidence. |
| Duplicate booking | Protocol event identity and controller replay protect economic booking; receipt persistence shares the confirmation transaction. Repeated GET and reopening preserve event/receipt counts. F03 compares each new order's cumulative quantity AND fee with its controller events rather than accepting matching aggregate fee totals. |
| ACK versus FILLED | NEW_DEMO_FILL_VERIFIED requires FILLED on GET plus positive actual trade quantity. ACK-only account acceptance remains false. |
| Market movement versus execution | Market marks never supply economic fill events. Restart comparison deliberately excludes mark price, while comparing quantity and entry price. |
| Historical versus new earnings | New fees/events are selected by new POST identities; whole-account FIFO retains its original capital baseline. Wallet/equity changes are account changes, not an isolated strategy return. Historical-only captures cannot set NEW_DEMO flags. |
| Restart omission/state drift | Existing comparison preserves account, intents, economic events, dispatch count, wallet and fees with GET-only reconciliation. F03 additionally compares orderId/status/cumulative quantity/cumulative fee/reconcile-required and nonzero position quantity/entry price. Legitimate fills or cancellations during the gap make the unchanged-state comparison fail; they require separate event evidence, not an unchanged-state claim. |
| Unattributed cash flows | Previously only FUNDING_FEE was explicitly blocked, and income was symbol-filtered. F03 queries account-wide income and requires nonzero income groups to match a known actual trade's commission or realized P/L by type/symbol/tradeId/asset and amount. Unknown, wrong-asset, unrelated-symbol, duplicated and offsetting unbooked events block complete account reconciliation even when final equity happens to match. Funding remains unbooked and blocked. |

An empty income response means no event observed in that response, not complete historical coverage. The existing adapter rejects a full 1000-row response because explicit pagination would be required. TODO: provide time-range-complete income coverage before claiming settlement-history completeness across arbitrary account lifetimes. TODO: attribute confirmed non-trade settlements through the existing authoritative ledger before accepting accounts with those settlements; this change does not invent settlement postings.

## Offline recovery coverage

The existing fault suite covers missing/conflicting trade history, commission asset conflicts, cumulative regression, duplicate stream events, lost POST ACK plus failed GET, partial fills during cancellation, filled cancellation responses and lost cancellation responses, protocol commits preceding controller rollback, and interrupted Grid confirmation replay. `test_acceptance_resumes_only_unattempted_initial_proposal` distinguishes a proposal that never reserved a POST from an already reserved dispatch: only the former can submit.

The new independent-process regression closes both protocol and controller stores, transfers only synthetic exchange business facts to a child process, reopens both original SQLite journals without write authorization, and performs repeated GET recovery. ACK-only, partial-fill and fully-filled states must preserve original order identity, cumulative quantity/commission, wallet, inventory, controller economic events and protocol receipt count. These tests are **OFFLINE_FAULT_VERIFIED**, not official exchange observations.

The receipt regressions additionally reject equal aggregate totals assigned to the wrong order, ACK/orderId or source mismatches, altered restart cumulative execution, altered position economics, and unrecognized offsetting income. Market mark changes alone do not fail position-identity comparison.

Targeted regression: pending completion. The initial five-module regression passed 54 tests on Python 3.13. The expanded Python 3.13 run reached 106 passed / 1 failed: the new ACK-only subprocess test compared a pre-GET original process with a post-GET child, so legitimate first GET evidence violated its unchanged-record assertion. The test now takes an authoritative GET snapshot in the original process before closing the stores, matching the recovery procedure. Final Python 3.14 regression is running against that correction.

Codex Review is **BLOCKED**: the installed reviewer exited with code 1 before producing a review because its configured model was not supported by its ChatGPT account. No review approval or finding count is inferred from the failed invocation.

## Evidence classifications

Existing boolean status names retain their meanings. `experiment_evidence` adds `evidence_classifications` and an observation field without renaming or removing existing fields.

| Classification | Required evidence |
|---|---|
| HISTORICAL_ACTUAL_VERIFIED | Existing historical actual account reconciliation, separately scoped. |
| OFFLINE_FAULT_VERIFIED | Successful synthetic/intercepted fault regression, never an actual exchange claim. |
| NEW_DEMO_ACK_VERIFIED | New POST's actual successful ACK linked to exchange GET identity. |
| NEW_DEMO_FILL_VERIFIED | New order GET FILLED with actual userTrades and validated cumulative execution. |
| NEW_DEMO_FEE_RECONCILED | Per-order actual commission and cumulative quantity equal controller economic facts. |
| NEW_DEMO_ACCOUNT_RECONCILED | Filled new scope plus reconciled whole-account FIFO/position/fees/equity and no unexplained observed income. |
| NEW_DEMO_RESTART_RECOVERY_VERIFIED | Later independent-process capture, new identities present before restart, unchanged economic facts and GET-only recovery. |
| REAL_EXCHANGE_NOT_OBSERVED | Required real lifecycle states are absent, irrespective of offline success. |
| RIGHT_CENSORED | An actual observation deadline is reached with a resting/partially filled order; not inferred merely from taking a snapshot. |
| BLOCKED | An execution or verification prerequisite has not been established. |

NO_NEW_ORDER is only an observation outcome when an actual strategy window produces no new POST. The machine-readable [acceptance summary](acceptance.json) separates unexecuted F03 observation from historical outcomes.

## Official Demo and restart procedure

After F02 integration and serialized account handoff, retain the original NVDAUSDT Grid. Read current authoritative consumption and remaining budget, STOP/HALTED state, ownership, open orders, unknown requests, positions, rules and equity before execution. Use existing RUNNING continuous authorization and automatic windows; do not clear STOP/HALTED or reset consumption. Policy ceilings are not remaining allowance: 100 total POSTs, 700 USDT total submitted notional, 4 POSTs/window, 7 USDT/order, 0.04 NVDAUSDT maximum quantity, 1 USDT maximum loss and one active order.

Freeze a private pre-POST capture, then observe natural strategy signals without repricing or manufacturing an opposite-side trade. For each new identity retain reservation/ACK, GET payloads, orderId, tradeIds, cumulative quantity/quote and actual commission. If a real deadline leaves an order unfilled, record status and RIGHT_CENSORED. Do not inject faults into Demo.

When a real new order exists, capture the original worker's authoritative facts, stop that worker, release ownership, and only then reopen the original journals in an independent process for GET-only recovery. Compare identity, cumulative execution, positions and economic events; assert no new dispatch and no duplicate booking. Preserve pre/post private raw captures outside Git and publish only reviewed business summaries. No two workers may own the same account.

## Acceptance

| Result | State |
|---|---|
| ENGINEERING_ACCEPTED | BLOCKED — final regression and review pending |
| CI_ACCEPTED | BLOCKED — Draft PR CI and trusted merge gate pending |
| DEMO_NEW_ORDER_ACCEPTED | BLOCKED — integration/ownership prerequisites pending |
| DEMO_RECONCILIATION_ACCEPTED | REAL_EXCHANGE_NOT_OBSERVED — no F03 actual fills |
| DEMO_RESTART_ACCEPTED | REAL_EXCHANGE_NOT_OBSERVED — no F03 real new-order restart |
| DEMO_LIFECYCLE_VERIFIED | REAL_EXCHANGE_NOT_OBSERVED |

The full lifecycle contract still requires NEW/PARTIALLY_FILLED/FILLED/CANCELED, actual UNKNOWN-ACK GET recovery and cancellation/fill race, account reconciliation and independent-process recovery. Offline success cannot set that exchange result. No automatic merge.
