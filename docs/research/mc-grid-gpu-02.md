# MC-GRID-GPU-02

## Economic contract

The research kernel remains independent of Paper execution. CPU and CUDA execute the same per-path state transition source. Paper ledger transactions and the actual GridExecution subclasses provide the independent economic reference.

Funding is signed wallet income: `-base_quantity * settlement_mark * signed_rate`, rounded per settlement to 0.00000001 USDT using ties-to-even. Historical settlement marks are not replaced with bar close or entry prices. Events in `(previous_close, current_close]` settle against pre-fill inventory, before current observation and matching. Missing scheduled rates are not zero rates.

The account uses the existing USD-M full entry-notional collateral and order reservations, not an exchange maintenance-margin or liquidation model. Funding states outside the nonnegative available/equity domain accepted by GridAccount are rejected, not silently repaired or included in population summaries.

Profiles are distinct: ordinary arithmetic/geometric; observed-OHLC ATR; hourly C volatility adaptation with frozen reference and warmup; hourly B settled-funding entry/exit rules. ATR uses a rolling simple mean of true ranges, not Wilder smoothing. OHLC determines the statistic, not intrabar fills. C uses sample standard deviation of log returns, extreme-volatility buy gating and the existing one-level flat-only rebuild construction. Profiles are not implicitly composable.

A rebuild requests cancellation, retains reservations until confirmation and suppresses reverse proposals. ATR close processing occurs after matching and existing cancellation confirmations; cancellation requests created by that close remain pending into the next observation. Inventory, average entry and wallet remain continuous through rebuilding.

Trading realized gross PnL excludes funding; funding, actual fees and embedded execution-price slippage are separately reported. Slippage attribution must not be subtracted twice.

The Paper oracle enables existing continuous capacity in both GridExecution and PaperStore: cumulative proposal history and journal history are not active-order limits. This removes only operational lifetime budgets; account capital, inventory, reservations, proposal admission, controller submission-rate limits and the native 128-slot working-order capacity remain unchanged.

## Implementation and evidence

The shared transition source contains event-time funding, rebuild/ATR state and separate B/C policy branches, with bounded history workspace and optional event audit output. The complete 64-test CPU suite passed on Windows Python 3.14, including funding-sensitive spike, age and margin exits. Stepwise accounts, fills and canonical order-event receipts match Paper at the original tolerances. RTX2070 acceptance passed all 45 explicit CUDA tests. The 1k/10k/100k/1m-path, 168-hour CPU/CUDA pilot completed for static, ATR, C and B profiles, including selected Paper event audits. Timing, resource and financial evidence is in [the validation report](../validation/mc-grid-gpu-02/README.md). Debian Linux under WSL with Python 3.13.18 also passed all 64 CPU/Paper tests.

Observation timestamps are UTC completed hourly bar closes for B/C and funding runs. Settlement timestamps retain their actual UTC instant, including whole-hour events inside an observation interval. Pre-anchor events are signal history only; an event exactly at the endowed anchor is explicitly rejected because the pre-endowment holding convention is unspecified. Expected event timestamps are required to distinguish zero rates from missing observations.

Budget sizing, expiry reseeding, minute execution, more than 4096 increments, exchange liquidation, ATR+C, B+C and multilevel C remain unsupported. Existing static API and financial columns retain their meaning.
