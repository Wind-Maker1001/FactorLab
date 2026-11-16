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

The bounded run and directed test results are recorded in `docs/validation/p7-m15/`. Final sample counts and research conclusion will be entered after the bounded experiment terminates.
