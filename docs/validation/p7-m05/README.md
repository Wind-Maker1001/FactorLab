# P7-M05 E02 Demo time semantics and approval preparation

Current follow-up: [F02 rejection recovery](f02-rejection-recovery.md), **P7_M05_BLOCKED**. The F01 POST was rejected; its original error code/message were not retained. F02 exact-ID GET returned −2013. The E02 readiness observations below are historical, not current write authorization.

**P7_M05_READY_FOR_EXPLICIT_AUTHORIZATION** at the E02 preparation observation below. This is readiness for independent approval, not order authorization or online acceptance. Absolute expiry still applies.

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

E01 baseline: `0d3c4a17eab5ddb6294c1f7fa183dd781c89e66f`. E02 continues branch `validation/p7-m05-e01-admission`, PR #142, from `2ea071433864d8e5e6c22c7d7c9ce64788d15491`.

## Corrected time conclusion

The earlier calendar-date conflict was a timezone interpretation error in the report, not evidence that the policy had expired. UTC November 13 and Beijing November 14 can represent the same instant. No old receipt or expiry has been changed.

E02 official Demo time measurement:

| Fact | Value |
| --- | --- |
| Local request start | 2026-11-13T17:46:59.381187Z |
| Local request end | 2026-11-13T17:46:59.937882Z |
| Server time | 2026-11-13T17:46:58.323Z |
| RTT | 0.556695 seconds |
| Existing midpoint sync offset | −1336 ms |
| Evaluation UTC | 2026-11-13T17:46:59.937994Z |
| Same instant in Beijing | 2026-11-14T01:46:59.937994+08:00 |

At that evaluation the E01 policy expiry, `2026-11-13T19:40:34.126457Z`, was still in the future: **POLICY_UNEXPIRED**. Its historical quote was **STALE_QUOTE**. Neither statement permits execution.

Inspection of protocol, preflight, bounded quote, plan review and network capability checks found no date-only comparison or use of a session calendar date as UTC. Internal contracts require timezone-aware UTC and reject naive timestamps. Normalize UTC+08:00 inputs to UTC before passing them to those contracts. Policy uses full timestamp comparison; equality with expiry rejects. Signing uses local epoch plus the measured midpoint offset. Preflight evaluates exchange mark/book timestamps against local UTC plus that offset. Quote evaluation separately checks observation/mark/book ages; capability additionally uses an absolute expiry and monotonic max_seconds.

`sync_time()` has no independent maximum-offset invariant; it measures an offset for signing rather than declaring a universal clock-safety bound. The existing recvWindow remains 5000 ms, unchanged. Successful refreshed signed GETs and the existing five-second source-age checks establish usability of this small measured offset for this preparation. No NTP service, widened window or relaxed age threshold was introduced. No core trading code changed in E02.

## Refreshed evidence

E02 adds **GET=14, POST=0, DELETE=0, account-configuration writes=0**: one time measurement and thirteen preparation requests. E01+E02 cumulative counts are **GET=45, POST=0, DELETE=0, account-configuration writes=0**. Offline tests are not exchange requests.

Fresh preparation observation: `2026-11-13T17:47:53.288307Z`. Its independent sync measured −848 ms. Book time was `17:47:49.183Z`; mark time was `17:47:50.592Z`. At clock-corrected receipt their ages were 3.257307 and 1.848307 seconds, respectively; local observation age was zero. All satisfied five seconds at preparation. These saved quotes do not remain fresh for later execution: any separately authorized execution must obtain new observations without changing the approved policy or its hash.

Host: `https://demo-fapi.binance.com`. The private full credential-derived account binding matches the E01 credential (`...660da077`). This is an API-key-bound identity, not an independently returned exchange UID. Fresh account facts: canTrade=true, ONE_WAY, NVDAUSDT leverage 1×, CROSSED, zero nonzero positions, zero open orders, sufficient available USDT. The new exact clientOrderId was ABSENT. Opening review, fixed-quantity rules and observed/upper-bound controller reviews passed.

| Evidence | Result | Scope |
| --- | --- | --- |
| API Key futures permission | VERIFIED / USER_DECLARATION | User confirmed Read and Futures enabled for this exact Demo credential; Spot/Margin/Stocks also enabled. Not an API permission-query result. |
| Quantitative restriction evidence | PARTIAL | Successful official response has empty indicators; ACCOUNT/NVDAUSDT remain NOT_REPORTED, not NO_LOCK. No applicable reported lock or parser error. |
| History and UNKNOWN | PARTIAL | Known IDs checked, no located unresolved UNKNOWN; user declares no other execution; LOCAL_HISTORY_INCOMPLETE remains disclosed. |
| Complete approval object | VERIFIED | Fresh version-2 Policy/Plan, canonical serialization and full-account hashes; independent approval still required. |

Manual evidence identity `m05-user-declaration-demo-660da077-permissions` records the permission and exact Demo-key association confirmation. Identity `m05-user-declaration-demo-660da077-additional-history` records the declaration that no manual orders, other scripts or ambiguous order outcomes occurred outside M02–M05. These are USER_DECLARATION evidence, not exchange facts or approval records. IP restriction compatibility and key expiry were not separately declared; authenticated reads succeeded and first-write key permissions remain an exchange acceptance condition.

The official quantitative response retained `indicators: {}` and `updateTime`; neither target scope was present. Existing `observe_quantitative()` distinguishes this successful non-report from malformed/conflicting responses and explicit isLocked=true. Existing safety contracts allow preparation for non-report, block errors/UNKNOWN, and block applicable ACCOUNT/symbol locks. Those gates are unchanged. Exchange restrictions not reported by GET remain a first-write acceptance risk, not a reason to require a test POST during preparation.

History inspection was limited to the known local Demo root, predecessor M05 evidence and depth-three known M02–M04 worktree state roots. No execution SQLite/protocol journal was located. Historical IDs `m03-5589adc5a5e847f9-01` and `m04-3f07f8d7d793423d-01` returned -2013 on E01 exact GET. M03 recorded a pre-write price-boundary stop, not a POST. Exact absence, recent orders and flatness do not prove lifetime absence. LOCAL_HISTORY_INCOMPLETE remains a residual evidence gap; no new unresolved UNKNOWN is inferred or cleared by the user declaration. Any recovered UNKNOWN must use the unchanged blocking/recovery contract.

## New independent approval candidate

[Complete sanitized display](candidate-summary.json). It is not an executable policy: the full account binding is private, and redaction changes canonical hashes. The executable private Policy is exactly the Policy nested in the Plan; display derives from their canonical serialization. The immutable preparation receipt still contains generic manual-evidence blockers because the tool does not load external declarations. The summary separately binds the declarations above without modifying that receipt or claiming an API-observed permission.

| Boundary | Value |
| --- | --- |
| Environment / order | DEMO / NVDAUSDT BUY LIMIT GTC |
| positionSide / reduceOnly | BOTH / false |
| Unique clientOrderId | `m05-033ae399268e414c-01` |
| Pricing | `floor(best_bid * (1 - offset_bps / 10000) / tickSize) * tickSize` |
| Offset / tick | 50 bps / 0.01 USDT |
| Limit interval / maximum ask | 159.27–161.69 / 163.37 USDT |
| Observed bid / ask / mark | 160.88 / 161.75 / 161.30 USDT |
| Observed candidate / mark band | 160.07 / 156.461–166.139 USDT |
| Maximum source age | 5 seconds each for observation, mark and book |
| Fixed / maximum quantity | 0.04 / 0.04 NVDA |
| Maximum notional | 6.54 USDT |
| Independent maximum fee / reserve rate | 0.01 USDT / 0.001 |
| Maximum funds / margin | 6.55 / 6.55 USDT; full-notional collateral model |
| max_loss | 0.21 USDT opening-admission threshold, not stop-loss or guaranteed loss cap |
| max_orders / max_open_orders | 1 / 1 |
| Maximum writes after separate authorization | One LIMIT POST, one DELETE for this exact known order |
| Automatic resend / MARKET / close | false / false / false |
| UNKNOWN | Exact-ID GET-only recovery; no replay or replacement |
| max_seconds | 600 from capability creation |
| Absolute expiry UTC | 2026-11-13T19:47:53.288307Z |
| Absolute expiry Beijing | 2026-11-14T03:47:53.288307+08:00 |
| Cleanup / recovery | CANCEL_KNOWN_THEN_STOP / GET_RECONCILE_NO_RESUBMIT |
| Local approval record | `unapproved:m05-033ae399268e414c-01` |

Policy SHA-256: `e319cf6dfaa3559abd2e41abad7b175e7f42ab06fd2c53ab3db79b5217c6bd07`.

Plan SHA-256: `d51d427721f26138fc6e146d875cb367401c5f3f2c53a66c62ba73361094979d`.

Rule identity: `45cf84fd3ded0b27ad5f5887172d407569d80514b552c29b95b203f6773c3d1c`.

Configuration identity: `f6bc24839130878e7a20b9665f4d422d668bd6ebfc4bee2db6a3c7fba00ef502`.

Account-observation identity: `e86c01450aab820880bbc5f238ca051e969822e67b9bf7e52eacd2ea576a8b84`.

Execution-source identity: `9eb8cd4ebc53efe28f0ed1e68ae86a2b6dc65a64bd5c5a8104940e14670c20d8`, content digest of the ten directly applicable integration modules. Version-2 execution rejects changed rules/tick, configuration or execution-source identity, alongside existing account/freshness/economic gates.

No network-write capability exists. Approval must independently bind the complete full-account object. The earlier of policy/plan expiry and capability max_seconds ends writes; expiry rejects DELETE too. A GTC order may remain open after expiry. Partial/full fills leave inventory; closing requires separate reduce-only approval. A rejected first write must not be retried automatically; ambiguous POST/DELETE permits only exact-ID GET recovery. Actual exchange commission and key-level trading acceptance cannot be established by these GET observations.

## Validation and private originals

E02 targeted tests: **12 passed**, covering equivalent timezone instants, expiry before/equal/after, five-second inclusive and exceeded ages for each source, naive-time rejection, unchanged hashes under quote refresh, independent identities/hashes, and unauthorized POST/DELETE blocking. No full 190-test rerun. The actual midpoint measurement and successful signed refresh cover the observed small clock offset.

E02 modifies only this report, candidate-summary.json and `tests/integrations/test_m05_time_semantics.py`. E01's 167 existing targeted and 23 context tests remain historical validation evidence.

Private originals: `D:/Code/FactorLab-final-evidence/p7-m05-e02/private/time.json`, `private/preparation.json`; local masked receipt: sibling `review/preparation.json`. E01 originals remain unchanged under `p7-m05-e01/private/`. No raw private responses or credentials are uploaded. No environment or ledger was created. PR #142 remains Draft and must not auto-merge.
