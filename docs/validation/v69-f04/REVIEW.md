# V69-F04 review

Codex CLI 0.165.0, model `gpt-6.1-sol`, read-only, xhigh, reviewed against `origin/main`. The initial review completed with six findings:

- Requested task equity was replaced by the owner's allocation ceiling. The consumer now caps the requested equity by the account-derived ceiling.
- Reopened shared sessions retained Grid's account-wide 7 USDT limit. Session construction now restores the total account budget from the persisted static allocation, preserving per-instrument limits.
- Expired tasks could leave resting orders indefinitely. Recovery now cancels only known orders from the original task's persisted batches and reconciles before terminal rejection; expired tasks cannot submit new orders.
- Installation omitted Execute's shared-mailbox environment and sandbox write grant. The installer now installs the same narrow drop-in used on Azure without restarting Execute. A new installation requires a coordinated Execute start before task production.
- Repeated polls recomputed immutable target equity. Existing targets now reuse their persisted equity despite subsequent account changes.
- Expiry preceded durable completion recovery. The consumer now republishes a saved terminal result before evaluating expiry.

Targeted regression after the fixes: 31 tests passed in 42.39 seconds across the portfolio consumer/runtime, TaskMailbox and existing Demo strategy-session tests; installer shell syntax passed. Coverage includes requested-equity capping, persisted equity after account changes, original-batch expiry cancellation and durable completion after expiry.

Codex follow-up stopped with exit code 1 after reporting the account usage limit; it produced no verification conclusion. Subsequent source review verified all six fixes without additional correctness findings: capped/persisted target equity, saved-result replay before expiry, original-batch cleanup through the existing known-active-order cancellation gate, persisted shared account budget restoration, and matching installer environment/sandbox grants. An additional real-scope authorization regression proves expiry blocks POST while permitting DELETE; all six consumer tests passed in 5.04 seconds. Constructor-level shared-budget restoration was verified by tracing persisted metadata and limit construction. These fixes have not been deployed to the running Execute owner.

Before the fixes, 43 focused tests and 123 additional Grid/isolation/recovery/systemd tests passed; installer shell syntax passed. The first new regression run had 17 passes and two failures caused by an incomplete test fixture (`controller.broker` missing); the fixture was corrected before rerunning.
