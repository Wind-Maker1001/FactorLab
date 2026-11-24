# V69-F03 implementation status (partial)

The new trading.portfolio_target module reuses target_quantity and account
contracts to plan long-only, one-times-collateral portfolio targets from a single
USDT balance. Reductions form a separate phase; increases require a subsequent
reconciled snapshot. Missing prices/rules preserve cash. Ownership is supplied
explicitly; this planner does not infer ownership from exchange positions.
Economic multipliers are explicit instrument metadata (TQQQUSDT requires 3).

This is a planning foundation, not an operational portfolio executor. It is not
wired to DemoStrategyRuntime, Supervisor, Feed or Deployment. Existing Demo
runtime remains NVDAUSDT-only. The planner's caller must supply fresh prices,
current rules, verified ownership and account equity including unrealized P&L.
Lot/minimum-notional admission remains the execution gateway's responsibility.

Outstanding: durable rebalance identities and restart recovery; multi-symbol
Demo gateway/session admission and account allocation; campaign integration;
fill/cancel/UNKNOWN recovery integration; funding, fee and NAV reconciliation;
complete required fault-injection tests and official online validation.

Validation: python -m pytest tests/integrations/test_portfolio_target.py -q
Result: 7 passed. Covers shared cash, reduce-first planning, unavailable symbol,
explicit ownership, economic multiplier, dynamic quantity step and bad weights.

Online record: NOT RUN. No multi-symbol admitted execution path has been
implemented; no exchange request or order was made for this task. This record
must not be treated as official Demo acceptance evidence.

## Follow-up implementation (2026-11-24)

Session account configuration, rule acquisition, strategy symbol validation,
market-feed cursors/books/bars and network plan identity now accept explicit
symbols while preserving NVDA defaults. Gateway admission requires intent,
observed rules and network plan to agree. Bounded quote policies retain their
existing restrictions and must match the plan symbol.

PortfolioRuntime persists scheduler identity and whole batches in PaperStore
before dispatch and delegates recovery/submission to the existing gateway and
controller. Missing or nonterminal order identities fail closed after restart.
It is an integration building block, not yet wired into a multi-symbol Session.
Unsent members after interruption currently require reconciliation/operator
repair; automatic safe continuation is not implemented. Rule freshness, price
rounding and verified allocation are caller responsibilities at this stage.

Tests: 137 passed across runtime, network, feed, strategy-session and portfolio
suites; 28 passed across Demo execution and configured-symbol tests. These
counts overlap neither command's selected files except independent suite scope;
they are not full-repository acceptance.

Official Demo read-only observation using saved Windows credentials:
can_trade=true; no positions; one existing NVDAUSDT BUY open order;
QQQUSDT and SPYUSDT account leverage=20, CROSSED. No credentials, client IDs or
account identifiers are included here. No order was submitted, canceled or
modified. No leverage was changed. Existing Session account isolation refuses
new orders while an account order is active. Account allocation and admitted
leverage-configuration paths still need implementation before acceptance.

F01 evidence inspected at docs/validation/v69-f01/demo_equity_tradfi.json in
its separate worktree: 24 candidates, 20 TRADING. This is metadata evidence,
not execution eligibility or proof of fill. There is no QQQ fill evidence to
provide to F01 yet. v69 original universe remains unchanged.

Remaining requirements: shared-account allocation/admission; true multi-rule
Session; Campaign/Supervisor/Deployment/cloud propagation; 1x configuration;
partial-fill/UNKNOWN end-to-end recovery; authoritative fee/funding/NAV report;
QQQ round trip and two-contract shared-account online execution.

## Batch recovery correction (2026-11-24)

PortfolioRuntime now resumes persisted, unsent batch members automatically.
The durable futures_orders row is authoritative: the existing adapter commits
its UNKNOWN placeholder before HTTP dispatch. A missing row proves no dispatch
through that adapter (including transport refusals that delete their placeholder).
A present row is never resubmitted; GET recovery must import it first. Without
an adapter journal, an attempt marker remains ambiguous and fails closed.
The callback must use this same adapter and journal, within the single account
execution owner. It must not dispatch through an unrelated transport or store.

After each submission the runtime refreshes account authority before spending
cash on another member. batch_status projects UNATTEMPTED, UNKNOWN and existing
exchange statuses from the original journal rather than maintaining a second
order-state ledger. Live/partial members remain pending; their proven-unsent
successors resume after recovery confirms the earlier member terminal.

Crash tests cover before and after the protocol boundary for first, second and
last members of a three-contract batch. They verify three distinct original
client IDs and no duplicate dispatch after reopening the controller journal.
These are injected failures, not official Demo partial-fill/UNKNOWN evidence.

Azure read-only inspection confirmed the original Execute service active and
running the Grid supervisor. No service was restarted/stopped, no remote file
was written and no trading/configuration request was sent. F04's local
tradfi_cloud observer currently reports AWAITING_SHARED_ACCOUNT_EXECUTION_INTERFACE;
its code is observation-only. There is not yet an implemented owner-side request
consumer, allocation registry or leverage-configuration boundary. This task
remains incomplete; online acceptance has not been performed.
