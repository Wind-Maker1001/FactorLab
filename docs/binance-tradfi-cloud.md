# Binance TradFi cloud observation and scheduling

The existing `factorlab-demo-execute.service` remains the sole account order owner. The TradFi observer has no credentials, reads the existing account health projection, and performs only public GET requests against the fixed official USD-M Demo host. It does not initialize an account, submit orders, change campaigns, or assign ownership of existing positions.

`factorlab-demo-tradfi-observer.service` maintains private SQLite observations, a current health snapshot, and Eastern-date daily JSON reports in `/var/lib/factorlab-tradfi-observer`. Lifecycle counters survive restarts. Account financials are suppressed when the Execute heartbeat or account evidence expires. Returns and costs without strategy attribution remain null, not zero.

Weekly scheduling uses the existing `cross_market.calendar.us_calendar` (XNYS via `exchange-calendars`), including holidays, DST and early closes. `WeeklySchedule` returns the first actual session of each civil week and the previous completed session as the history cutoff. Quotes must be observed after the scheduled open, before the actual close, and not in the future. Completed sessions are skipped; a missed closed session is not replayed as a historical trade.

## Installation

Install a fixed reviewed revision with the `cross-market` extra in a separate root-owned virtual environment under `/opt/factorlab/tradfi-observer/releases/<commit>/venv`. The service uses `/opt/factorlab/tradfi-observer/current`; retain previous releases for code rollback without replacing observation state.

`tools/install_tradfi_observer.sh <archive> <commit> <python-3.13-or-3.14>` runs as root on the host. Supply a trusted Git archive containing `src`, `pyproject.toml`, `README.md`, `LICENSE`, `NOTICE` and the observer unit. It refuses to unpack over an existing release, installs declared package dependencies with `cross-market`, provisions private state, atomically switches its own release symlink and restarts only the observer.

The observer requires no credentials, trading-control changes or deployment-timer changes. Its unit uses `--task-db /var/lib/factorlab-tradfi-observer/task-mailbox/tasks.sqlite3`; the installer provisions that private subdirectory without replacing an existing database. F03's existing Execute owner uses the same path through `FACTORLAB_DEMO_TASK_DB` and a narrow `ReadWritePaths` grant for the mailbox only. The observer sandbox makes original account state read-only and hides both `/etc/factorlab-demo` and the original workers' systemd credential mounts.

The shipped unit observes QQQUSDT and SPYUSDT, both observed as TRADING / TRADIFI_PERPETUAL / EQUITY in official Demo exchange metadata on 2026-11-24. Additional instruments must be verified against current official Demo exchange metadata before configuring them; missing stock instruments are not replaced with unrelated contracts.

## Forward observation record

On 2026-11-24, the first observer release started on `factorlab-demo-01` at 15:27 UTC. Four observations through 15:29:12 UTC persisted official QQQUSDT books with fresh account projections and no market transport errors. The original Execute process remained unchanged with zero automatic restarts. The next weekly window was 2026-11-30 14:30–21:00 UTC, with completed-history cutoff 2026-11-27; XNYS identifies that preceding Friday's early close separately.

Official exchange metadata also identified SPYUSDT as TRADING / TRADIFI_PERPETUAL / EQUITY, and its public book was observed before adding it to the service configuration. At 15:41:10 UTC, release `5302407` persisted fresh books for both QQQUSDT and SPYUSDT. The ledger retained 28 observations beginning at 15:27:42 UTC across the observer restart; persistent starts was 2, with zero recorded transport disconnects. The daily report existed and the quote/heartbeat health command succeeded. These are market-observation and observer-restart records, not portfolio order, fill or execution-idempotency evidence.

The earlier `193a2db` shared-account admission regression is resolved by F03's Grid-owned inventory/order filtering. Execute is pinned to `201d76ad2a67257127bfb7f9a0f1e01a8d339230`. The [deployment/recovery report](validation/v69-f04/REPORT.md) and [read-only runtime capture](validation/v69-f04/cloud-runtime.json) record current RUNNING status, recovered account, UNKNOWN=0, shared mailbox receipts and enabled boot units. The observer remains pinned to `5302407564be47893529079492163f6e0675d3bb`. Quote observation health and account execution readiness are reported separately.

## Execution integration

The first integration uses static ownership: `NVDAUSDT` belongs exclusively to Grid; `QQQUSDT` and `SPYUSDT` belong to `tradfi-qqq-spy-test`. No dynamic subaccount or position-allocation system is required.

`tradfi_tasks.PortfolioTask` and `TaskMailbox` provide the F04 submission/receipt boundary. `submit(task)` durably binds one immutable target to `task_id`; retries return the original status, and changing a target under an existing ID is rejected. Payloads include schema version, allocation, session, scheduled-versus-acceptance type, price observation time, expiry, allocated USDT equity and complete QQQ/SPY weights. They do not contain order instructions or claim existing positions.

F03's sole account owner calls `outstanding()`, validates allocation/budget/expiry against its authoritative state, executes through its existing machinery and publishes `receipt(task_id, recorded_at=..., status=..., detail=...)`. Receipt statuses are ACCEPTED, RUNNING, RECONCILING, COMPLETED and REJECTED. COMPLETED must follow actual journaled execution/reconciliation, not mere mailbox acceptance. Terminal receipts are immutable. Expired in-flight tasks remain visible for recovery; F04 never silently hides or reissues them. Journal references must be sanitized.

`tradfi_dispatch.dispatch` publishes only after fresh observations of both instruments inside the weekly core session. `--task-config` supplies a static target configuration (`revision`, `equity_usdt`, `weights`); `--task-db` selects the shared mailbox. A restart reuses the same allocation/session/revision identity and its original immutable payload. Explicit `--acceptance-task-id` uses a separate acceptance identity outside the natural schedule while still requiring fresh observed quotes. Acceptance completion is reported separately from natural rebalancing.

F03's deployed consumer reads the shared mailbox inside the original Execute process and publishes ACCEPTED → RECONCILING → COMPLETED receipts. The observer exposes those terminal receipts and a separate `last_successful_acceptance`; `last_successful_rebalance` remains null. Task equity is a request, not an account balance or budget grant: the owner caps acceptance allocation at 20 USDT, preserves Grid's 7 USDT allocation and applies account-wide available margin and reservations. The signal process never becomes a competing account writer.

The mailbox lies outside `/var/lib/factorlab-demo/execute-state`. A coherent disaster-recovery backup must therefore stop both writers and retain the mailbox database/WAL alongside the original Execute journals and observer ledger. The existing account-root backup alone does not contain mailbox intents or terminal receipts. Restarts and code rollbacks use the surviving live state; do not restore old task or account snapshots to replay a completed acceptance. TODO: Extend the existing coherent backup boundary to include the shared mailbox before claiming host-loss restoration acceptance.

TODO: Attach configured strategy histories, indicator evaluation and target checkpoints through the existing isolated strategy machinery. No V69 weights are generated here; original V69 execution belongs to Alpaca Paper.

TODO: Once the shared executor exposes strategy ownership and fill/funding attribution, extend the existing daily accounting with target/actual weights, cash/margin, nominal/economic exposure, stock/contract basis, fees/funding, realized/unrealized P&L, observed slippage, strategy drawdown, and separate stock-reference versus contract returns. Account-wide NAV alone is not strategy performance.

Two QQQ/SPY official Demo round trips were completed by F03, including one mailbox OPEN/CLOSE acceptance pair and held-position Execute restart recovery. Both mailbox tasks are explicitly `ACCEPTANCE`; these fills test the execution path, not original v69 or natural weekly signal generation. The next natural window remains 2026-11-30 14:30–21:00 UTC, using completed history through 2026-11-27. No strategy configuration is currently supplied to this observer, so it does not fabricate a scheduled target. See the deployment report for receipt identities and existing order references.
