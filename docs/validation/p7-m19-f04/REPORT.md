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

The Preview fixture correction passed its isolated success case, the complete observation module (**8 passed in 4.04 seconds**), and the affected collection/feed/recovery/supervisor selection (**99 passed in 52.55 seconds**). The affected selection promoted unhandled thread warnings to errors. Identical `tools/ci/targeted_validation.py` selections passed on Python 3.13 (**110 passed in 20.31 seconds**) and Python 3.14 (**110 passed in 21.25 seconds**), with independent temporary directories, execution inventories and unhandled thread warnings promoted to errors. The five selected targets cover constructor timing, signed GET recovery, observation windows, Grid supervisor and market collection.

## Follow-up account-read optimization (not implemented)

`DemoSession.__init__` calls `_read_account` to verify the binding and obtain the wallet seed, then `observe_demo_rules` reads exchangeInfo and leverageBracket. `grid_factory.create` subsequently calls `session.recover` before constructing the initial Grid; the empty-journal `DemoExecutionGateway.recover` delegates to `controller.reconcile`, so a second complete account set is required for actual reconciliation. The first set is not proof of journal consistency and may already have expired during rules initialization.

A normal set contains six GETs: v3 account, v3 positionRisk, positionSide/dual, openOrders, symbolConfig and apiTradingStatus. Missing v3 `canTrade` adds v2 account; missing is UNKNOWN, not true. Strong admission facts include identity, complete wallet/available funds and margin facts, position amount/side/entry/risk, one-way mode, leverage/margin configuration, unmanaged open-order absence, explicit trading permission and quantitative lock status. AccountFacts joins v3 account positions with matching risk amount/side and configuration; these dynamic facts must share one bounded five-second read window. Rule filters and leverage brackets are separate rule-initialization facts, not substitutes for that window.

The smallest future optimization candidate is initialization sequencing across `DemoSession.__init__`, `grid_factory.create` and `DemoExecutionGateway.recover`: acquire rules before the sole authoritative empty-journal account set, then use that same still-fresh set for wallet seeding and journal reconciliation. It could save **six GETs, or seven on the v2 compatibility path**, per fresh empty-journal initialization. This needs a dedicated proof of binding checks, no intervening writes, coherent controller initialization, the same five-second origin and invalidation on expiry; merely assigning `adapter.facts` or skipping `reconcile` is unsafe. Existing-order replay and late-fill reads must remain.

`prepare_limit_book` and `_fresh_facts_for_submission` are final-order admission reads, while `_admit_write` rechecks freshness at transport dispatch. Do not remove those reads or mix an older account set with a newer book. Persisted/expired facts and rules may support a clearly aged display, never current trading qualification. Reusing immutable rule metadata within one initialization could avoid a repeated rules acquisition if one is introduced, but the observed F04 window has only one exchangeInfo and one leverageBracket request, so it currently saves **zero GETs** there. Caching the v2 permission value across rounds is not an acceptable latency fix.

## Before/after interpretation

The F02 observation recorded 121 GETs over 187.757 seconds, including a positionRisk CLOCK_SKEW at 5.730 seconds RTT, an openOrders transport failure at 8.585 seconds and a subsequent 31.839-second quote receipt gap. This window ended during initialization after 16 GETs. Their counts and durations are therefore **not equivalent throughput samples**, and the lack of CLOCK_SKEW here is not evidence that network latency improved.

Deterministic comparisons isolate the code effects:

- Previously a Session CLOCK_SKEW resynchronized and escaped the recovery call. Now the failed GET is re-signed once within its recovery scope; successful earlier endpoints are not replayed. A second rejection still fails. Network timeouts still receive no immediate retry.
- Previously foreground recovery also prevented book acquisition. The modeled 70-second owner interruption now permits eight acquisitions at ten-second intervals, without consumer callbacks; the long interruption resets Bar eligibility. A true acquisition or market-time gap over 30 seconds remains a sticky failure even when newer books replace earlier receipts.
- Ordinary empty-order recovery retains six required account endpoints. Missing `canTrade` can still require the seventh v2 compatibility read; the official observation exercised that path. Existing order-recovery replays and late-fill queries are retained. No cross-round account cache or order-history omission was introduced.
- An account read set exceeding five seconds is rejected at the first response that crosses the bound, saving only subsequent requests whose combined facts could no longer qualify. This is not a transport-tail latency reduction.

## CI regression investigation (2026-11-20)

The cloud offline logs for run 44736626031 show the same obsolete CLOCK_SKEW assertion on Linux and Windows. The test raised directly from `gateway.recover`, outside the protocol request boundary. It now injects Binance -1021 into the positionRisk HTTP response and verifies one calibration, two attempts of that endpoint, one preceding account read, unchanged business parameters and a successful reconciliation. A separate non-protocol exception test verifies propagation without resynchronization or replay and rejects dispatch while the bridge is not ready.

The original Fast Python 3.13 failure reported only `ValueError`. On e31c315, trusted run 44741041715 failed the same Preview case in Fast Python 3.13 (134 passed) and Python 3.14 shard 0 (2680 passed); the stage was `supervisor_run`, with the HTTP opener reaching NetworkFixture. Cross-platform offline run 44741044517 passed, but its selection did not exercise this Preview test.

Forcing the first full poll locally confirmed the exact failing request: **GET `/fapi/v1/klines`**, raising **`ValueError('unsupported fixture request')`** from FakeTransport. It is the initial one-minute closed-Bar read, not a collector book failure, monkeypatch leak or state-directory collision. `worker_seconds=.01` let account recovery consume the entire ten-millisecond deadline on slower executions; DemoSupervisor then exited before feed.poll. Faster executions reached the unsupported endpoint. Consequently the earlier isolated and 108-case local passes did not prove coverage of the market path.

The Preview test now reuses `test_demo_market_feed.bar` to respond to klines at the existing HTTP fixture boundary, only for that test. It checks NVDAUSDT, one-minute interval, aligned start/end, requested count and the fixture exchange-time boundary, and returns contiguous valid OHLCV rows. All other requests retain the existing strict fixture behavior; unknown paths still fail. A test-only supervisor stops after the first report instead of a wall-clock deadline. Assertions require exactly one poll delivering one Bar and one quote, both klines and book GETs, reconciliation with live-Bar eligibility still false, zero writes and redacted output. The real background collector remains enabled. Successful shutdown also verifies the joined thread, closed feed SQLite, released session lock and reopenable databases. No production module or official evidence was changed by this fixture correction.

The observation now records a bounded stack of module basenames, function names and line numbers together with the initialization/run stage. Exception messages, local values, request parameters and account bindings are not recorded. Expected budget termination has category `WINDOW_ENDED`; an ordinary ValueError remains a failure. Injected failures after collector startup verify thread termination, closed feed SQLite, reacquirable operator lock and unlocked state databases without permitting writes.

## Strategy worker deadline regression (2026-11-20)

At 65b570d, trusted run 44744616279 passed Fast Python 3.13 and all software shards except Python 3.14 shard 2 (2654 passed, two Strategy worker failures). Actual sharded coverage and the merge gates correctly failed downstream. Cross-platform offline run 44744619585 passed all four jobs. Both Strategy failures reproduced locally: missing last_signal in Preview and zero POST in Execute.

The ten-millisecond worker deadline could expire during successful account recovery, before the first feed.poll. A controlled monotonic clock now independently exercises that boundary in both modes: recovery completes, elapsed time advances beyond the original ten-millisecond deadline, any poll is forbidden, and EXITED persists without Bars, signals or writes. Feed closure and operator-lock release are checked. This confirms the deadline path rather than treating missing signals as a strategy failure.

The composition test now stops after its first actual poll report, retaining a 60-second failure bound. It requires 21 Bars, one quote, no pause reason, reconciled account state, persisted BUY and watermarks, and LIMIT BUY quantity 0.04 at original price 100. Execute must traverse the offline HTTP fixture exactly once and consume exactly one persistent POST; Preview remains zero-write. The existing Campaign and risk decisions are unchanged. Only the Strategy test and this report are modified.

Self-review checked the real run/poll/recovery path, naturally generated signals, HTTP and persistent POST counts, separate deadline coverage, function-scoped monkeypatches and state cleanup. No production change or additional confirmed production defect was required. This is self-review, not Codex Review; the unavailable review service was not retried.

Focused validation passed all four cases. The first full module invocation passed 17 cases but its subprocess restart case imported an installed package instead of this checkout and failed with ModuleNotFoundError. With checkout src explicitly inherited through PYTHONPATH, the complete Strategy module passed **18 cases in 38.71 seconds**, including both ten-millisecond controlled deadline cases and subprocess restart. The affected feed/Grid supervisor/collection/GET recovery/Campaign/observation selection passed **132 cases in 86.53 seconds**, with unhandled thread warnings promoted to errors. Final-head trusted CI and gates remain pending.

## Review

The original Codex Review did not run: configured `gpt-6.2-sol` was rejected as unsupported for the account. A new read-only review against `origin/main` selected the locally listed `gpt-6.1-sol`; it stopped at the account usage limit before producing findings. Codex Review remains **BLOCKED**, not passed. A separate automated review could not access the target checkout and produced no verified findings.

A direct targeted code inspection covered protocol retry and allowlists, calibration lock order, collector ownership/coalescing/shutdown, owner recovery and fresh-Bar scheduling, account-set expiry and dispatch gates, plus network/telemetry changes. No additional confirmed P1/P2 production defect was found in that inspection. This is a direct code inspection, not a successful independent automated review. Self-review of the fixture correction confirms that it intercepts only klines, reuses the existing Bar constructor, preserves the deployment fixture and real collector, leaves failure-injection tests unchanged, and uses a completed poll rather than execution speed as its stopping condition. Final-head CI remains required for engineering acceptance.

The current offline evidence comprises 108 passing CI-targeted tests before the new cleanup cases, two passing new exceptional-cleanup cases, and 136 passing Campaign/UNKNOWN/network tests. The newly added non-protocol test initially used the wrong dispatch assertion; after correcting it to require `SafetyBlocked(RECONCILE_REQUIRED)`, its isolated rerun passed. These are 247 distinct passing cases across the selections, not a claim that the earlier failing invocation passed.

## Acceptance

| Judgment | Status | Evidence boundary |
|---|---|---|
| ENGINEERING_ACCEPTED | BLOCKED | Preview fixture root cause is confirmed and corrected; final-head validation remains pending. |
| CODEX_REVIEW_ACCEPTED | BLOCKED | Alternate listed model reached the account usage limit; no completed review. |
| SIGNED_GET_RECOVERY_ACCEPTED | BLOCKED | Deterministic retry tests pass, but the official window contained no CLOCK_SKEW/recovery event. |
| MARKET_CONTINUITY_ACCEPTED | BLOCKED | Deterministic continuity tests pass; official initialization stopped before collection began. |
| M19_EXECUTION_INPUT_READY | BLOCKED | The observed account read set exceeded five seconds; no fresh-Bar execution input was established. |
| CI_ACCEPTED | BLOCKED | Final-head trusted CI is pending. |
| TRUSTED_MERGE_GATE_ACCEPTED | BLOCKED | Final-head trusted merge-gate evidence is pending. |

The preserved 5000 ms recvWindow, five-second account/quote freshness, 30-second quote-gap guard, UNKNOWN reconciliation, original LIMIT prices, SELL reduceOnly and existing cumulative budget/STOP policies are unchanged. Preview has no write capability. F05 order/lifecycle experiments and C02 cloud validation remain separate from this observation.
