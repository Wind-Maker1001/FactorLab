# P7-M12A: Binance Demo continuous Grid soak

**NO LIVE MONEY.** The repaired supervisor completed its 300-second and 5100-second observation bounds, retained one real unfilled order across restart, and recovered from transient feed/account failures without a reporting crash. No natural fill occurred. Sanitized process observations are retained in `observations.json`.

Baseline: `33f03b4` (merged M11). Campaign `demo-31a02a398c7147318f8956791936a8fe` began at 2026-11-15 09:04:30.126792 UTC with a three-hour expiry, four POST attempts, maximum quantity/long inventory 0.04 NVDA, 7 USDT notional, 1 USDT loss threshold and one active order. It was never renewed. Deployment: `fl-d7345f8795464a65-g`; immutable range: 159.47–162.71 USDT.

## Duration, bars and controls

The repaired short worker was launched at approximately 09:19:30 UTC with a 300-second bound and exited normally after the 09:25 UTC bar. The restarted worker ran with a 5100-second bound and exited normally after the 10:50 UTC bar; its final account observation was 10:50:34.727350 UTC. Thus configured observation duration totals 90 minutes; the repaired launch-to-last-account interval is approximately 91 minutes, including initialization, restart and in-flight request overhead. The implementation checks duration between polls rather than interrupting HTTP requests at the deadline.

There are 91 distinct reported closed 1m bars, 09:20–10:50 UTC inclusive: six in the repaired short worker and 85 in the restarted long worker. The 09:21 bar committed before a poll error; summing successful-poll `bars` fields alone would undercount it. Every minute in that interval appears in the retained observations.

Pause was requested around 09:22 UTC. The resume command around 09:23 UTC returned observed effective PAUSED/reason PAUSED, bar progress from 09:21 to 09:22 UTC, and GET evidence at 09:22:59.048294 UTC. Requests then were GET 232 / POST 0 / DELETE 0, with no positions, open orders or UNKNOWN. After resume, effective RUNNING at the 09:24 UTC bar produced one POST. A separate process then restored the same deployment, advanced from 09:26 through 10:50 UTC, retained the same exchange order and sent no POST. Both repaired processes exited normally. After duration expiry, explicit supervisor stop set desired STOPPED before cancellation; process exit itself did not cancel the order.

## Real order and financial outcome

| Field | Exchange/GET evidence |
| --- | --- |
| clientOrderId | `fl-d7345f8795464a65-g-00000003` |
| orderId | `192147238` |
| Trigger bar | 2026-11-15 09:24 UTC |
| Exchange update at submission | 2026-11-15 09:24:20.411 UTC |
| Side / quantity / limit | BUY / 0.04 NVDA / 159.47000 USDT |
| Notional | 6.3788 USDT |
| Independent subsequent worker GET | NEW, same identity, zero cumulative fill |
| Actual fill / fee | 0 NVDA / 0 USDT |
| SELL | None |
| Cancellation | DELETE HTTP 200; CANCELED at 10:51:39.399 UTC |
| Final independent GET | Same identity CANCELED, zero fill, no reconciliation required |

No quote crossing or bar extreme was counted as a fill. The order was neither repriced nor replaced. The campaign-window GET `/fapi/v1/income` with incomeType FUNDING_FEE returned an empty list for NVDAUSDT from 09:04:30 UTC through approximately 10:53:27 UTC: actual funding entries and funding income are zero.

## Actual exceptions and recovery

The initial 300-second worker initialized the new deployment but inherited the fixed `grid-execute` cursor from M11, whose last bar was 2026-11-14 19:04 UTC. It remained BACKFILL_LIMIT / RECONCILE_REQUIRED, delivered zero bars and exited normally. Its final account evidence was 09:11:07.317377 UTC. Initial pause returned a stale M11 snapshot and is not counted as current-run pause evidence.

The minimal repair scopes the feed consumer identity to the Grid deployment ID. Old cursor history is retained; a new deployment starts its own feed history, and same-deployment restarts reuse it. No backfill bound was enlarged and no historical database was deleted. All 22 Supervisor regressions passed in 408.17 seconds, including stale legacy cursor isolation, same-deployment restart continuity and composed-session unavailable-account status reporting. Existing installations using a legacy unscoped cursor receive a deployment-scoped startup cursor; Grid event identity continues to deduplicate already committed bars.

The repaired short worker encountered DataQualityError after the 09:21 bar. Status emitted inventory null and continued rather than raising the M11 reporting exception. Valid account evidence returned at 09:21:43.462350 UTC, before later resumed execution submitted the real order. Exact failure onset is not timestamped in the report.

The long worker had four reported interruptions, each followed by recovered account/feed evidence and no duplicate POST:

| Reported error | Evidence timeline (UTC) | Recovery observation |
| --- | --- | --- |
| ExchangeError near 09:32 | Last valid evidence 09:32:12.486473 → next valid 09:33:01.068186 | 48.582 s between valid observations |
| STALE_POLL | Error report account evidence 09:43:07.809071 → next valid 09:43:46.480800 | 38.672 s between those observations |
| DataQualityError near 10:14 | Last valid 10:14:25.658266 → next valid 10:15:16.943569 | 51.285 s valid-evidence gap |
| DataQualityError near 10:44 | Last valid 10:44:37.712508 → next valid 10:45:48.910289 | 71.198 s valid-evidence gap |

These are observation intervals, not exact request-failure durations. Recovery first closed the submission gate pending a fresh closed bar. Unavailable account evidence remained null rather than being reported as flat. The worker did not crash, including during exit reporting. ExchangeError reports retain the classification, not a specific exchange code; no narrower cause is assigned.

A separate funding-query helper was mistakenly run concurrently with final journal reconciliation and failed with sqlite3 `database is locked` during controller construction. The serialized GET-only query succeeded after reconciliation released the journals. This was an evidence-collection concurrency error, not a supervisor failure; no retry loop or authorization change was used. The failed helper did not print its GET counter, so its requests are not included in the exact recorded subtotal below.

## HTTP accounting

| Operation | GET | POST | DELETE |
| --- | ---: | ---: | ---: |
| Initial preflight | 50 | 0 | 0 |
| Campaign creation preflight | 50 | 0 | 0 |
| Initial blocked worker | 343 | 0 | 0 |
| Repaired short worker | 377 | 1 | 0 |
| Restarted long worker | 6459 | 0 | 0 |
| Known-order cancellation and reconciliation | 102 | 0 | 1 |
| Authorization stop | 0 | 0 | 0 |
| Independent final GET recovery | 52 | 0 | 0 |
| Successful funding helper, including session setup | 11 | 0 | 0 |
| **Recorded subtotal** | **7444** | **1** | **1** |

POST/DELETE totals are complete. Total GET is 7444 plus the unreported setup GETs from the failed concurrent helper; an exact all-process GET total is not available. Historical smoke receipts displayed by the CLI are not requests from this campaign. An initial dependency-only invocation failed before any HTTP request.

## Final account and unresolved items

Cancellation completed under the original active authorization. `demo_cli stop` then returned DEMO_STOPPED with enabled=false. A fresh independent recovery process at **2026-11-15 10:53:00.168247 UTC** confirmed authorization disabled, no positions, no open orders, no UNKNOWN, zero initial margin and order `192147238` CANCELED. No residual inventory requires liquidation. Funding evidence was collected afterward with no write capability.

The cursor defect is repaired and tested. No natural fill occurred, so this run does not establish a new fill/reversal lifecycle. Exact failed-helper GET accounting and exact failure-onset durations remain unavailable as described above. No automatic campaign renewal or production operation occurred.
