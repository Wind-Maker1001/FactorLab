# P7-M15 — NVDAUSDT forward shadow observation

**NO LIVE MONEY.** This study preserves the existing Grid Preview and its original LIMIT proposals. A sampled midpoint touch and a potential opposite-book crossing are market observations, never `EXCHANGE_FILLED`.

## Fixed experiment

One Demo GET-only window: NVDAUSDT, unchanged P7-M13 Grid factory, five-second inter-poll delay, maximum 3600 seconds including initialization, 3600-second proposal deadlines, 1500 total request admissions, 20 proposals and 5 MiB retained research data. The initial range remains the existing factory's once-bound 99%/101% range, one level, zero initial inventory, quantity parameter 1, maximum inventory 1, cash committed 7 and breakout pause; legal order sizing and original reservations remain unchanged. Deployment configuration/hash and source commit are frozen in capture records. No Grid tuning is used to increase touches.

The experiment event fixes UTC start/deadline and all command parameters before network access. Every session/feed GET is durably reserved before dispatch. Reservations conservatively include a request interrupted between reservation and actual dispatch. Restart retains the original deadline and request budget. API/network/data interruptions stop the window rather than launch recovery retries. Terminal proposals remain terminal. A separate output directory is required for an independent run.

## Interpretation

`NOT_TOUCHED` means no sampled midpoint reached the original LIMIT in the side's direction. `PRICE_TOUCHED` uses midpoint <= BUY LIMIT or >= SELL LIMIT. `POTENTIAL_BOOK_CONDITION` uses ask <= BUY LIMIT or bid >= SELL LIMIT; visible opposite-level quantity coverage is separate. `RIGHT_CENSORED` records an untouched proposal when observation ends. These are overlapping attributes, not four mutually exclusive outcome bins.

The first valid nonforward book delivered with the proposal is its baseline; its exchange and receive times remain explicit. Forward evidence requires both times strictly after capture. Baseline distances use `(limit / quote - 1) * 10000`; the analysis does not substitute later overwritten proposal distances. Missing baseline or top quantity stays unknown. Raw bookTicker objects and normalized observations share the bounded CSV; signed account responses and credentials are not research evidence.

Discrete polling does not reveal the intrainterval path, a touch during an outage, continuous crossing duration, exchange queue position or executions. A price jump across LIMIT is not evidence of a continuously traded path. Quote event-to-receive age is not interchangeable with HTTP request latency. Initialization, reconciliation and API latency add to five-second polling delay. The absolute deadline prevents new requests after the boundary; an already dispatched request can finish under the existing ten-second request timeout. Proposal expiry retains the existing conservative receive-time rule, so a quote received after expiry cannot establish an earlier touch.

UTC-hour quote summaries are descriptive within the captured window. Different hours are confounded with one continuous market path and do not establish a time-of-day effect. Historical OHLC and P7-M13 samples are not added to the new prospective sample count.

## Execution status

**M15_INSUFFICIENT_VALID_SAMPLES — INSUFFICIENT_EVIDENCE.** The new run started at 2026-11-16 10:39:53.148299 UTC and stopped at 11:02:51.565948 UTC (1378.418 seconds), before its fixed 11:39:53 deadline. Feed validation detected `STALE_POLL`; the interruption record preserves this reason even though the supervisor's final feed status is `STOPPED`. No replacement window or recovery retry was launched.

There were 71 valid captured books, one unique BUY LIMIT proposal and 68 valid forward book/proposal pairs. No observer-level rejected books were retained; the terminating poll was rejected by feed validation before its callback and is not included in the 71. The proposal, `preview-g:preview-g-00000002`, froze LIMIT 157.84000 and quantity 0.04 at 10:41:04.023721 UTC, from the closed 10:41:00 source bar. Deployment configuration hash is `50820fe9c024a0c9460fcb568ba39b1501b910afb931f89766ec8d90971d145e`; source commit is `bb27431c7ea041cfa7a388bc5cfa2730afad113d`. The once-bound range was 157.84–161.04, initial price 159.44.

Its baseline bid/ask was 159.39/159.44, exchange time 10:41:01.871 UTC and receive time 10:41:04.013121 UTC. LIMIT was 97.2457 bps below bid and 100.3512 bps below ask. Opposite top quantity was 0.06, covering 0.04 at that baseline price, but the ask was above LIMIT: quantity coverage alone did not constitute an executable condition.

Observed classifications were NOT_TOUCHED 1, PRICE_TOUCHED 0, POTENTIAL_BOOK_CONDITION 0 and RIGHT_CENSORED 1. Touch and potential proportions were 0/1; right censoring was 1/1 (100%). No first-touch waiting time or quantity coverage at a potential crossing can be estimated because there were no such observations. These results establish neither absence of intrainterval touches nor an exchange fill probability.

Spread across 71 books ranged 0.02–0.42 USDT (median 0.16, mean 0.15014). Valid-book event gaps ranged 15.420–28.668 seconds, median 18.3975, mean 18.8876: the configured five-second delay was not five-second effective sampling. Quote age ranged 2.057953–4.635219 seconds (median 2.623512). Serial reconciliation and network costs materially restricted coverage.

UTC hour 10 contributed 63 books, mean spread 0.16063; hour 11 contributed only eight, mean spread 0.0675. This is enough to formulate a narrower-spread-at-later-time hypothesis, not to distinguish time-of-day effects from this single market path. More independent, pre-fixed windows would be needed.

All 1322 request reservations completed as GETs; POST/DELETE counts were zero. This used 88.13% of the 1500-request ceiling. Aggregate HTTP time was 1006.003 seconds, mean 0.7610 seconds/request, maximum 8.2780 seconds. Endpoint/phase counts and latency totals are retained in `real/summary.json`; per-request timings are in `real/events.csv`. Captured CSV is 482882 bytes; CSV plus summary and analysis total 499083 bytes, below 5 MiB. Local Preview/feed database state occupied 167936 bytes and is excluded from published research evidence.

The directed integration suite passed 98 tests; after adding the final interruption-reason test, the observation module passed all 23 tests. XML results are retained alongside reproducible commands and the standard-library analysis script. P7-M13's previous ten books and one proposal remain separate: this run adds 71 books and one proposal, not 1322 samples. One heavily right-censored proposal does not support claims about profitability, win rate, slippage, execution probability or queue priority.
