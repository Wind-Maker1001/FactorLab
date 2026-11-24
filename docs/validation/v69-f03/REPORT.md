# V69-F03 official Demo acceptance and engineering delivery

Two QQQ/SPY round trips completed through the existing Azure Execute owner.
F04 TaskMailbox acceptance OPEN/CLOSE receipts are COMPLETED; held-position
restart recovered without duplicate portfolio submissions. No v69 strategy
signal or original asset-pool substitution was used.

## Evidence and accounting

Source: official Binance USD-M Demo POST acknowledgements, subsequent GET order
and user-trade receipts, and the original account owner's persistent journals.
[Owner journals](owner-journals.json) reuses the existing receipt_mapping
validator to match order IDs, quantities, cumulative quote and actual commissions.
No API keys, signatures, secrets or credential-bound account IDs are included.
Exchange order/trade/client IDs are retained for correlation.

| Round | Contract | BUY order / average price | Reduce-only SELL order / average price |
|---|---|---|---|
| 1 | QQQUSDT | 65893831 / 638.40 | 65896503 / 634.21 |
| 1 | SPYUSDT | 65794316 / 717.55 | 65796666 / 714.61 |
| F04 mailbox | QQQUSDT | 65897174 / 635.68 | 65897750 / 636.16 |
| F04 mailbox | SPYUSDT | 65797009 / 716.89 | 65797260 / 714.61 |

Every filled order quantity is 0.01. Both contracts were configured to leverage
1 via official Demo requests and GET-confirmed. Two earlier unfilled portfolio
SELL LIMITs were canceled by the owner and replaced by a new explicit close task;
they have no fills or commissions and are retained in the evidence.

| Accounting item | USDT |
|---|---:|
| Portfolio realized trading P&L, BEFORE commissions | -0.08930000 |
| Actual portfolio commissions | 0.01620863 |
| Portfolio net P&L after commissions | -0.10550863 |
| New QQQ/SPY funding events in acceptance | 0 |
| Concurrent Grid realized P&L | -0.02080000 |
| Concurrent Grid commission | 0.00151870 |
| Concurrent Grid net change | -0.02231870 |
| Opening wallet, after the first two zero-commission BUY fills | 4999.45108420 |
| Closing wallet | 4999.32325687 |
| Wallet delta | -0.12782733 |
| Wallet delta minus portfolio net and concurrent Grid net | 0.00000000 |

Do not deduct commissions twice: realizedPnl in the actual trade receipts is
before commissions. Portfolio net = realizedPnl - commission + scoped funding.
The closing portfolio and account positions are empty in this captured snapshot;
unrealized P&L is zero. NAV = wallet + unrealized P&L = 4999.32325687, equal to
both the official margin balance and the Execute health NAV (difference zero).
The active NVDA BUY reservation remains visible in account available margin.

[Open account](open-account.json), [first close](closed-account.json),
[mailbox open](mailbox-open-account.json), [mailbox held restart](mailbox-restart-held.jsonl),
and [F04 observer](f04-observer-health.json) preserve intermediate facts.
The early [restart snapshot](restart-held.json) intentionally retains the PAUSED
regression instead of disguising it as a successful recovery.

## NVDA protection and service recovery

The 193a2db regression was caused by DemoGridBridge.account checking ALL account
positions against NVDAUSDT. Actual account facts included the newly filled QQQ
and SPY positions. Filtering only Grid-owned instrument inventory/commitment and
exit orders corrected this without clearing positions or restoring an old ledger.
Per-symbol exchange open-order counts were corrected as a separate shared-account
admission issue. The normal Grid later executed its own 0.04 SELL (order
195421988); this accounts for the concurrent Grid P&L above. Portfolio dispatch
records contain only QQQ/SPY operations. They did not cancel or alter NVDA orders,
its leverage, its strategy parameters or its persisted lifetime budget.

Deployment used the existing installer, systemd unit, account lock and same SQLite
files. Installer receipts confirmed ledger inodes preserved. Cloud code is pinned
to 201d76ad2a67257127bfb7f9a0f1e01a8d339230. Final-delivery inspection confirmed
MainPID=335200, active/RUNNING, account_recovered=true and UNKNOWN=0. A historical
STALE_OR_FUTURE_QUOTE diagnostic remains recorded; the current reason is null.
No service restart, deployment or acceptance order is performed for this report.

## F01/F04 integration and merge order

F01 #241 is already merged. Its [market discovery report](../v69-f01/REPORT.md)
and candidate metadata remain the discovery evidence; this report adds actual
QQQUSDT and SPYUSDT Demo fills. It does not reopen #241 or claim v69 qualification.

Preferred merge order: #239 (owner-side consumer) then #242 (F04 observer/producer).
tradfi_tasks.py and tradfi_schedule.py are shared F04-owned contracts, imported
unchanged from #242; blob equality was checked against its current head. #239 owns
the Execute consumer, shared Session/Grid admission and runtime recovery changes.
#242 owns the observer, schedule/producer integration and observer service files.
Merging identical shared blobs produces no second implementation. origin/main
6c4df82 was merged into this branch without conflicts.

Shared mailbox: /var/lib/factorlab-tradfi-observer/task-mailbox/tasks.sqlite3.
Execute uses FACTORLAB_DEMO_TASK_DB and a narrow systemd ReadWritePaths grant;
F04's existing --task-db reads the same DB. Its status is now COMPLETED, with a
separate last_successful_acceptance; no scheduled v69 rebalance is claimed.
The submitted equity_usdt=999999 was deliberately non-authoritative: the owner
ACCEPTED receipt records a 20 USDT cap and actual account margin remained binding.

## Validation limits

Officially observed: two-symbol actual fills, actual commissions, reduce-only
closes, canceled unfilled orders, persistent mailbox receipts, held-position
restart recovery, account/margin/NAV reconciliation and concurrent original Grid
activity. The online window did NOT exhibit partial fills, lost POST responses or
new QQQ/SPY funding settlements. Existing fault injection and batch-crash tests
cover those recovery mechanics; they are not online event evidence. Historical
NVDA funding entries are not attributed to this portfolio acceptance.

Regression and Codex Review results are recorded in REVIEW.md.
