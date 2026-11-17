# P7-M18-F01 — Constructor clock recovery

NO LIVE MONEY. NO_DUPLICATE_DISPATCH. PERSISTED_RISK_BUDGET_UNCHANGED. NO_NEW_APPROVAL_GATE.

## Failure and cause

Baseline: `4926a60c9b81d4f83245f2f04faaf729c7e50836`.

The recorded real startup failure was HTTP 400 / Binance -1021 on `/fapi/v3/account` after an initial successful `/fapi/v1/time`. `_read_account()` constructed a temporary in-memory adapter, synchronized once, and read account facts without recovery. `DemoSession.recover()` could not run until construction succeeded; it synchronizes on CLOCK_SKEW but propagates the error. This missing constructor recovery is the established control-flow defect. The physical cause of the historical timestamp rejection is not established.

`sync_time()` computes server time minus the midpoint of two local wall-clock samples. Every signed request uses `int(clock() * 1000) + offset_ms`, with recvWindow unchanged at 5000 ms. A local clock step during sampling affects this estimate; RTT/asymmetric network delay also limits a midpoint estimate. The historical 630.49 ms RTT / -1765 ms offset / 315.24 ms uncertainty sample was followed by a successful GET and does not prove why the previous request failed.

Account facts are read in order: `/fapi/v3/account`, `/fapi/v3/positionRisk`, `/fapi/v1/positionSide/dual`, `/fapi/v1/openOrders`, `/fapi/v1/symbolConfig`, and quantitative status (with the existing v2 account compatibility path). Temporary adapter initialization mutates only its in-memory store; individual GETs do not book account facts. Persistent protocol adapter initialization and controller recovery follow successful `_read_account()`. `open_grid_session()` acquires the existing operator lock and opens the existing stores before constructing the session; automatic Campaign creation follows construction and GET reconciliation.

Signing occurs inside the existing request path immediately before transport invocation. Demo GET transport performs identity/path validation, URL and opener construction before HTTP. There is no intentional delay or write admission for GET between signing and sending. No measurement of the historic failed request's sign-to-send delay exists.

## Implementation

An explicit `FuturesClient.recover_get_clock()` scope is enabled only around constructor account reads and the constructor's rule observation (`/fapi/v1/leverageBracket` is also signed). Only signed GET / -1021 / CLOCK_SKEW is eligible. The failed request is re-signed after the same client's `sync_time()` and attempted once, not by replaying `read_facts()` or rebuilding the session. Retry/sync failures preserve the original exception as cause. No timeout, AUTH, SIGNATURE, RATE_LIMIT, unsigned GET, POST or DELETE receives this recovery.

`ObservedClient` observes each underlying attempt, retaining the first error and counting the resync and retry separately. Client diagnostics retain endpoint/code/status/category, initial and recovery synchronization measurements, and outcome, never signed queries or credentials. Synchronization retains the existing algorithm and exposes monotonic RTT plus wall elapsed time for interpreting clock steps.

No changes to transport endpoint, recvWindow, write capabilities, journal replacement, risk/position/fee checks, Campaign policy, cumulative budgets, UNKNOWN reconciliation, or persisted STOP.

## Verification in progress

Initial constructor intercepted-HTTP selection: **27 passed in 3.98s**. This covers normal startup, one recovery at each of the six signed account-fact endpoints, repeated -1021 termination, original-cause retention, malformed synchronization, high RTT, both wall-clock step directions during and after synchronization, timeout, -1022, AUTH, RATE_LIMIT, unsigned/write exclusion, and full Grid reopening with filled order/fees/equity, retained STOP and cumulative budget.

The first expanded selection completed with **226 passed, 1 failed in 230.85s**. The sole failure was the pre-existing telemetry test mocking the outer `FuturesClient.request()` after telemetry moved to the per-attempt boundary; it now intercepts HTTP instead, preserving its error/weight/source assertions. Full-constructor failure, UNKNOWN restart and multiprocess reopening tests passed in that selection. The corrected constructor/observability selection then passed **40 tests in 102.77s**. Final constructor coverage, including the signed rule-observation GET and its sustained-failure bounds, passed **34 tests in 13.51s**. Across these selections, all **230 distinct affected cases** passed; the one initial mock-boundary failure was corrected and rechecked.

Commands:

```text
PYTHONPATH=src python -m pytest -q tests/integrations/test_demo_constructor_clock.py
PYTHONPATH=src python -m pytest -q tests/integrations/test_demo_constructor_clock.py tests/integrations/test_demo_observability.py
PYTHONPATH=src python -m pytest -q tests/integrations/test_demo_constructor_clock.py tests/integrations/test_demo_continuous_campaign.py tests/integrations/test_demo_receipts_m19.py tests/integrations/test_binance_demo_network.py tests/integrations/test_binance_demo_grid_network.py tests/integrations/test_binance_demo_runtime.py tests/integrations/test_demo_reconciliation_m14.py tests/integrations/test_demo_observability.py tests/integrations/test_binance_futures_trading.py
```

The last command is the initial expanded run with the one mock-boundary failure; the first two commands are the successful targeted follow-ups, not a claim that this command was rerun to a green exit. No unrelated full matrix was run locally.

## Original-account bounded GET observation

At **2026-11-17 03:51:29–03:51:59 UTC**, the original Windows Credential Manager binding and existing SQLite journals completed `open_grid_session(read_only=True, continuous=True)` and `bridge.recover()`: **39 GET, 0 POST, 0 DELETE** in 30.29 seconds. Admission was capped at 64 GET and 180 seconds, with the existing per-request timeout and no write capability. No Supervisor trading loop or new deployment was started.

Synchronization measured offset **-2557 ms**, monotonic RTT **562.00 ms**, midpoint network-delay uncertainty **281.00 ms**, wall elapsed **548.76 ms**, with recvWindow unchanged at **5000 ms**. No -1021 occurred: the real-network recovery branch was **not triggered**. Injected HTTP tests, not this successful read, establish the failure branch.

Account reconciliation succeeded: canTrade=true, ONE_WAY, wallet/available/margin balance **4999.97114760 USDT**, zero unrealized P/L, empty positions/open orders/UNKNOWN. Existing FILLED, CANCELED and REJECTED order records and historical fees remained recoverable. Original account binding and saved deployment `fl-d7345f8795464a65-g` matched before/after; all runtime/Campaign/dispatch rows were identical, preserving authorization identity and consumed risk budgets. Continuous authorization remained **STOPPED**, active Campaign remained absent, Grid supervisor remained **STOPPED** before/after. Gateway kill switch was false, a separate control layer; none of these controls was changed.

## Remaining risk and completion

A persistent clock error, repeated local clock steps, or network delay can still exhaust the single retry; initialization must then fail rather than widen recvWindow or repeat writes. The cause of the historical real timestamp rejection remains unknown. Existing post-construction recovery behavior is unchanged.

**CONSTRUCTOR_CLOCK_RECOVERY_ENGINEERING_VERIFIED**: normal startup preserved; injected constructor signed GET -1021 recovers once with a new signature; sustained failures terminate with the original failure and retry-stage diagnostics; failures perform no POST/DELETE; full Grid restart preserves account, positions/fees/equity, UNKNOWN recovery, STOP, deployment, authorization windows and cumulative budgets. Separate-process startup is serialized by the existing operator lock and produces neither extra windows nor writes. This engineering result includes intercepted failure evidence and the separately described real GET observation, not a claim of a naturally triggered real-network retry.
