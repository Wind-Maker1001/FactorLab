# CI-PERF-09: trusted dual-Python V2 weight activation

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED.

## Measurement identity and migration

Baseline: `adcc6cf4ec35130b57d540e09abfa57272f63b54` (#181).
The production timing file is an exact byte copy of P05's
`candidate_module_timings.json`, SHA-256
`75a5bac467d1bc23cf9f1e4e6b83d3b00beb87301994051d2442f6e032fbc5d3`.
The candidate's historical status and analysis target remain unchanged; deployment
does not turn an earlier observation into a measurement of the deployment tree.

Both Python tables contain 315 measured modules and 7,819 measured nodes, from
[run 43735547182](https://github.com/Wind-Maker1001/FactorLab/actions/runs/43735547182),
attempt 1. Subject: `31346cca2d233ce65bf64728009884c53788b86c`;
PR head: `cfdaafaee9cc1c2fad045696bc18133c59c28361`;
trusted base and event SHA: `27180431626e6395dfb7907bab5e81f74cbb20fe`.
Python identities are 3.13.17 and 3.14.10, with each version's own three
`extended-<version>-shard-0/1/2` source artifacts. P05's source manifest retains
artifact IDs/digests, dependencies and frozen input identities. Timing is JUnit
setup + call + teardown, not a new execution or stage-specific measurement.
No #175 PARTIAL observations or historical V1 values enter either V2 table.

Strict JSON loading rejects duplicate keys, nonfinite/negative/non-numeric costs,
missing provenance and missing/mismatched Python tables. The production tables
were compared with P05's independently archived observed and candidate tables;
all module paths are relative `tests/*.py` paths without traversal. Canonical
assignment digests below use the existing sorted compact JSON SHA-256 function.

## Compatibility with the deployment tree

Main's current inventory contains 7,927 nodes in 316 modules, versus P05's
7,819 nodes in 315 modules. All 108 added nodes are in
`tests/security/test_trusted_shard_plan.py`; no P05 nodes were removed.
Two previously measured modules changed:
`tests/security/test_four_shard_contract.py` and
`tests/security/test_validation_v2.py`.
Runtime identity also differs in `tools/ci/current_validation.py`,
`tools/ci/integration_checks.py`, `tools/ci/pr_identity.py`, and
`tools/ci/sharding.py`. These are actual differences from P05, not fresh
measurements. The historical measurements remain scheduling estimates, not
claims of current-tree timing equivalence. New unmeasured modules retain the
existing `max(60, slowest measured module)` reservation.

## Deterministic replay of the P05 inventory

| Python | Shards | Node counts | Modeled loads (seconds) | Assignment SHA-256 |
|---|---:|---|---|---|
| 3.13 | 3 | 2576 / 2210 / 3033 | 551.317 / 551.317 / 551.317 | `ee8258ff022c307d405842b5240c05983856bc2b03ed05d05d78f7260c53acdf` |
| 3.13 | 4 | 1755 / 2001 / 2145 / 1918 | 413.488 / 413.488 / 413.487 / 413.488 | `8500a9cca74256ed036e2e75f89cc63eb921dac7494a17d950d2e75bd210ee94` |
| 3.14 | 3 | 2816 / 2355 / 2648 | 775.069 / 775.070 / 775.070 | `3605f619c815b9645e40e1d684fa67318456307caec37c28c54742ac33a56813` |
| 3.14 | 4 | 1810 / 1983 / 1678 / 2348 | 581.303 / 581.302 / 581.302 / 581.302 | `5d916c49038d03691b7e5c6ef067886659e4eb9073bbb8c822e7a9a2ee839009` |

Every module assignment matches the corresponding P05 archived plan exactly.
Four-shard results are deterministic offline replay only. The default remains
three shards; `CI_PR_SHARD_COUNT` is not changed. P05's 1,006–1,024-second
three-shard and 812–830-second four-shard envelopes are not hosted acceptance
results for this change.

The final candidate inventory, including the ten new activation regressions, has
7,937 nodes in 317 modules. `planning.json` records current-inventory digests,
node counts, conservative reservations and assignment digests. All four plans
were checked for a unique complete union and disjoint partitions. Current
three-shard counts are 3.13: **3057 / 2263 / 2617**, 3.14:
**2457 / 2049 / 3431**; four-shard counts are 3.13:
**1867 / 1635 / 1765 / 2670**, 3.14: **1699 / 2276 / 1947 / 2015**.
Both new safety modules are UNMEASURED, reserved at 93.554 seconds each on
3.13 and 187.925 seconds each on 3.14. P05's old E2E envelopes must not be
reused as predictions for this expanded inventory.

## Historical replay correction

The raw-evidence P05 analyzer previously loaded the mutable production file as
its V1 baseline and indexed top-level `module_seconds`. V2 activation would
therefore raise `KeyError` and invalidate historical raw replay. It now uses the
already archived `previous_module_timings.json`, as normalized replay already
did. The archived V1 document is semantically identical to this task's trusted
base V1 document. A regression exercises raw-mode baseline selection. This
change affects the historical analyzer only, not trusted CI execution or policy.

## Targeted regression evidence

Python 3.13.9: the existing trusted-plan, four-shard, validation-v2 and identity
suites passed **282 tests in 100.10 seconds**. Activation, risk-classification
and P05 analyzer suites passed **102 tests in 47.94 seconds**, for **384 passed**
in total, with no skips. Python 3.14's same seven suites passed **384 tests in
150.41 seconds**, with no skips, for **768 successful test executions** across
the two runtimes. These are local targeted tests, not hosted executions.
The first activation-only invocation was **9 passed / 1 failed** because a new
V1 replay assertion treated P05's `baseline` observation list as an assignment
map. The assertion now uses `shared["3"].existing_v1.assignments`; the subsequent
102-test run passed.

New regression coverage includes exact production provenance and bytes,
version-specific three/four-shard P05 replay, full 7,819-node-per-version
synthetic receipt aggregation under both shard counts, source/digest/table/cost
plan mutations, unknown-module reservations and archived V1 assignments.
Existing attack regressions cover mutable candidate weight injection, plan and
runtime identity mutations, missing/extra shards, counts, JUnit, duplicate
execution and missing/failing phases. The historical analyzer correction has a
raw-mode baseline-selection regression.

## Deployment boundary and rollback

The activation PR's trusted base still contains V1. Its hosted checks establish
candidate/control acceptance, not hosted V2 activation. The separate post-merge
requirement is a qualifying high-risk PR whose event base contains this change,
with archived V2 risk plan, version-correct receipts, complete independent
coverage aggregation, and protected App SUCCESS. Until that exists:
**V2_ACTIVE_PENDING / POST_MERGE_V2_NOT_VERIFIED**.

The PR remains draft and open for existing control review; it is not automatically
merged. Environment approvals remain with authorized GitHub reviewers. No
Ruleset, publisher, historical failure matrix, Full workflow or shard-count
variable is changed. Rollback is a separately reviewed restoration of the
production timing file from the baseline Git blob; it is not a runtime V2-to-V1
fallback, and plans already bound to their event base keep that identity.
