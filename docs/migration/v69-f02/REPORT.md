# V69-F02 native migration

SOURCE_AVAILABLE. Native signal and research-unit order planning are implemented without Qlib or XGBoost. This replaces the earlier missing-source scaffold. PR #240 remains draft.

## Evidence and limits

- SOURCE_AVAILABLE: all 27 supplied manifest entries verified by SHA-256 and byte count; complete handoff frozen under tests/fixtures/v69_original. Original manifest, protocols, license, matching/audit code and result files retained byte-for-byte. The local untracked research archive is the source; c3f93e5 is host checkout context, not authentication of these bytes. Supplied LICENSE-QLIB is retained; no broader research-code license is invented.
- SIGNAL_EQUIVALENCE: same-input differential against the actual frozen inheritance chain passes. Covers all 280 synthetic sessions, indicators, three holdings states and both omit_sndk settings. Durable weight-differences.csv has 2,688 stock rows over synthetic Monday observations, maximum absolute difference 0. These are synthetic equivalence observations, not historical reference replay results.
- ORDER_INTENT_EQUIVALENCE: original generate_trade_decision and native planner compared on identical inputs at 100,000 and 300,000 USD cash. Explicit tests cover outside holdings, invalid held quotes, zero-target exits, deadband and volume-limited buys/sells. The Qlib transport classes are inert test shims; original strategy math and method bodies execute unchanged.
- FULL_REFERENCE_REPLAY: PENDING. Supplied outputs have 977 daily rows, 204 targets and 371 fills, and final eight weights are zero. The archived reported net return is 38.832504%, max drawdown -4.243085%, initial capital 300,000 USD. These are supplied results, not independently reproduced metrics. fixture/market and Provider are absent. Alpaca data was not substituted.
- ALPACA_INTEGRATION: OFFLINE CONTRACT PASS against ALP-E03 local working tree. No Paper HTTP submission or live-account acceptance claimed. ALP-E03 files are still untracked in its separate worktree and not dependencies committed by this PR; the bridge test skips unless that source is explicitly supplied. It executed one test-only capped, converted order and verified repeated execution did not submit twice. This synthetic test is not a V69 market signal.

## Exact strategy semantics

Actual chain: v69 -> v61 -> v51 -> BandParent(rule=band) -> v50 -> v48. Eight symbols retain original order and TARGET: seven 4% sleeves and TQQQ 4%/3 nominal. MA28, QQQ MA63, 21-return sample standard deviation (ddof=1), upper/lower half-volatility bands and holdings hysteresis retain pandas missing-value behavior. Breadth denominator counts eligible valid/ready stocks only. Strong-market 1.10 scaling precedes V51 weak-market mean exit and V61 global 1.10 scaling. V69 uses rolling 42-return variances/covariance, clipped residual variance, row-wise 75% quantile and strictly greater high-vol classification. Only previously unheld high-vol entries require two consecutive upper-band breaks. omit_sndk removes SNDK from breadth/residual universe and sets its weight to zero. No prelisting fill, renormalization, v85 enhancement or 37.5% clipping is introduced. 37.5% is a reference acceptance cap, not strategy logic.

## Planning and causality

Native planner retains the original operation order and floating-point arithmetic. It values holdings and NAV using the preceding completed session adjusted close plus current cash, computes the 0.005 deadband, exits zero targets regardless of deadband, caps both sides by 0.001 times preceding adjusted volume, and emits sells before buys. Invalid held market inputs and outside holdings raise errors. A zero/invalid NAV is not silently repaired.

V69Strategy.decide accepts explicit history, session calendar, adjusted holdings, cash and JSON state. It requires history to end exactly on the preceding session; appends only an empty current-session label; and restricts execution preparation to the first actual weekly session using the existing calendar, including cross-year ISO weeks. Duplicate completed-week state emits no new orders. Persist state only together with the durable portfolio plan; no inferred fills or hidden holding state. Pure recomputation with the same state yields identical intentions. Midweek process restart does not manufacture a late weekly signal.

Prepare history and full targets before open. There is no opening-observation signal dependency. Execute only in a legal session with fresh raw quotes; E02 execution_quote and E03 run_once own quote causality/session validity. Qlib reference open matching and actual Alpaca limit fills are separate outcomes; equal execution prices are not promised. The old require_causal_execution helper remains a generic observation-order check, not a requirement for V69 opening observations.

## Adjustment and execution contract

adjusted_price = raw_price * adjustment_factor; adjusted_volume = raw_volume / adjustment_factor; adjusted_quantity = raw_quantity / adjustment_factor. Thus real share quantity = adjusted quantity * factor. Caller must supply factors consistent with the exact history basis and execution-session corporate actions. Rebase inventory/factors for splits before planning; do not mix factor epochs. Actual broker prices are raw quotes. Historical Yahoo adjustment basis and E02 Alpaca adjustment modes are different provenance; this PR does not assert their histories interchangeable.

v69_alpaca.to_adjusted_holdings converts official raw inventory for signal/NAV calculations. create_alpaca_plan converts research-unit intentions to raw shares and floors each order to caller-supplied broker quantity steps. It atomically creates the E03 plan and freezes absolute intent targets in the existing target journal, preserving volume caps and deadband rather than allowing midpoint revaluation to change share quantities. A private transaction facade joins E03 create to the same SQLite transaction without mutating the shared executor. This bridge depends on E03 plan schema and must retain the optional contract test when E03 changes. All eight symbols remain owned; TQQQ economic multiplier is 3 for reporting, without applying a second quantity leverage conversion. E03 handles actual equity, fresh quotes, ownership, funds, sell-before-buy and recovery. No 300,000/100,000 account value is hardcoded into sizing. Actual cash/equity facts must come from the account adapter.

No client, ledger, cloud framework or additional manual approval is added. E03 may round, reject or leave residuals under executable cash/market constraints. Original 0.1% one-way research costs stay in the archived research protocol; the bridge deducts no synthetic fee. Actual Alpaca fees belong to execution accounting.

## Reproduction

python -m pytest tests/research/test_v69_boundary.py tests/research/test_v69_equivalence.py -q

python tools/research/validate_v69_migration.py

Optional ALP-E03 contract: set FACTORLAB_ALP_E03_SRC to that worktree's src directory, then run python -m pytest tests/research/test_v69_alpaca_bridge.py -q.

Current validation: 23 strategy/boundary tests plus 1 E03 offline contract test passed. Full real-market DST, suspension/corporate-action feed integration and 977-day historical replay are not claimed by the synthetic harness. Synthetic missing-bar, prelisting, warmup, calendar-holiday/year-boundary and restart cases are covered.
