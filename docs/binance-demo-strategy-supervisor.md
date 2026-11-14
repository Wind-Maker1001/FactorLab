# Binance Demo continuous DualMA strategy

NO LIVE MONEY. This worker accepts only the Binance USD-M Demo environment and NVDAUSDT. It reuses the existing DualMA algorithm, Demo account journals, persistent authorization guard, and LIMIT execution path.

## Start and control

Use Python 3.13 or 3.14 with FactorLab installed. Existing `factorlab-demo setup` must have bound the current user's saved Demo credential and account. The worker does not create or renew authorization.

Minimal configuration is the command line; the default is **dry-run**:

```powershell
python -m factorlab.integrations.binance_futures_trading.demo_strategy_supervisor start --interval 1m --poll-seconds 2 --max-backfill 120
```

For a bounded observation add `--seconds 150`. A new deployment warms up from 21 confirmed bars; unfinished bars are never delivered. The Demo source and selected interval are explicitly passed to DualMA. No signal is a normal running state.

In another terminal:

```powershell
python -m factorlab.integrations.binance_futures_trading.demo_strategy_supervisor status
python -m factorlab.integrations.binance_futures_trading.demo_strategy_supervisor pause
python -m factorlab.integrations.binance_futures_trading.demo_strategy_supervisor resume
python -m factorlab.integrations.binance_futures_trading.demo_strategy_supervisor stop
```

Control output distinguishes `desired` from the last `observed` worker report. A command requests a transition; the next worker control check applies it. A request made during an already-dispatched HTTP operation does not recall that operation. Existing account-wide `factorlab-demo stop` separately revokes the persistent write capability at its dispatch guard.

Pause continues collecting bars and signals without deciding orders. Resume reconciles using GET and discards previously pending signals. Stop does not cancel existing orders or liquidate positions. Ctrl+C or the bounded duration ends the worker without deleting its checkpoint; start reopens its journals and reconciles. After an explicit stop, start explicitly reactivates the checkpoint and only new signals can execute.

## Execution mode

`start --execute` explicitly selects execution mode and requires a current, non-smoke persistent authorization created separately by `factorlab-demo enable-demo`. Add `--execute` to control/status commands for that mode. Never use execution mode for a GET-only validation run.

The worker binds the campaign present at startup. Expiry, revocation, exhausted POST budget, uncertain writes, or replacement of that campaign blocks further submissions. Renewal requires explicitly restarting the worker; it never automatically adopts new authority. Explicit startup with a new campaign archives the old strategy configuration and retains bars, decided signal IDs, account and order records; prior signals are not resubmitted under a new prefix.

Orders remain LIMIT GTC. SELL is reduce-only. Sizing uses reconciled equity, available funds, fees, exchange step size, current position and the current executable LIMIT price, capped by both 7 USDT and 0.04 units. It never rounds up to meet minimum notional. The 10% strategy allocation is an upper bound, not an instruction to exceed the Demo budget.

The decision is persisted before a submission attempt. A crash between those steps may leave a decided but unsubmitted order; restart does not retry it. UNKNOWN orders require GET reconciliation rather than another POST. Open orders are periodically reconciled before later decisions.

## State and dry-run isolation

State lives under `%LOCALAPPDATA%\FactorLab\BinanceDemo`. `strategy-dry-run.sqlite3` and `strategy-execute.sqlite3` contain feed cursors and a small worker-control record. Existing execution account journals remain shared with the Demo CLI. Dry-run uses separate existing-format journals under `strategy-preview`, obtains no campaign capability, and records preview targets without invoking submission or cancellation. An already-active authorization cannot turn dry-run into execution.

Status reports mode, retained history, feed watermarks, strategy signal/target, observed account state, blocked reason and request counts. Counts include market and account GETs for the current process. POST and DELETE must remain zero during a real-network dry-run. Do not delete state to recover an uncertain order.

A market interruption pauses decisions. Recovery first reconciles using GET, then resumes contiguous closed-bar delivery. Gaps beyond `--max-backfill` remain paused rather than skipping data; resolve the missing history before proceeding.

## Verification categories

Offline integration tests inject exchange responses at the network boundary and forbid real connectors. Their POST/DELETE counts describe **simulated** exchange calls, including partial fills, rejection, UNKNOWN, restart and duplicate delivery.

A real Demo dry-run measures actual GET market/account integration, confirmed bars and persisted restart watermarks. It does not demonstrate a real exchange order or fill; a naturally occurring signal is previewed only. Never run smoke tests or manufacture an order to validate this worker.

### Offline integration results

The combined targeted feed, strategy, strategy-session, runtime and network suite passed 152 tests; the Windows Credential Manager mutation test was excluded. An additional startup/stop race test passed separately. The suite includes an actual child-process abrupt exit before feed acknowledgement followed by reopening and replay, plus simulated accepted/rejected/UNKNOWN submissions, partial fills, duplicate delivery, budget constraints, preview isolation, and slow account-GET freshness checks.

### Real GET observation, 2026-11-14

A 150-second bounded run delivered 21 warmup bars and two subsequent complete 1m bars. The closed-bar watermark advanced from 2026-11-14 14:26 UTC through 14:28 UTC. It observed a natural SELL signal while the account was flat; no order was manufactured. Request counts were **GET=141, POST=0, DELETE=0**. An initial reconciliation interruption paused the worker before it recovered and delivered bars.

A separate 90-second restart reopened the same checkpoints, caught up three bars from the prior watermark, and reached 14:31 UTC without repeating warmup. It produced two RUNNING reports with a normal HOLD/NO_SIGNAL result and **GET=100, POST=0, DELETE=0**.

That run exposed trigger aging during slow account GET refresh. The runtime now checks the triggering Quote at receipt and the freshly prepared executable book at decision time. Offline tests cover both a fresh book after a delayed refresh and a stale book that must remain blocked.
