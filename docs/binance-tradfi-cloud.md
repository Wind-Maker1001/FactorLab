# Binance TradFi cloud observation and scheduling

The existing `factorlab-demo-execute.service` remains the sole account order owner. The TradFi observer has no credentials, reads the existing account health projection, and performs only public GET requests against the fixed official USD-M Demo host. It does not initialize an account, submit orders, change campaigns, or assign ownership of existing positions.

`factorlab-demo-tradfi-observer.service` maintains private SQLite observations, a current health snapshot, and Eastern-date daily JSON reports in `/var/lib/factorlab-tradfi-observer`. Lifecycle counters survive restarts. Account financials are suppressed when the Execute heartbeat or account evidence expires. Returns and costs without strategy attribution remain null, not zero.

Weekly scheduling uses the existing `cross_market.calendar.us_calendar` (XNYS via `exchange-calendars`), including holidays, DST and early closes. `WeeklySchedule` returns the first actual session of each civil week and the previous completed session as the history cutoff. Quotes must be observed after the scheduled open, before the actual close, and not in the future. Completed sessions are skipped; a missed closed session is not replayed as a historical trade.

## Installation

Install a fixed reviewed revision with the `cross-market` extra in a separate root-owned virtual environment under `/opt/factorlab/tradfi-observer/releases/<commit>/venv`. The service uses `/opt/factorlab/tradfi-observer/current`; retain previous releases for code rollback without replacing observation state.

`tools/install_tradfi_observer.sh <archive> <commit> <python-3.13-or-3.14>` runs as root on the host. Supply a trusted Git archive containing `src`, `pyproject.toml`, `README.md`, `LICENSE`, `NOTICE` and the observer unit. It refuses to unpack over an existing release, installs declared package dependencies with `cross-market`, provisions private state, atomically switches its own release symlink and restarts only the observer.

No changes to the original Execute unit, credentials, trading controls or deployment timer are required. The sandbox makes the original account state read-only and hides both `/etc/factorlab-demo` and the original workers' systemd credential mounts.

The shipped unit observes QQQUSDT and SPYUSDT, both observed as TRADING / TRADIFI_PERPETUAL / EQUITY in official Demo exchange metadata on 2026-11-24. Additional instruments must be verified against current official Demo exchange metadata before configuring them; missing stock instruments are not replaced with unrelated contracts.

## Execution integration

The first integration uses static ownership: `NVDAUSDT` belongs exclusively to Grid; `QQQUSDT` and `SPYUSDT` belong to `tradfi-qqq-spy-test`. No dynamic subaccount or position-allocation system is required.

`tradfi_tasks.PortfolioTask` and `TaskMailbox` provide the F04 submission/receipt boundary. `submit(task)` durably binds one immutable target to `task_id`; retries return the original status, and changing a target under an existing ID is rejected. Payloads include schema version, allocation, session, scheduled-versus-acceptance type, price observation time, expiry, allocated USDT equity and complete QQQ/SPY weights. They do not contain order instructions or claim existing positions.

F03's sole account owner calls `outstanding()`, validates allocation/budget/expiry against its authoritative state, executes through its existing machinery and publishes `receipt(task_id, recorded_at=..., status=..., detail=...)`. Receipt statuses are ACCEPTED, RUNNING, RECONCILING, COMPLETED and REJECTED. COMPLETED must follow actual journaled execution/reconciliation, not mere mailbox acceptance. Terminal receipts are immutable. Expired in-flight tasks remain visible for recovery; F04 never silently hides or reissues them. Journal references must be sanitized.

`tradfi_dispatch.dispatch` publishes only after fresh observations of both instruments inside the weekly core session. `--task-config` supplies a static target configuration (`revision`, `equity_usdt`, `weights`); `--task-db` selects the shared mailbox. A restart reuses the same allocation/session/revision identity and its original immutable payload. Explicit `--acceptance-task-id` uses a separate acceptance identity outside the natural schedule while still requiring fresh observed quotes. Acceptance completion is reported separately from natural rebalancing.

TODO: Agree and connect the mailbox location and consumer hook with PR #239, including its backup boundary. Keep status `AWAITING_SHARED_ACCOUNT_EXECUTION_INTERFACE` until the actual consumer is deployed. The signal process must never become a competing account writer. The current stock Grid campaign and remaining cumulative POST budget cannot be silently treated as a fresh QQQ/SPY authorization.

TODO: Attach configured strategy histories, indicator evaluation and target checkpoints through the existing isolated strategy machinery. No V69 weights are generated here; original V69 execution belongs to Alpaca Paper.

TODO: Once the shared executor exposes strategy ownership and fill/funding attribution, extend the existing daily accounting with target/actual weights, cash/margin, nominal/economic exposure, stock/contract basis, fees/funding, realized/unrealized P&L, observed slippage, strategy drawdown, and separate stock-reference versus contract returns. Account-wide NAV alone is not strategy performance.

TODO: Validate minimum-size official Demo orders through the sole executor, reconciliation, automatic campaign continuation within existing authorization, and restart idempotency without disturbing the running strategy. Observer startup or public quotes do not satisfy order acceptance.
