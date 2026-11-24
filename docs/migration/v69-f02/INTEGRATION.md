# V69-F02 public integration handoff

This PR is independently reviewable before E03 merges. main synchronized at 6c4df82. No runtime, executor, ledger or account owner is duplicated. No acceptance orders were submitted.

## Frozen E03 public boundary

PortfolioExecutor.create retains its existing account/strategy/session identity and accepts:

- quantity_deltas: complete signed Decimal broker-share vector. Zero means no trade. Missing sleeves do not mean liquidation. E03 freezes targets against owned planning inventory; it must not recalculate these quantities using current quote weights.
- signal_inputs: immutable serializable preparation snapshot. F02 writes kind=V69_NATURAL, checkpoint, raw holdings, execution-basis factors, quantity steps and caller provenance. E03 includes this snapshot in idempotency comparison and the same creation transaction as the task.

F02 calls only create. It does not read or mutate plan fields, copy executors, wrap transactions, calculate fees or submit orders. E03 owns claims, atomically frozen quantities, sell-before-buy execution, broker-step rounding, raw quotes, actual account funds and order recovery. A changed checkpoint or quantity under the same identity is an error. Deliberate test weights must use a separate strategy/task identity and evidence category from V69_NATURAL.

The public E03 implementation is not on main as of this delivery. The integration test directly imports the official module and automatically activates in standard CI after merge. Independent bridge contract tests run now. Do not merge a substitute E03 implementation into F02 merely to eliminate the dependency skip.

## E02 adjustment boundary

Verified against the E02 service prepare_history(..., adjustment_basis='yahoo_ratio') source:

adjust_ratio = adjusted_close / raw_close
adjusted_open = raw_open * adjust_ratio
adjusted_volume = raw_volume / adjust_ratio

Use inputs(..., prepared=prepared, use_opening=False). Require the full eight-symbol prior-session matrices, validity and receipt/completion cutoffs. Never switch to vendor_adjusted_volume silently. The formula matches the Yahoo convention; cross-provider values are not asserted equal.

F02 factors passed to create_alpaca_plan refer to the execution-session raw-share basis, expressed in the same adjusted unit system as prepared history. Do not blindly reuse the previous session factor across a split. After an overnight split, obtain authoritative execution-day actions, reconcile raw inventory and construct that day's factor on the frozen adjusted basis before converting quantity. If that evidence is unavailable, preparation is incomplete; do not guess the factor. An adjusted-share holding is raw shares divided by the corresponding factor; raw intention is adjusted intention times the execution factor. Caller provenance should include preparation cutoff, factor session, action IDs and source/basis identity. Raw quotes go to E03; adjusted prices never become broker limit prices. Research 0.1% cost is not charged by this bridge.

## E04 scheduling and recovery handoff

E04 currently remains an observation runtime with WAITING_F02/WAITING_E03_HANDOVER markers. Its public weekly_window already exposes execution_session and previous-session signal_cutoff. Full runtime activation is deferred with the unmerged E03/E04 integration; no live handoff is claimed here.

Preparation flow for that integration:

1. Before first weekly session, E02 prepare_history provides only completed prior sessions, with full original universe and explicit adjustment provenance.
2. Reconcile official raw holdings/cash and execution-day factors. Convert holdings with to_adjusted_holdings. V69Strategy.decide(event, checkpoint) prepares original weekly weights and capped adjusted-share orders without needing an opening observation.
3. create_alpaca_plan passes converted signed deltas plus the resulting checkpoint to E03 create in one atomic public call. Initial inventory adoption remains E03's explicit existing contract.
4. Advance the runtime's checkpoint only from the persisted task's signal_inputs after create succeeds. If preparation crashes before create, neither is committed; recompute from the retained immutable input snapshot. If it crashes after create, recover the task and checkpoint, not a new calculation using post-fill holdings. Do not separately commit last_week before a durable task exists.
5. During the legal first-session execution window E03 runs/reconciles the persisted task with raw causal quotes. Repeated startup resumes that task's original client IDs. A completed-week checkpoint produces no extra task; a naturally flat signal produces an all-zero-delta task and no forced trade.

The immutable task snapshot is authoritative for recovery; a separate runtime checkpoint is only a recoverable projection. E04 must retain the preparation snapshot or recover the existing task before calling a fresh decision. This is the required integration contract, not an assertion that the observation service has been switched to execution.

## Validation and review

Latest-main regression: 25 passed, 1 E03 module dependency skip. Public E03 source-snapshot bridge validation: 3 passed. Natural flat case produces only zero deltas. No Paper network writes, simulated acceptance orders or historic market substitutions occurred. FULL_REFERENCE_REPLAY=PENDING remains separate from the strategy module's review readiness.

Codex Review (independent read-only reviewer): no actionable correctness findings in this integration diff relative to e066f56. Reviewer independently ran the bridge tests on main (2 passed, 1 E03 dependency skip) and checked the E03 public source API. The primary agent additionally ran all 3 bridge tests against that source snapshot. Neither review exercise constitutes broker execution or full crash-recovery acceptance. No indicator reimplementation was reviewed or added in this round.
