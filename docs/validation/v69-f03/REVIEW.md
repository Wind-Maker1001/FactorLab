# Validation and review status

Regression: 237 passed in 139.20 seconds on the main-synchronized delivery branch.
Selected suites cover portfolio sizing/batches, configured symbols, Demo execution,
network admission, Session, Grid bridge/supervisor, market feed and F01 validation.
Additional targeted campaign-loss persistence check is included in that result.

Codex review of `b74927d` reported two P2 findings:

- Immutable mailbox equity was recomputed after account changes. Reused #242 owner fixes from `87ee5d1` and consumer regressions from `b5ae796`. Persisting the target now precedes ACCEPTED and exchange configuration; resumed tasks read its original equity without deriving a new allocation from funds.
- Portfolio scope left Gateway leverage tied to NVDA. Scope now saves/restores Gateway leverage and maximum leverage and uses 1x for QQQ/SPY. Existing symbol configuration continues to carry the confirmed margin type and one-way mode; Gateway validates the portfolio symbol's own margin configuration.

The other owner recovery fixes from #242 are reused unchanged, not independently reimplemented. Observer installation remains owned by #242.

Current targeted validation: 76 tests passed in the initial consumer/runtime,
strategy-session and futures-gateway run; four new Gateway cases initially
failed because the fixture used an offline rather than Demo-bound account ID.
After correcting that fixture, all 11 consumer tests passed in 18.41 seconds.
They include both QQQ/SPY symbols, NVDA 5x versus portfolio 1x admission,
normal/exceptional scope restoration, and reopening the SQLite target after
fees/unrealized P&L changed before the first exchange configuration.
The first suite invocation referenced an F04-only test file and collected no tests.

Latest main `9386a34` merged without textual conflicts. Applying the complete
F04 commit encountered observer-file modify/delete conflicts; only its owner
source/tests were reused, keeping the deployment boundary unchanged.

The cloud acceptance runtime remains 201d76a. Synchronizing origin/main added
F01 evidence/tests; no deployment or restart was performed for final delivery.
