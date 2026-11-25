# RELEASE-01 — dual simulated-account release

## Deployment identity

Frozen integration baseline: `bbda212299ccb1b6c2b4dcb021662397d19923d0`.
Both Azure runtimes were deployed at `ea9cbbdd7519e5400df097e5661b0d6c47b6c4da` on 2026-11-25.
Subsequent archival documentation does not change that deployed revision.
At 2026-11-25 11:52 UTC, all 480 tracked source files in each deployed tree matched that commit byte-for-byte. Alpaca also contained six installation-generated egg-info metadata files, which triggered the initial strict extra-file rejection but did not change tracked source. Service executable/current-release paths selected the same revision. Binance imports the matched source tree directly. Alpaca imports its installed package: all 474 package files matched the verified release source byte-for-byte; six source-only `.gitattributes` files are not installed. Details are in `source-consistency.json`.

This patch addresses two observed integration defects: Alpaca Paper rejected the SSP activity query filter with HTTP 422; the Demo continuous budget omitted historical smoke and portfolio dispatches. Corporate activities now use complete pagination before local receipt filtering. Demo consumption includes account-wide POST history, and portfolio POST admission preserves the continuous authorization's exit reservations.

## Integration and targeted regression evidence

Python 3.13 baseline regression at the frozen revision:

| Group | Result | Duration |
|---|---:|---:|
| Unified strategy/Champion, budget migration, Grid supervisor, Alpaca execution/corporate actions, execution performance, lifecycle/research and v69 bridge | 157 passed | 464.35 s |
| History recovery, hot-switch cancellation, strategy/continuous budgets, funding, breakout exit, Alpaca deployment/review/portfolio inputs | 153 passed | 786.62 s |
| Total baseline | **310 passed** | |

Patch regression:

| Group | Result | Duration |
|---|---:|---:|
| Alpaca corporate activities and cloud execution | 18 passed | 9.36 s |
| Demo migration, continuous campaign, strategy budget and portfolio consumer | 61 passed | 72.21 s |
| Final portfolio admission guard and migration tests | 25 passed | 15.88 s |

These patch groups overlap and are not additive. The 61-test run began before the final portfolio guard edit; the final 25-test run covers that guard. Preserved console evidence is in `regression-evidence.json`. Existing research evidence in `../p8-r11/REPORT.md` and `../p8-s02-f01/REPORT.md` remains the basis for retaining the incumbent; no Challenger was promoted.

## Binance Demo — 2026-11-25 11:21 UTC

Recovery completed with zero UNKNOWN orders and no positions. The account retained its identity, dispatch history, persistent controls and original 5,000 USDT equity baseline.

| Limit | Authorized | Consumed | Remaining |
|---|---:|---:|---:|
| Cumulative POST | 100,000 | 116 | 99,884 |
| Submitted notional, USDT | 500,000 | 728.9148 | 499,271.0852 |
| Cumulative loss, USDT | 250 | 0.67674313 | 249.32325687 |

Wallet and margin balance were 4,999.32325687 USDT, with zero unrealized P&L. Existing Grid allocation remained 7 USDT, maximum quantity 0.04; existing portfolio and shared-account allocation limits remained 20 and 27 USDT. Active strategy remained `legacy-grid`. The unified qualified-strategy consumer and hot-switch service remained enabled without promoting an unqualified Challenger.

The preserved natural NVDAUSDT BUY order was 0.04 at 146.09, exchange order `195680090`, submitted at 2026-11-25 10:24:03.247 UTC. Recovery reported NEW with zero filled quantity. It predates the final patch deployment and is not a new fill. The retained historical Grid reduction fill was SELL 0.04 at 151.87, exchange order `195421988`, at 2026-11-24 15:50:00.439 UTC.

The controller was RUNNING; its effective Grid state waited for a fresh closed bar (`FRESH_CLOSED_BAR_REQUIRED`). Market batch age was approximately 2.07 seconds. Post-backup health reported ready, execute healthy, market fresh and zero UNKNOWN.

## Alpaca Paper — 2026-11-25 11:21 UTC

The original account and persistent state were retained. Account recovery completed, the runtime was healthy in EXECUTE mode, and trade/market streams were CONNECTED. The original eight-symbol `v69-original` strategy was enabled with natural-week scheduling. Execution was IDLE without a task; strategy state was WAITING_NEXT_WEEK.

The week's execution session was 2026-11-23 and signal cutoff 2026-11-20. The schedule was not due. At the snapshot the market was closed; IEX was available with no missing symbols, while fresh quotes were false. No catch-up acceptance order was manufactured.

Execution-day corporate-action and adjustment-basis handling remained enabled. Strategy sizing uses cash rather than margin buying power. The complete-page receipt fix removed the observed SSP filter failure; current-service restart count was zero after the fix.

## Backup and unattended operation

The pre-deployment Demo backup `backup-20261125T101219Z` succeeded with verification and an isolated restore drill, covering 14 databases including the external task mailbox. Credentials were excluded. Existing 14-day retention and scheduled backup remained in place.

Post-patch Demo backup `backup-20261125T112048Z` succeeded and verified. Its maintenance downtime was 6.046 seconds; restart and reconciliation succeeded, preserving one open order, zero positions and zero UNKNOWN. The backup runtime was aligned to the deployed patch. Structured results are retained in `deployment-snapshot.json`.

Alpaca's complete persistent-state copy was taken with the service stopped before deployment: `release-01-pre-bbda212-20261125` (169 MiB).

Both runtimes continue under the existing Azure infrastructure, separate service accounts, systemd restart/startup controls, monitoring and backup arrangements. Archival validation does not redeploy services, migrate budgets again, repeat official fill acceptance, disturb natural orders or repeat restore drills.
