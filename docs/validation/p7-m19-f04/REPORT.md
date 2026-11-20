# P7-M19-F04 — Demo signed GET recovery and market continuity

NO LIVE MONEY.

## Measurement

The official observation uses isolated Preview state, a 155-second worker window, at most 600 GET requests, a 5 MiB evidence budget and a 600-second absolute admission deadline. POST and DELETE are rejected before dispatch. The observation is run once, including failed startup or interrupted recovery.

The single window started at **2026-11-20 12:29:11.711740 UTC** and ended after **16.625486 seconds** with `STALE_ACCOUNT` during Preview initialization. It admitted and completed **16 GET, 0 POST, 0 DELETE**. All HTTP responses were 200; there were no exchange rejections, transport failures or CLOCK_SKEW events. Raw sanitized endpoint and local-stage evidence is in [get-only-evidence.json](get-only-evidence.json).

The initial account read set completed within five seconds. Rules initialization then required exchangeInfo and leverageBracket. The subsequent account read set began at elapsed 9.054824 seconds and returned its v2 account compatibility response at 16.624425 seconds: approximately **7.570 seconds** for the set. That response alone took **4.251803 seconds**. The read-set expiry correctly prevented the following apiTradingStatus request and prevented the Grid factory from establishing a fresh reconciled deployment. The collector and Bar consumer were never started.

| Endpoint | Count | Total seconds | Maximum seconds | HTTP/network errors |
|---|---:|---:|---:|---:|
| `/fapi/v1/time` | 1 | 0.610302 | 0.610302 | 0 |
| `/fapi/v3/account` | 2 | 1.251287 | 0.695068 | 0 |
| `/fapi/v3/positionRisk` | 2 | 1.373791 | 0.870200 | 0 |
| `/fapi/v1/positionSide/dual` | 2 | 1.037607 | 0.532600 | 0 |
| `/fapi/v1/openOrders` | 2 | 1.094230 | 0.548445 | 0 |
| `/fapi/v1/symbolConfig` | 2 | 1.393779 | 0.730126 | 0 |
| `/fapi/v2/account` | 2 | 5.360676 | 4.251803 | 0 |
| `/fapi/v1/apiTradingStatus` | 1 | 0.519586 | 0.519586 | 0 |
| `/fapi/v1/exchangeInfo` | 1 | 2.404482 | 2.404482 | 0 |
| `/fapi/v1/leverageBracket` | 1 | 0.927461 | 0.927461 | 0 |

For the slow v2 response, signature-to-transport was approximately 0.005 ms, transport-to-opener 14.493 ms, opener-to-headers 2.594377 seconds, and headers-to-body-completion 1.642662 seconds. Calibration age at signing was 11.459 seconds and uncertainty 317.679 ms. These measurements locate substantial response/body delay locally, but do not reveal server reception time. The exchange accepted the signed request; total response time is not evidence of recvWindow violation.

No replacement official window was run.

## Diagnostic boundaries

Signature completion, transport entry, HTTP opener entry, headers available and body completion are local observations. Opener entry is not a wire-send timestamp; headers availability is not server reception. Total RTT cannot locate the request's arrival within the 5000 ms recvWindow.

A signed GET CLOCK_SKEW triggers one calibration and one re-signed retry of that GET, not a replay of the whole reconciliation. Transport failures are not automatically retried. Expired account read sets fail before subsequent unnecessary reads and never establish reconciliation.

An independent unsigned client shares the Session calibration authority. Background book acquisition neither advances durable Bar cursors nor invokes strategy callbacks. Every adjacent acquisition is checked for the existing 30-second continuity limit; coalescing cannot erase an interruption. The owner handles durable cursors and consumer callbacks. A recovery spanning a Bar interval resets the fresh-Bar eligibility threshold. Collection failures also participate in the dispatch gate.

## Offline evidence

- Signed recovery, constructor timing, market collection, Grid supervisor, market feed and Demo network: 212 passed in 68.41 seconds.
- Extended collection suite, including owner recovery spanning a Bar interval: 8 passed in 1.20 seconds.
- Final measurement harness, collection, read-set recovery and observability run: **31 passed in 52.90 seconds**, with unhandled thread warnings promoted to errors. The offline Preview test intercepts both authenticated and unsigned HTTP opener paths and checks that no account binding or credential is emitted.

## Before/after interpretation

The F02 observation recorded 121 GETs over 187.757 seconds, including a positionRisk CLOCK_SKEW at 5.730 seconds RTT, an openOrders transport failure at 8.585 seconds and a subsequent 31.839-second quote receipt gap. This window ended during initialization after 16 GETs. Their counts and durations are therefore **not equivalent throughput samples**, and the lack of CLOCK_SKEW here is not evidence that network latency improved.

Deterministic comparisons isolate the code effects:

- Previously a Session CLOCK_SKEW resynchronized and escaped the recovery call. Now the failed GET is re-signed once within its recovery scope; successful earlier endpoints are not replayed. A second rejection still fails. Network timeouts still receive no immediate retry.
- Previously foreground recovery also prevented book acquisition. The modeled 70-second owner interruption now permits eight acquisitions at ten-second intervals, without consumer callbacks; the long interruption resets Bar eligibility. A true acquisition or market-time gap over 30 seconds remains a sticky failure even when newer books replace earlier receipts.
- Ordinary empty-order recovery retains six required account endpoints. Missing `canTrade` can still require the seventh v2 compatibility read; the official observation exercised that path. Existing order-recovery replays and late-fill queries are retained. No cross-round account cache or order-history omission was introduced.
- An account read set exceeding five seconds is rejected at the first response that crosses the bound, saving only subsequent requests whose combined facts could no longer qualify. This is not a transport-tail latency reduction.

## Review

Codex Review was attempted against the required baseline but did not run: the configured `gpt-6.2-sol` model was rejected with “The 'gpt-6.2-sol' model is not supported when using Codex with a ChatGPT account.” Codex Review is **BLOCKED**, not passed.

## Acceptance

| Judgment | Status | Evidence boundary |
|---|---|---|
| ENGINEERING_ACCEPTED | BLOCKED | Offline implementation evidence is available; required Codex Review did not execute. |
| SIGNED_GET_RECOVERY_ACCEPTED | BLOCKED | Deterministic retry tests pass, but the official window contained no CLOCK_SKEW/recovery event. |
| MARKET_CONTINUITY_ACCEPTED | BLOCKED | Deterministic continuity tests pass; official initialization stopped before collection began. |
| M19_EXECUTION_INPUT_READY | BLOCKED | The observed account read set exceeded five seconds; no fresh-Bar execution input was established. |
| CI_ACCEPTED | BLOCKED | Final-head trusted CI is pending. |
| TRUSTED_MERGE_GATE_ACCEPTED | BLOCKED | Final-head trusted merge-gate evidence is pending. |

The preserved 5000 ms recvWindow, five-second account/quote freshness, 30-second quote-gap guard, UNKNOWN reconciliation, original LIMIT prices, SELL reduceOnly and existing cumulative budget/STOP policies are unchanged. Preview has no write capability. F05 order/lifecycle experiments and C02 cloud validation remain separate from this observation.
