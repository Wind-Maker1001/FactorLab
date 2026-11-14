# Bounded official Binance Demo observation — 2026-11-14

NO LIVE MONEY.

Integrated baseline: `34106b4` (PR #155 merged after the original-limit Grid wiring in PR #154). Both entry points import together in a Python 3.14 editable installation.

## Actual outcomes

- REALTIME_FEED_ACCEPTED: `dual-ma-online.json` records 21 warmup bars, 10 subsequent confirmed 1m bars and 12 Quote deliveries. Closed-bar watermarks advance from 15:29 through 15:39 UTC.
- STRATEGY_SIGNAL_ACCEPTED: retained history remains 21 bars; recorded DualMA evaluations are HOLD/NO_SIGNAL. Reconciliation interruptions pause decisions; subsequent reports recover and deliver contiguous catch-up bars.
- STRATEGY_DEMO_ORDER_ACCEPTED: not reached. No natural executable signal produced an order.
- STRATEGY_DEMO_FILL_RECONCILED: not reached. No new strategy fill occurred.
- GRID_DEMO_ORDER_ACCEPTED: not reached. The acceptance driver supplied a timezone-aware timestamp to Grid's timezone-free observation interface and failed before dispatch.
- GRID_DEMO_RECOVERY_ACCEPTED: not reached. No Grid exchange order existed to recover.

The execution supervisor used 636 GET, zero POST and zero DELETE. These are real-network counts, not intercepted test requests. No deterministic acceptance strategy or fabricated market event was used.

## Campaign and final recovery

The explicitly enabled persistent campaign had a 900-second lifetime, expiry `2026-11-14T15:43:09.569143+00:00`, two POST attempts, 1 USDT loss threshold, NVDAUSDT only, 0.04 maximum quantity, 7 USDT maximum notional and one active order. Independent recovery after DualMA showed zero consumed POST attempts.

The Grid attempt observed bid 160.41000 / ask 161.37000 at 15:40:55.567 UTC and constructed fixed limits 159.75 / 162.99 for quantity 0.04. Timestamp validation failed before any submission. The driver now converts UTC to the timezone-free representation required by Grid; that correction has not been exercised in another authorized online campaign.

`demo_cli stop` returned DEMO_STOPPED. A separate GET-only recovery observed at `2026-11-14T15:42:20.086532+00:00` returned DEMO_RECONCILED, zero positions, zero open orders and zero UNKNOWN. Wallet, available and margin balances were each 4999.97114760 USDT; unrealized PnL was zero. That recovery used 46 GET, zero POST and zero DELETE. Campaign authorization was disabled.

This campaign generated no orders, trades, fees or realized PnL. Historical manual orders restored by account reconciliation are excluded from this campaign's results.

## Reproduce the existing CLI

From the installed checkout, default read-only operation:

```powershell
.venv/Scripts/python.exe -m factorlab.integrations.binance_futures_trading.demo_strategy_supervisor start --seconds 150
```

Only after a separately explicit bounded campaign has been enabled:

```powershell
.venv/Scripts/python.exe -m factorlab.integrations.binance_futures_trading.demo_strategy_supervisor start --execute --interval 1m --poll-seconds 2 --max-backfill 120 --seconds 600
```

Stop new account writes and recover without submitting replacements:

```powershell
.venv/Scripts/python.exe -m factorlab.integrations.binance_futures_trading.demo_cli stop
.venv/Scripts/python.exe -m factorlab.integrations.binance_futures_trading.demo_cli recover
```

## Remaining work

- TODO: Observe a legal natural-signal order and fill, then independently recover its actual trade and fee records.
- TODO: Validate original-limit Grid acceptance, known-order cancellation and cross-process Grid replay under a separately enabled campaign. The acceptance driver currently generates configuration from the current book and is not a restart driver; persist and reuse the exact deployment configuration before attempting that replay.
- TODO: Exercise the acceptance driver's UTC timestamp conversion in a focused offline test before another online attempt.
- Real online POST-budget exhaustion and UNKNOWN recovery were not exercised in this campaign.
- Long-duration unattended operation is not established by this bounded observation.
