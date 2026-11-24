# Shared-account Demo execution and F04 mailbox

Current runtime: one Azure Execute process, one TradingController, one protocol
journal, one account cash/NAV ledger. Grid owns NVDAUSDT; the acceptance
allocation owns QQQUSDT and SPYUSDT. These contracts test the executor only and
do not replace the original v69 universe or validate the v69 strategy.

The Execute supervisor consumes PR #242 PortfolioTask/TaskMailbox directly.
The producer has no order client. The owner derives a test capital ceiling from
actual wallet/equity, preserves the Grid's 7 USDT allocation, and caps the test
allocation at 20 USDT. Available margin and all existing reservations remain
account-wide. A submitted equity_usdt value is not an account balance or grant.
Maximum account allocation is 27 USDT; symbol and allocation admission remain
separate. Orders use fresh official Demo rules and bounded marketable LIMITs
with a 0.5% price cushion, plus all existing rule/filter checks.

Session refreshes quotes for all instruments reserving cash before shared risk
admission. Grid inventory, commitment and exit views include NVDA only. Exchange
open-order filter counts are per symbol; aggregate money/risk admission remains
account-wide. Unknown ownership on a target symbol blocks that target.

Each portfolio task and batch is immutable in the existing PaperStore journal.
The original adapter persists its UNKNOWN placeholder before HTTP. Recovery
queries the original client IDs and resumes only proven-unsent members. A fresh
account is read again before subsequent planning. Canceled/rejected batches
terminate without endless new-ID replanning. Per-task campaigns reuse the runtime
metadata and dispatch table, renew time windows without resetting consumed
identities, and persist the opening-equity loss baseline. Account STOP/HALTED
controls remain authoritative. No API key, client or account ledger is cloned.

Mailbox receipts are ACCEPTED/RECONCILING until confirmed order/account outcomes
permit COMPLETED or REJECTED. Expired tasks are GET-recovered first, including
in-flight tasks; expiry is checked again at network admission. A crash after
journal completion but before the mailbox receipt republishes that completion.

Cloud wiring:

- Execute unit: factorlab-demo-execute.service (existing service and lock).
- Shared DB: /var/lib/factorlab-tradfi-observer/task-mailbox/tasks.sqlite3.
- Execute environment: FACTORLAB_DEMO_TASK_DB points to that DB; only this shared
  subdirectory is added to its systemd ReadWritePaths.
- F04 observer uses its existing --task-db option for the same DB. Its market
  collector remains credential-free and its execution status follows receipts.
- The earlier private JSON inbox is retained for recovery/acceptance cancellation
  compatibility; new F04 portfolio tasks use TaskMailbox exclusively.

Official acceptance is documented in [the online report](validation/v69-f03/REPORT.md).
The read-only export tool tools/export_demo_portfolio_owner_evidence.py reuses
receipt_mapping to verify actual order/trade/fee associations without exchange IO.

Limits: static QQQ/SPY allocation, not same-symbol subaccounting. No actual
partial fill, lost POST response or new QQQ/SPY funding settlement occurred in
the online window; those must not be inferred from ordinary restart recovery.
Fault injection covers the existing order recovery path and six batch crash
boundaries. This change does not qualify v69 research or authorize live money.
