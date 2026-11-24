# Binance TradFi cloud observation and scheduling

The existing `factorlab-demo-execute.service` remains the sole account order owner. The TradFi observer has no credentials, reads the existing account health projection, and performs only public GET requests against the fixed official USD-M Demo host. It does not initialize an account, submit orders, change campaigns, or assign ownership of existing positions.

`factorlab-demo-tradfi-observer.service` maintains private SQLite observations, a current health snapshot, and Eastern-date daily JSON reports in `/var/lib/factorlab-tradfi-observer`. Lifecycle counters survive restarts. Account financials are suppressed when the Execute heartbeat or account evidence expires. Returns and costs without strategy attribution remain null, not zero.

Weekly scheduling uses the existing `cross_market.calendar.us_calendar` (XNYS via `exchange-calendars`), including holidays, DST and early closes. `WeeklySchedule` returns the first actual session of each civil week and the previous completed session as the history cutoff. Quotes must be observed after the scheduled open, before the actual close, and not in the future. Completed sessions are skipped; a missed closed session is not replayed as a historical trade.

## Installation

Install a fixed reviewed revision with the `cross-market` extra in a separate root-owned virtual environment under `/opt/factorlab/tradfi-observer/releases/<commit>/venv`. The service uses `/opt/factorlab/tradfi-observer/current`; retain previous releases for code rollback without replacing observation state.

Provision `/var/lib/factorlab-tradfi-observer` as `factorlab-demo`, mode 0700. Install the supplied systemd unit and enable it. No changes to the original Execute unit, credentials, trading controls or deployment timer are required. The sandbox makes the original account state read-only and hides `/etc/factorlab-demo`.

The shipped unit observes QQQUSDT. Additional instruments must be verified against current official Demo exchange metadata before configuring them; missing stock instruments are not replaced with unrelated contracts.

## Execution integration

TODO: Connect strategy target publication and acknowledgement to the operational shared-account executor from PR #239. Its current `plan_targets` foundation is not an order-consumption interface. Keep the observer status `AWAITING_SHARED_ACCOUNT_EXECUTION_INTERFACE` until that interface is deployed. The independent observer must never be promoted into a competing account writer.

TODO: Attach configured strategy histories, indicator evaluation and target checkpoints through the existing isolated strategy machinery. No V69 weights are generated here; original V69 execution belongs to Alpaca Paper.

TODO: Once the shared executor exposes strategy ownership and fill/funding attribution, extend the existing daily accounting with target/actual weights, cash/margin, nominal/economic exposure, stock/contract basis, fees/funding, realized/unrealized P&L, observed slippage, strategy drawdown, and separate stock-reference versus contract returns. Account-wide NAV alone is not strategy performance.

TODO: Validate minimum-size official Demo orders through the sole executor, reconciliation, automatic campaign continuation within existing authorization, and restart idempotency without disturbing the running strategy. Observer startup or public quotes do not satisfy order acceptance.
