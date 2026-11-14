# P7-M06: Demo rejection diagnostics and read-only order preparation

**DEMO_READ_ONLY_DIAGNOSTICS_ACCEPTED** — **NO LIVE MONEY**.

The original rejection remains **ORIGINAL_REJECTION_CAUSE_UNKNOWN**. No new exchange order was submitted. Stage B requires independent approval of a new exact-account plan; this report and its candidate grant no execution authority.

Source inspection and validation used `f51d6de`, containing the #144 diagnostic repair. The delivery branch is based on its merged counterpart `247768a3763b465fd0c91e87a0811a92dd59706b` (#144, 2026-11-13), avoiding republication of that repair. No production source change was necessary for the demonstrated request path.

## Actual execution capabilities

| Boundary | Inspection result |
| --- | --- |
| Strategy → controller | `strategy_runtime.py:92–154` dispatches through the shared controller, preserving signal/order identity. Its present target-order implementation constructs MARKET, not LIMIT. The bounded LIMIT entry is the existing gateway preparation plus controller submit; no strategy rewrite is required for this one-order validation. |
| Shared admission | `trading/control.py:155–189` assesses risk and binds the exact intent. Duplicate known intents return the gateway confirmation, not a new submission. Reconciliation-required intents cannot be resubmitted. |
| Demo gateway | `execution.py:253–420` requires bound controller, exact account, active independent plan, same-client observed rules, canTrade, applicable lock checks, fresh account, fresh bounded prices and economic limits. `gateway.py:38–77` also checks mode, leverage, rule validation and available margin. No PIT/HFQ research certification gate occurs on this path. |
| Adapter | `execution.py:83–136` admits only the gateway intent and enforces a durable single cancellation opportunity for bounded policies. `adapter.py:87–131` commits identity before HTTP dispatch and persists definitive rejection diagnostics with the local event. |
| Offline versus network transport | `DemoExecutionTransport` in `execution.py` is deliberately offline; actual REST uses `DemoNetworkTransport` in `network.py:116–186`. The network writer still exists; it is not the read-only transport. |
| Host and credentials | Network host is the literal `https://demo-fapi.binance.com`; redirects are disabled. Protocol requires DEMO credential/client/transport alignment, and network admission checks the API-key-derived account binding. Successful signed Demo reads establish present key/secret usability on Demo, not write permission. |
| Signing | `protocol.py:138–188` synchronizes time, sorts/URL-encodes parameters, uses HMAC-SHA256, and signs timestamp plus default recvWindow=5000 ms. The observed midpoint offset was −1427 ms; signed reads succeeded without widening the window. |
| Parameter mapping | `rules.py:151–172` uses fixed-point Decimal strings, BUY/LIMIT, BOTH, reduceOnly=false and explicit GTC for the candidate. Filters are checked by `RuleObservation.validate`; no precision truncation or float conversion is used. |
| Error evidence | `protocol.py:48–70,228–241` retains HTTP status, integer exchange code, category and bounded sanitized msg. Unsafe credential/signature/URL echoes are suppressed. `adapter.py:119–126,209–241` stores diagnostics atomically with rejection. It does not retain arbitrary raw HTTP bodies. |
| REJECTED / UNKNOWN | `adapter.py:337–354` preserves terminal REJECTED without exchange ID by skipping order lookup during reconciliation. Unknown outcomes use exact-ID GET and never replacement POST. `gateway.py:91–151` returns existing confirmations and replays only unbooked deltas; repeated evidence does not charge fees again. |

## Current exchange observations

[Read-only observation](readonly-observation.json) completed at **2026-11-14T09:32:21.728081Z**. Twelve GET requests all returned HTTP 200: time, exchangeInfo, leverageBracket, account v3, positionRisk, position mode, openOrders, symbolConfig, account v2 (canTrade absent from v3), apiTradingStatus, premiumIndex and bookTicker. Balance was already available from account; a separate balance request was unnecessary. No order/history query was needed.

An earlier public-only pass made four successful GETs before credentials were available. One additional exchangeInfo GET at **09:34:09.194520Z** supplied the GTC and protection fields omitted from the preflight display: [symbol details](symbol-detail.json). **Total: GET=17, POST=0, DELETE=0, account-configuration writes=0**. No retry, polling or new execution authorization was used. Saved evidence contains selected facts, not raw private responses or full account identities.

| Condition | Actual reported value |
| --- | --- |
| Contract | NVDAUSDT / TRADING / TRADIFI_PERPETUAL / USDT quote and margin |
| Order support | LIMIT reported; timeInForce explicitly includes GTC |
| Price | tickSize 0.01; minimum 0.01; maximum 20000 USDT |
| LIMIT quantity | stepSize/minQty 0.01; maxQty 6000 |
| Minimum notional | 5 USDT |
| Price band | mark × [0.97, 1.03] |
| Other filters | MAX_NUM_ORDERS=200; POSITION_RISK_CONTROL=NONE; no unsupported filter reported |
| Protection fields | triggerProtect=0.0300; marketTakeBound=0.03. These are reported fields, not an assertion that conditional-order protection is activated for plain LIMIT. |
| Mark / bid / ask | 162.57844396 / 162.58 / 162.74 USDT |
| canTrade | true, from account v2 |
| USDT | wallet=available=5000; initial margin=unrealized PnL=0 |
| Exposure | zero nonzero positions; zero open orders |
| Account configuration | ONE_WAY; NVDAUSDT 1×; CROSSED |
| First risk tier | 0–10000 USDT, maximum initial leverage 10; observed 1× is compatible |
| Quantitative restrictions | Successful observation; ACCOUNT and NVDAUSDT NOT_REPORTED, no applicable reported lock. This does not certify global unlock. |
| Key-level trading permission / unreported write restrictions | FIRST_WRITE_UNKNOWN; authenticated GET is not evidence of order acceptance. |

These facts satisfy the observable conditions for preparing this small opening order. They must be refreshed for execution, rather than treated as permanently fresh account or market state.

## HTTP 400 classification

| Classification | Conclusion and evidence |
| --- | --- |
| CONFIRMED | The historical POST was HTTP 400; local order is REJECTED with zero fill/fee and no exchange ID. Original code/msg were lost. #144 repaired future diagnostic retention. See [F02](../p7-m05/f02-rejection-recovery.md); its historical artifacts were not searched again. |
| CONFIRMED | The later exact-ID GET returned −2013 for a different request. It cannot identify why POST failed. |
| POSSIBLE | Exchange parameter/filter validation, key-level write permission or other write-time restrictions could yield a definitive rejection. None has direct evidence for the old POST; no specific error code is assigned to it. |
| RULED_OUT | Treating GET −2013 as the original POST error; claiming the network writer is absent; claiming current LIMIT/GTC is unsupported; treating empty successful quantitative indicators as an automatic block. Present code and actual GET evidence refute these explanations. These exclusions do not prove historical conditions were identical. |
| UNKNOWN | The original POST's specific reason, old write-time account/market conditions, and whether an otherwise valid new write will be accepted. **ORIGINAL_REJECTION_CAUSE_UNKNOWN** remains final for the unrecoverable old error. |

No demonstrated current signing, parameter mapping, environment, error-persistence or admission defect required a production fix. Present successful reads do not retroactively exclude an old signature/timestamp/quantity/price error.

## New unauthorized LIMIT candidate

[Candidate boundaries](candidate-summary.json) are newly calculated from this observation, not copied from an expired approval. This is a human-review candidate, not a serialized executable approval object. Exact credential-derived account binding must be frozen privately before independent approval; no full identity is published here.

| Boundary | Candidate |
| --- | --- |
| Environment / account | DEMO only; account observed in the 09:32:21.728081Z read-only session |
| Order | NVDAUSDT BUY LIMIT GTC; BOTH; reduceOnly=false |
| Unique clientOrderId | `m06-7da365fc71994d9e-01` |
| Price rule | `floor(best_bid × (1 − 50 / 10000) / 0.01) × 0.01`, evaluated once from fresh GET data |
| Fixed limit interval | 160.96–162.56 USDT; reject outside, never clip or requote |
| Maximum ask | 164.36 USDT; exchange mark band also remains mandatory |
| Reference limit / quantity / notional | 161.76 USDT / 0.04 / 6.4704 USDT |
| Quantity | Fixed and maximum 0.04; minimum legal opening quantity for the observed price and both frozen interval endpoints |
| Maximum conservative notional | 6.58 USDT, covering quantity × max(limit, quote) |
| Fee budget | rate 0.001; separate cap 0.01 USDT. Budget, not an observed exchange fee schedule. Actual fees require trade evidence. |
| Maximum funds / margin | 6.59 / 6.59 USDT; full-notional reservation, unchanged 1× CROSSED |
| Opening loss threshold | 0.21 USDT; entry-admission threshold, not a stop-loss or guaranteed loss cap |
| Freshness | account and each quote source ≤5 seconds; rules ≤300 seconds; observe necessary rules/account/market again before any authorized dispatch |
| Writes / concurrency | At most one POST and one DELETE for this exact known order; max_orders=max_open_orders=1 |
| Runtime / absolute expiry | 600 seconds from capability creation; **2026-11-14T11:34:09.196020Z** (Beijing 19:34:09.196020), whichever ends first |
| Recovery / cleanup | GET_RECONCILE_NO_RESUBMIT / CANCEL_KNOWN_THEN_STOP; uncertain cancel permits GET only |
| Forbidden | MARKET fallback, automatic resubmit, automatic requote, automatic close, account configuration changes and production writes |

A DELETE is permitted only if separately approved, the order is known and still active, and authorization has not expired. Any fill leaves inventory unless separately authorized handling is approved; no closing order is included. A GTC order may outlive the write capability.

[Offline candidate rule review](candidate-rule-review.json) replays selected actual GET facts at their observation time through existing `RuleObservation.validate` and `order_parameters`, for the reference, lower and upper prices. All three passed quantity/price/minimum-notional/bracket/margin validation. Worst frozen quote notional is 6.5744 USDT, fee budget at 0.001 is 0.0065744, and full reservation is 6.5809744, below the respective caps. This is not current-freshness, full controller admission or online order acceptance.

## Targeted validation

The three existing Demo network, bounded preparation and execution suites initially produced **94 passed, 2 failed**. Both failures were subprocess `ModuleNotFoundError: No module named 'factorlab'`: the parent pytest source-path setup was not inherited by child interpreters. Rerunning only those two tests with explicit inherited PYTHONPATH produced **2 passed**. The existing rejection-evidence suite produced **14 passed**. Thus all **110 distinct selected tests** passed after correcting the invocation environment, without modifying tests or production code. The suites use injected HTTP responses and do not send real exchange orders.

## Answers and next acceptance boundary

1. **Complete request path?** Yes: shared controller → Demo gateway → adapter → independently authorized DemoNetworkTransport → official REST exists. DemoExecutionTransport alone is offline. The current strategy runtime produces MARKET; the bounded LIMIT preparation/controller path is the appropriate existing entry for this candidate.
2. **Known rejection factors?** Original diagnostic loss is confirmed. No specific original exchange rejection reason is known, and no present observable account/rule block was reported.
3. **Old HTTP 400 root cause recoverable?** No: ORIGINAL_REJECTION_CAUSE_UNKNOWN. The −2013 read response is not that cause.
4. **Account and contract conditions?** The observed canTrade, balance, configuration, exposure and reported rules support this candidate. Key-level write permission remains FIRST_WRITE_UNKNOWN, not a new authentication prerequisite.
5. **Real production defect found?** No new demonstrated defect. The prior diagnostic defect is already repaired by #144; no production source was changed here.
6. **What can be verified without a new order?** Demo authenticated reads, current account/rule facts, request/signing/host construction, admission/recovery implementation, targeted regressions and bounded candidate arithmetic. Not order acceptance or fills.
7. **Can the existing system send one bounded order after approval?** Yes, after exact private account-plan binding, independent live authorization, and fresh GET checks within all frozen bounds. No new executor is required.
8. **Actual exchange accept/reject response?** The historical POST rejection is known only as HTTP 400. This task received successful GET responses, not a new order acceptance or diagnosed new rejection.
9. **Real Demo fill/account reconciliation?** No new fill occurred. The observed account is flat; offline recovery/fee tests are not real Demo fill reconciliation.
10. **What remains for sustained Demo strategy operation?** First complete the independently authorized one-order lifecycle and actual fill/fee/account/restart reconciliation if a fill occurs. The current strategy emits MARKET while this candidate permits LIMIT only: continuing that strategy under a LIMIT-only deployment needs an explicitly selected existing bounded-quote integration, not widened authorization. Longer-running operation also needs a separately bounded deployment duration/order budget and a defined authorization for inventory/cleanup; the one-order 600-second candidate is not that deployment. No additional PIT/HFQ certification or generic audit gate is needed for this Demo validation.

Stage A ends at **DEMO_READ_ONLY_DIAGNOSTICS_ACCEPTED**. DEMO_ORDER_ACCEPTED, DEMO_ORDER_LIFECYCLE_ACCEPTED, DEMO_FILL_RECONCILED and DEMO_ORDER_REJECTED_DIAGNOSED have not been established by this task. Stage B remains separate and unexecuted.
