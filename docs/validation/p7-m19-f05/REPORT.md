# P7-M19-F05 — official Demo new-order and economic evidence

NO LIVE MONEY.

Development baseline: `555a9bfdf0615f89146eddba991854299010dd97`.

## Evidence boundary

The historical 0.04 NVDAUSDT BUY/SELL, 0.00325240 USDT fees, -0.02885240 USDT net P/L and 4999.97114760 USDT wallet remain historical evidence in [M19](../p7-m19/REPORT.md). They are not F05 results.

F05 observation is NOT_STARTED, pending F04 completion and main integration, regression and exclusive account handoff. F04 remains in preparation with no integrated change. C02 has no active Demo observation or account ownership; that does not establish release by any original Windows account worker. No F05 account network session has been opened. An unstarted window is neither NO_NEW_ORDER nor RIGHT_CENSORED. Execution must retain the original frozen Grid, RUNNING authorization, persistent stop boundaries, history and actual remaining budgets.

## Offline preparation

Reuse F03 capture, receipt mapping, experiment identity selection and existing FIFO attribution. The recovery exporter now keeps unchanged-state facts separate from an additional natural-fill comparison: original economic events must be preserved as a prefix; GET userTrades must preserve old trades and explain each order's quantity, quote and commission delta; appended controller events must explain those same deltas; realized P/L minus actual commissions must explain the wallet change. No extra dispatch or write HTTP request is accepted. Changed facts are not reported as unchanged.

The unchanged baseline passed the existing seven-module Python 3.14 regression: 107 tests in 105.51 seconds. The extension, including account-position cross-checks and adversarial fee, wallet, entry-price and duplicate-event changes, passed the same seven modules: **108 tests in 104.45 seconds**. The expanded seven-module regression passed **110 tests in 107.87 seconds**, including five independent-process cases: unchanged ACK, partial and filled orders, plus ACK→partial and partial→filled during process handoff. The child reopens original protocol/controller journals without authorization and checks GET-only calls, preserved economic-event prefixes, one new event/receipt for the incremental fill and no additional booking on repeated GET.

Direct self-review found two evidence gaps: comparing only dispatch counts could miss changed dispatch parameters, and experiment-level output did not expose the natural-fill explanation. The comparison now also preserves the complete dispatch records; experiment output includes the explicit restart outcome and trade delta. Post-review receipt and closed-loop regressions passed **29 tests in 50.62 seconds**, including the changed-dispatch-parameter rejection and experiment-level natural-fill export. Direct self-review of the changed acceptance and test paths is complete; no remaining defect was identified in those paths. Codex review exited with code 1 before producing findings because configured model `gpt-6.2-sol` is unsupported by the current ChatGPT account. This is a direct self-review, not completed external Codex Review. TODO: resolve the existing income time-range coverage boundary before claiming complete settlement coverage for this experiment.

## Reused acceptance and execution paths

| Requirement | Authority / evidence |
|---|---|
| Pre-experiment account and historical identities | `capture` followed by `experiment_evidence`; preserve private capture before observation |
| POST reservation, ACK and GET identity | Runtime dispatch journal and `receipt_mapping`; new scope excludes baseline dispatch, receipt and economic-event IDs |
| Cumulative trades and actual fees | Existing adapter confirmation transaction, persisted userTrades and per-order controller quantity/fee comparison |
| Controller and FIFO economics | `fifo_evidence` calls existing `attribute_events`; no separate account model |
| Wallet, equity and NAV | Whole-history FIFO summary plus exchange balances; experiment wallet/equity deltas are changes, not isolated strategy returns |
| Historical versus new economics | Original opening capital remains 5000 USDT; experiment baseline is a separate capture, never a new capital origin |
| Restart | Original journal reopening, GET-only recovery, unchanged comparison or explicitly verified natural fill delta |
| Unknown income and duplicate fees | Account-wide income attribution and per-order receipt/controller comparisons; unresolved settlement coverage blocks completeness |
| No order, no fill and unstarted window | Window outcome requires actual start/end logs; identity exporter alone does not declare NO_NEW_ORDER or RIGHT_CENSORED |
| Market unavailable / rejected signal | Retain supervisor/feed reason, Grid reason and account admission state; do not label unavailable feed as a completed no-signal window |

The existing executable observation entry is `factorlab-demo grid-supervisor start --execute --interval 1m --seconds 180 --poll-seconds 5`, used only after the F04 gate and account handoff. Do not pass `--new-deployment` or a replacement configuration. Supervisor final output includes request counts, feed watermarks, account and Grid status; preserve it privately. Baseline and recovery export reuse the existing `demo_fill_acceptance` module with `--opening-wallet 5000`, unique `--output` files, `--experiment-baseline` and, after actual new-order process handoff, `--previous`.

`DemoRuntime.report` exposes continuous authorization state, total limits and historical consumption separately, along with active-window consumption and recovery diagnostics. Compute remaining allowance from total minus consumed; `ensure_window` owns window renewal. `operator_lock` and account ownership must be checked with the same account before observation; changing directories does not authorize concurrent ownership.

The intended observation is one original-deployment window: interval=1m, seconds=180, poll-seconds=5. Frozen deployment reference: [F01 definition](../p7-m19-f01/frozen-definition.json). Current account facts and remaining budgets must be read from authoritative stores at execution, not copied from historical values in that definition.

## Acceptance

| Result | State |
|---|---|
| ENGINEERING_ACCEPTED | PENDING — preparation and regression in progress |
| CI_ACCEPTED | PENDING — trusted CI not run for F05 |
| DEMO_NEW_ORDER_ACCEPTED | BLOCKED — F04 integration and exclusive account handoff pending |
| DEMO_FILL_ACCEPTED | REAL_EXCHANGE_NOT_OBSERVED |
| DEMO_FEE_RECONCILED | REAL_EXCHANGE_NOT_OBSERVED |
| DEMO_ACCOUNT_RECONCILED | REAL_EXCHANGE_NOT_OBSERVED |
| DEMO_RESTART_ACCEPTED | REAL_EXCHANGE_NOT_OBSERVED |
| DEMO_LIFECYCLE_VERIFIED | REAL_EXCHANGE_NOT_OBSERVED |

Actual F05 exchange HTTP counts so far: GET=0, POST=0, DELETE=0. No new order identity, ACK, fill, fee or independent-process actual-order evidence has been obtained. Partial fills, UNKNOWN-ACK recovery and cancellation/fill races remain REAL_EXCHANGE_NOT_OBSERVED. Raw future evidence belongs outside Git; only reviewed business summaries may be published.
