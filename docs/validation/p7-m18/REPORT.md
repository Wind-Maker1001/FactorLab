# P7-M18 — Continuous Binance Demo campaign lifecycle

**NO LIVE MONEY.** Base: `bc9000d42735f917fcb0559648118af9a363cf93`.

## Execution policy

Continuous permission is independent of the rolling Campaign. The frozen cumulative allowance is **100 POST attempts / 700 USDT submitted limit notional**, including historical non-smoke dispatch reservations. The latest historical non-smoke equity baseline is retained. Canceled, rejected and unknown submissions remain charged; DELETE does not replenish the allowance. Windows allow at most four POST attempts over 10,800 seconds. Renewal preserves the authorization, original deployment, order identities, inventory, fees and global counters.

Admission remains NVDAUSDT, maximum 0.04 quantity, 7 USDT per order/exposure, one active order, LIMIT GTC, one-way, BUY entry and reduce-only SELL. Global loss protection is 1 USDT. There is no leverage-increase operation. Historical successful POST/DELETE receipts are capability evidence, not a substitute for the current exchange response.

`start-continuous` records permission once after GET reconciliation. Repeating it cannot reset consumption or clear STOP. The worker automatically selects/creates a window after recovery. UNKNOWN must be resolved through GET; an expired window does not authorize resubmission. Known orders from historical account-owned windows can be canceled through their original dispatch ownership.

## Reproduction

From this checkout, with its dependencies installed and `src` on `PYTHONPATH`:

```console
python -m factorlab.integrations.binance_futures_trading.demo_cli start-continuous --total-orders 100 --total-notional 700 --window-seconds 10800 --window-orders 4
python -m factorlab.integrations.binance_futures_trading.demo_cli grid-supervisor start --execute
python -m factorlab.integrations.binance_futures_trading.demo_cli grid-supervisor status --execute
python -m factorlab.integrations.binance_futures_trading.demo_cli recover
python -m factorlab.integrations.binance_futures_trading.demo_cli stop
```

Worker pause/stop and duration expiry do not cancel resting orders or flatten inventory. Account-wide STOP is lock-independent and prevents any later automatic window creation or admission, including with an already minted capability.

## Persistence, migration and rollback

The existing `runtime.sqlite3` schema is retained. First continuous initialization appends version `continuous_schema=1`, a frozen account/policy/budget record, and later immutable window rows. Historical Campaign payloads, dispatches, protocol/controller journals and deployment events are not rewritten. The legacy `active` key is cleared; continuous windows use `continuous_window`. Consumption derives from historical non-smoke POST reservations for the bound account, including reserved responses not yet confirmed.

Existing legacy STOP records do not distinguish technical receipt failures from operator stops. Initialization is an explicit command; ordinary restart/recovery never interprets an old disabled record as RUNNING. Once continuous permission exists, STOP is durable and cannot be cleared by the initialization command, worker start, or resume.

Before rollback:

```console
python -m factorlab.integrations.binance_futures_trading.demo_cli stop
python -m factorlab.integrations.binance_futures_trading.demo_cli recover
```

Then stop the worker and deploy the prior code revision. Keep all SQLite files and continuous records. The legacy `active` key remains empty, so older code cannot restore a continuous window's writes. Use GET recovery only on the rolled-back version; do not restore a historical database backup over current orders, fills or consumed allowances.

## Actual Demo observation

At **2026-11-16 15:08:18 UTC**, `start-continuous` completed successfully against the existing official Demo account: **38 GET, 0 POST, 0 DELETE**. Account eligibility was true, position mode ONE_WAY, wallet/equity/available balance **4999.97114760 USDT**, with zero positions, open orders or UNKNOWN identities. Historical fills, actual commissions and three canceled exchange orders remained in the original journals.

A new execution window was created automatically, expiring at **2026-11-16 18:08:20.524841 UTC**. Its window consumption was zero; account-wide imported consumption was **4 POST / 25.5692000 USDT submitted notional**. The retained equity baseline was **4999.97114760 USDT**, loss zero and exposure zero. Historical successful write evidence was present; no permission-probe order was sent.

An independent-process `recover` then succeeded with **38 GET, 0 POST, 0 DELETE**, retaining the same window expiry, baseline, cumulative consumption, wallet, historical fills/fees and zero inventory/open/UNKNOWN state. Its result was `DEMO_RECONCILED`; continuous permission remained RUNNING.

Raw account output is retained privately in `local-p7-m18/start-continuous-private.json` and `local-p7-m18/restart-recovery-private.json`, excluded from version control.

## Focused validation

Tests use intercepted HTTP and isolated temporary account stores. The lifecycle/Grid/supervisor/real-fill-fee/M14 reconciliation group passed **83 tests in 139.01 seconds**. The final lifecycle/network/execution/runtime boundary group passed **109 tests in 36.68 seconds**. These groups overlap; their counts are not a distinct-test total.

Initial regression run: **56 passed, 6 failed**. All six failures occurred in new lifecycle tests that attempted Grid decisions before calling the bridge's existing required `recover()`; the tests were corrected to exercise that contract. A later run passed 35 tests and hit one pre-existing synthetic-clock issue: fixed exchange timestamps became stale as the wall clock advanced during the multi-fill fee test. The shared intercepted-HTTP fixture now uses its fixed exchange NOW for the session clock, without changing production freshness checks; the subsequent complete group passed. The first attempted CPython 3.13 invocation stopped before collection because that local interpreter lacks pytest; executed runs use the existing CPython 3.12.7 test environment.

The follow-up feed/Grid/lifecycle group passed **73 tests in 50.32 seconds**, including restart between historical catch-up pages, exact consecutive bar delivery and suppression of quotes while behind.

Coverage includes missing-window startup, expiry and POST-window exhaustion, unchanged global loss/count/notional budgets, historical consumption migration, scoped old-window cancellation, partial fills across windows, reserved-ID replay refusal, UNKNOWN GET-only recovery, restart, two independent concurrent processes selecting the same unique window, STOP blocking an already minted capability and restart, production credential rejection, automatic clock resynchronization after CLOCK_SKEW, and unchanged authoritative fee/FIFO/equity posting.

Targeted commands:

```console
python -m pytest tests/integrations/test_demo_continuous_campaign.py tests/integrations/test_binance_demo_runtime.py tests/integrations/test_demo_grid_supervisor.py tests/integrations/test_binance_demo_grid_network.py -q
python -m pytest tests/integrations/test_binance_demo_fill_closed_loop.py tests/integrations/test_binance_demo_fill_faults.py tests/integrations/test_demo_reconciliation_m14.py -q
```

The real worker produced three reports and reached **126 GET, 0 POST, 0 DELETE** before termination. A recovery-phase CLOCK_SKEW on `/fapi/v2/account` was followed by time synchronization and successful GET reconciliation. The last account observation at **2026-11-16 15:21:29 UTC** retained wallet **4999.97114760 USDT**, zero positions/open orders/UNKNOWN and the unchanged historical consumption. The worker then exposed `BACKFILL_LIMIT`: its retained market cursor was older than the 120-bar per-poll bound. Continuous Grid catch-up now replays consecutive bounded pages, persists each acknowledged bar, and delivers no quote while behind; it does not skip history or turn old bars into live orders.

Account-wide STOP returned `DEMO_STOPPED`, `enabled=false`, durable authorization state `STOPPED`, no active window and unchanged **4 POST / 25.5692000 USDT** cumulative consumption. The worker control was also set STOPPED and its background process terminated. No exchange cancellation or liquidation was necessary in the last reconciled flat account state.

The observation ended before the original three-hour window expired. Natural-expiry renewal is therefore not claimed as a real-exchange observation; expiry, budget-window rotation, concurrent creation and STOP/restart behavior are established by the intercepted-HTTP tests. Actual evidence establishes automatic missing-window creation, independent-process recovery, a transient clock-error recovery, preserved account/budget state and persistent STOP.

TODO: Cover constructor-phase CLOCK_SKEW with an explicit bounded startup-recovery contract; the existing recovery handler only resynchronizes an already constructed session. Preserve the failed startup diagnostic rather than enlarging recvWindow or retrying writes.
