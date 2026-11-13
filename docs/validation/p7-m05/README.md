# P7-M05 E01 admission evidence

**P7_M05_EVIDENCE_PARTIAL**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

Baseline: `0d3c4a17eab5ddb6294c1f7fa183dd781c89e66f`.

| Evidence | Result | Meaning |
| --- | --- | --- |
| API Key futures trading permission | VERIFIED | USER_DECLARATION confirms Read and Futures permission for the exact Demo credential bound to ...660da077; no API permission-query verification. |
| Quantitative restrictions | PARTIAL | Official response contains `indicators: {}` and `updateTime`; neither ACCOUNT nor NVDAUSDT is reported. |
| Historical orders / UNKNOWN | PARTIAL | Full local account bindings match; two known historical identities return `-2013`; LOCAL_HISTORY_INCOMPLETE. |
| Immutable approval object | PARTIAL | Version-2 Policy and Plan round-trip and hashes exist; history evidence and a current unexpired candidate remain outstanding. |

## Network and account facts

E01 actual official Demo traffic: **GET=31, POST=0, DELETE=0, account-configuration writes=0**. Initial preparation issued 13 GET; historical identity/quantitative inspection issued four GET; local/server time comparison issued one GET; refreshed preparation issued 13 GET. These counts exclude the predecessor M05 observation (13 GET). Official documentation retrieval did not complete and is not exchange account traffic.

Host: `https://demo-fapi.binance.com`. Account reference: `DEMO:FactorLab-Demo:sha256:...660da077`. The private full credential-derived binding matches the historical M03 plan and refreshed preparation exactly; this is an API-key-bound identity, not an independently reported exchange UID.

Latest complete account observation: **2026-11-13T17:23:16.790595Z**, as recorded by the observation runtime. Account canTrade=true; authenticated user-data reads succeed; ONE_WAY; NVDAUSDT leverage=1; CROSSED; nonzero positions=0; open orders=0; initial margin=0; available funds cover the 6.52 USDT candidate budget. This timestamp is earlier than the task's stated 2026-11-14 date. It is retained without substitution. The candidate expires **2026-11-13T19:23:16.790595Z** and must not be presented as unexpired on 2026-11-14. No READY status is issued.

## API permission evidence and manual verification

`canTrade` proves the account flag, not API-key TRADE permission. Signed GET success proves those authenticated reads only. No applicable Demo key-permission GET has been established: the existing USD-M read interface contains no key-restrictions endpoint, and official documentation retrieval was unavailable. This is UNKNOWN, not a finding that no such official endpoint could exist. Do not probe with POST or use a production SAPI key-restrictions endpoint against this Demo credential.

In the official Binance Demo interface, open the API management page for the exact credential used locally. Confirm that it belongs to the Demo account, is enabled and unexpired, and explicitly permits USD-M futures trading (the interface's Futures/Trade permission, not merely Read). Check any IP restriction against the execution host. If the page does not expose a separate trade permission, report that fact rather than inferring it. Provide a dated declaration naming the Demo context, local account reference, displayed permission wording and result; crop or redact API key, secret, QR codes and identifiers before sharing any screenshot. No secret needs to be submitted. Record this as USER_DECLARATION, separately from API_OBSERVATION. USER_DECLARATION recorded on 2026-11-14: the exact official Demo credential bound to `...660da077` has Read and Futures enabled; Spot, Margin and Stocks permissions are also enabled. The environment and credential association are explicitly confirmed. This closes the manual key-permission evidence only; it is not an API-observed permission result or order-write authorization. IP restriction compatibility and credential expiry were not separately declared.

The first separately authorized LIMIT POST may still reject for key-level permissions or exchange-only order validation. That is a first-write acceptance condition, not a prerequisite for finishing read-only preparation. A rejected request must not be retried automatically; an ambiguous outcome permits exact-ID GET recovery only.

## Quantitative mapping

The additional signed `/fapi/v1/apiTradingStatus?symbol=NVDAUSDT` returned top-level `indicators` and `updateTime`; `indicators` was an empty object. ACCOUNT and NVDAUSDT were absent. This is case A: the successful official response did not report those scopes. It is neither parser omission nor an affirmative NO_LOCK. The compact receipt retains field presence without copying raw account responses into the repository.

`observe_quantitative()` reads every reported scope and indicator. Boolean isLocked=true becomes LOCKED; false becomes NOT_LOCKED; malformed/missing indicator containers and conflicting fields produce an error. `applicable_locks()` applies ACCOUNT and exact-symbol locks. Existing preflight and opening review allow preparation for a well-formed empty report, block applicable reported locks, and block errored/UNKNOWN observations. These formal admission rules are unchanged. The residual risk is an unreported exchange restriction; first-write acceptance must inspect the actual response without treating the empty report as clearance.

## Account-bound history

Bounded depth-three inspection covered `%LOCALAPPDATA%/FactorLab/demo`, the predecessor M05 evidence root, and `local-runs`, `local-artifacts`, `local-tmp` in the known M02 online/C1, M03 and M04 worktrees. The Demo state root contained the M03 opening-plan JSON; no execution SQLite or protocol journal was located in these scoped roots. Missing directories are missing evidence, not proof of no execution.

Known historical IDs, deduplicated by exact identity:

| clientOrderId | Evidence |
| --- | --- |
| `m03-5589adc5a5e847f9-01` | M03 account-bound local plan; refreshed exact GET returns -2013 ABSENT. M03 report records a pre-write price-boundary stop and zero POST/DELETE. |
| `m04-3f07f8d7d793423d-01` | Prior M05 private preparation with matching full binding; refreshed exact GET returns -2013 ABSENT; preparation recorded zero POST/DELETE. |
| `m05-21c7e4fe2d7d4f2e-01` | Newly generated candidate; preparation exact GET returns -2013 ABSENT. |

No unresolved UNKNOWN was found in the located records. **LOCAL_HISTORY_INCOMPLETE** remains: missing execution journals and exact-ID absence cannot prove that no historical order ever existed, including orders outside exchange retention. Current flatness and no open orders are distinct facts. Any recovered local UNKNOWN must retain the existing conservative block until exact-ID recovery resolves it. USER_DECLARATION recorded on 2026-11-14: no manual orders, other script executions or ambiguous order outcomes occurred on this Demo account outside the reviewed M02–M05 flows. This closes the requested additional-history disclosure, but does not reconstruct missing local journals or constitute exchange-wide historical proof. No new order may be used to test history.

## Refreshed candidate and time evidence

A subsequent official Demo time GET returned `2026-11-13T17:39:53.445Z`; local UTC was `2026-11-13T17:39:55.049451Z`, with midpoint offset −1.350361 seconds. Thus both runtime and exchange report November 13, while the session date is November 14. This discrepancy is unresolved; no timestamp or expiry is rewritten and no READY status is issued.

Refreshed GET-only preparation completed at `2026-11-13T17:40:34.126457Z`, expiry `2026-11-13T19:40:34.126457Z`. The same masked account reference was observed; canTrade=true, ONE_WAY, 1×, CROSSED, zero positions and zero open orders. ACCOUNT/NVDAUSDT remain NOT_REPORTED. All four opening/rule/controller reviews passed.

This refreshed candidate supersedes the earlier candidate below for review, but is not represented as currently unexpired against the session date. Its complete boundaries are in [candidate-summary.json](candidate-summary.json), a sanitized non-executable display. The executable full-account object remains only in the private `preparation-refresh.json` receipt. Its policy hash is `a89c3bcefba4061aba3582fc842602fbc16ad3270ff3f45e4c6d9eb83466d94f`; plan hash is `a294c2d6a3752035900653b8a2ef17111fae0355a9827347688a1177a9bdcfdb`.

The standalone preparation tool does not consume external manual declarations: its generic permission/history blockers remain in the original immutable receipt. The declarations recorded above close those manual evidence requests in this report, not by editing the receipt. Remaining preparation status is **P7_M05_EVIDENCE_PARTIAL** because time/validity is unresolved, not because another permission/history declaration is required.

## Earlier candidate boundaries and immutable identities

The private receipt holds the full executable account binding and canonical version-2 Policy nested in the canonical Plan. `BoundedQuotePolicy.from_json()` and `DemoExecutionPlan.from_json()` reconstruct those same objects; the display is derived directly from their canonical serialization, not an independently assembled authorization dictionary. A masked public summary is not an executable policy and cannot reproduce the full-account digest. Serialized data never grants process-local network authorization.

| Boundary | Candidate value |
| --- | --- |
| Environment / symbol / order | DEMO / NVDAUSDT / BUY LIMIT GTC |
| Position / reduceOnly | BOTH / false |
| clientOrderId | `m05-21c7e4fe2d7d4f2e-01` |
| Formula | `floor(best_bid * (1 - offset_bps / 10000) / tickSize) * tickSize` |
| Offset / tick | 50 bps / 0.01 USDT |
| Limit range / ask ceiling | 159.37–161.79 / 162.61 USDT |
| Observed bid / ask / mark | 160.98 / 161.00 / 161.00 USDT |
| Observed candidate / mark band | 160.17 / 156.17–165.83 USDT |
| Quote maximum age | 5 seconds for observation, mark and book |
| Fixed / maximum quantity | 0.04 / 0.04 NVDA |
| Maximum notional | 6.51 USDT |
| Independent maximum fee / reserve rate | 0.01 USDT / 0.001 |
| Maximum funds / maximum margin | 6.52 / 6.52 USDT; full-notional collateral model |
| max_loss | 0.21 USDT opening-admission threshold, not a stop-loss or guaranteed loss cap |
| Orders / open orders | 1 / 1 |
| Network write bounds after separate approval | At most one LIMIT POST and one DELETE for this exact known order |
| Automatic resubmit / MARKET / close | false / false / false |
| UNKNOWN recovery | GET only; no replacement or replay |
| max_seconds / expiry | 600 from capability creation / 2026-11-13T19:23:16.790595Z, whichever is earlier |
| Cleanup / recovery | CANCEL_KNOWN_THEN_STOP / GET_RECONCILE_NO_RESUBMIT |
| Local approval record | `unapproved:m05-21c7e4fe2d7d4f2e-01` |

Policy SHA-256: `54db0b74a7e13fc5e7021d809fd983932bf0f648c02d8e4a0a8c05179e828446`.

Plan SHA-256: `ea03553637bbe5d94c50f9cbe69d542c88b8ddea0225e0e9664e2d3f9b553eae`.

Rule content identity: `45cf84fd3ded0b27ad5f5887172d407569d80514b552c29b95b203f6773c3d1c`.

Configuration identity: `f6bc24839130878e7a20b9665f4d422d668bd6ebfc4bee2db6a3c7fba00ef502`.

Account observation identity: `1cab7477b5eb824fbd1d91482c060f4b5f7c1a3102a145e3b61cb58943018a95`.

Execution-source identity: `9eb8cd4ebc53efe28f0ed1e68ae86a2b6dc65a64bd5c5a8104940e14670c20d8` (content digest of the ten directly applicable integration modules, not a repository commit identity).

Rule content excludes observation time, allowing a fresh observation of unchanged rules. Configuration binds the account, hedge mode and exact symbol configuration. Version-2 quote evaluation rejects changed rules/tick, configuration or execution-module identity, in addition to existing freshness, account, price, fee, funds, margin and admission checks. Historical account-observation identity is provenance; fresh account facts are still required at execution. The marked price band is an observation, not a fixed future exchange band.

A GTC order can survive expiry, and expiry prohibits DELETE as well as POST. Partial/full fills create inventory; there is no automatic close, guaranteed loss cap or stop-loss. Closing requires separate reduce-only approval. Independent maximum fee is a local reserve ceiling, not an exchange commission promise.

## Validation and remaining evidence

Targeted existing preparation, bounded quote, network, execution and preflight suites: **167 passed**. Additional approval-context tests: **23 passed**, covering canonical round trips, policy/plan identity, important-field hash changes, changed approved rules, missing versus locked/malformed quantitative reports, and non-expandable one-shot write fields. Existing suites cover account binding, stale/expired quotes, economic boundaries, UNKNOWN recovery, one-shot cancellation, no-write preflight and production rejection. An initial test command named a nonexistent preflight test file and collected no tests; the corrected selection passed.

Private original receipts remain at `D:/Code/FactorLab-final-evidence/p7-m05-e01/private/preparation.json` and sibling `history.json`; the local masked preparation is in sibling `review/preparation.json`. They are not uploaded. No new environment, order ledger or admission system was created.

TODO: Establish a current, consistent observation time and generate a new unexpired candidate for independent approval; retain LOCAL_HISTORY_INCOMPLETE as a disclosed residual evidence gap. Until then formal approval preparation is not complete. No prior authorization carries forward and no order write is authorized by this report.
