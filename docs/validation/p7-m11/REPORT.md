# P7-M11: continuous Binance Demo Grid online execution

Supervisor submitted a real NVDAUSDT BUY LIMIT GTC from a newly closed 1m bar, not from the manual submit CLI. This validates execution behavior, not profitability.

The campaign `demo-c5b931026bf948fbaa6f55110c465af0` started at 2026-11-14 18:48:52.136355 UTC with a 3600-second lifetime, two POST attempts, a 1 USDT loss threshold, 0.04 NVDA maximum position, 7 USDT maximum notional and one active order. The deployment is `fl-b77ee78873704451-g`; its unchanged range is 158.83–162.05 USDT.

## Confirmed automatic order

| Field | Exchange evidence |
| --- | --- |
| Closed-bar trigger | 2026-11-14 18:52:00 UTC |
| Exchange acknowledgment | 2026-11-14 18:52:20.815 UTC |
| clientOrderId | `fl-b77ee78873704451-g-00000001` |
| orderId | `191960373` |
| Side / type / time in force | BUY / LIMIT / GTC |
| Quantity | 0.04 NVDA |
| Original / accepted limit | 158.83 / 158.83000 USDT |
| POST receipt | HTTP 200, NEW |
| Subsequent independent GET | Same identity, quantity and price, NEW |
| Initial order margin | 6.35320000 USDT |
| Fill / actual fee at recovery | 0 NVDA / 0 USDT |

The initial process consumed GET 184 / POST 1 / DELETE 0 and exited normally. Independent Grid recovery consumed GET 51 / POST 0 / DELETE 0 and confirmed persistent campaign consumption of one POST out of two.

The restarted process observed PAUSED while advancing closed-bar watermarks from 18:54 through 18:56 UTC. It consumed GET 186 / POST 0 / DELETE 0, preserved the same active exchange order and exited normally. The resume request was not observed by this process before exit and is not counted as resumed execution.

A subsequent GET encountered exchange CLOCK_SKEW (-1021). A fresh GET-only session using existing time synchronization recovered the account and same NEW order, with no UNKNOWN or positions and no additional POST. No uncertain write was repeated and recvWindow was not increased.

A final resumed process exposed a reporting defect: after account evidence became unavailable, status collection called the trading account interface and raised `Demo account evidence required`, including during exit reporting. Status now uses the existing session report and represents unavailable inventory as null rather than claiming a flat account. A composed-session regression covers this observed failure.

## Final reconciliation and disposition

**PARTIAL.** Real closed-bar automatic submission, exchange price/quantity confirmation, independent order recovery, persisted watermark continuation and pause with zero duplicate POSTs were observed. Resumed active execution was interrupted by the reporting failure; the corrected reporting path was verified offline, not re-certified online in a new campaign. No BUY fill or SELL occurred.

The exchange confirmed cancellation at 2026-11-14 19:07:35.011 UTC: DELETE HTTP 200, CANCELED, same orderId and clientOrderId. Cancellation consumed GET 100 / POST 0 / DELETE 1. A subsequent independent GET consumed 51 requests and confirmed no positions, active orders or UNKNOWN. `demo_cli stop` returned DEMO_STOPPED with enabled=false. The final independent GET at 2026-11-14 19:09:42.500292 UTC consumed GET 51 / POST 0 / DELETE 0 and confirmed the campaign inactive, the order CANCELED, no positions, no active orders and no UNKNOWN. Campaign totals are POST 1 / DELETE 1; fill and actual fees are zero.

The two normally exited workers processed five newly delivered closed bars in total (two in the first process, three in the paused process); the terminal observed closed-bar watermark was 2026-11-14 18:56:00 UTC, with quote watermark 1794682390239 before restart. Worker duration settings were 90 and 120 seconds; the later 390-second attempt failed rather than completing its requested duration. Run observations span the first 18:52 UTC bar through final reconciliation at 19:09:42 UTC; campaign creation was 18:48:52 UTC. Failed-process GET totals are not available in a completed report and are not included in successful-process counts.

The initial 44 targeted cases passed after correcting two new tests to stop the prior campaign before enabling the next. Five deployment/pause regressions and the additional composed-session unavailable-account reporting regression passed after the reporting change.

NO LIVE MONEY.
