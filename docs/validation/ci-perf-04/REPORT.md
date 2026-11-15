# CI-PERF-04 — trusted four-shard PR trial

## Status and activation

Engineering supports Python 3.13 and 3.14 with three or four independent module partitions per version. Default PR execution remains **three shards per version**. Four-shard production activation and the **900-second trigger-to-trusted-status objective are pending real four-shard evidence**.

Base revision: `ac3505faa13f2cc9533fb13ba16551c4d521cd36`.

A repository administrator can explicitly select a trial by setting the repository Actions variable `CI_PR_SHARD_COUNT=4`; absent configuration selects `3`, and other values fail before candidate execution. This selects the existing high-risk Extended Software matrix only. Fast risk selection, Full CI, protected environments and the official publisher App remain in place. This change does not set the variable. Restore `3` or remove it to end a trial. Configuration is repository-wide: coordinate the trial window before setting it.

The updated trusted workflow and verifier must first pass the existing `ci-control-review` process and be available on the trusted base. A PR's modified verifier is not substituted for that base. After approval, use a high-risk PR with an exact event-bound merge subject, collect all eight artifacts, and record run ID/attempt, HEAD/base/subject/event and protected App status timestamps. A successful run of the old base workflow is not four-shard acceptance. Do not enable four shards by default until that evidence and the performance budget justify it.

## Coverage and trust

`partition`, `module_plan`, `verify_execution`, `aggregate` and `aggregate_pr` remain the sole partition and coverage machinery. API/CLI defaults remain three; `--shard-count 4` and pytest's `--current-shard-count 4` select four. The old three-folder Full CLI and three-shard coverage schema remain supported.

Four-shard aggregation independently rebalances the full nonhistorical inventory, requires unique module ownership and nonempty shards, and compares every started/completed node and JUnit PASS with that selection. It validates receipt shard/count metadata against actual results. PR aggregation requires exact expected shard directories, shared full collections across both Python versions, unchanged frozen inputs, dependency evidence and exact HEAD/base/subject/event/run/attempt binding. Missing files, duplicate tests, failures, skips, cancellations and identity drift fail the coverage job. The existing gate requires that job's success.

Only the existing `ci-merge-publisher` environment obtains the App token; candidate test jobs gain no status-write permission. No Ruleset, protected check, timeout, release flow or dependency cache write policy changes are made. No financial or trading code changes.

## Verification evidence

- `synthetic-receipts.json` contains eight clearly labelled synthetic input artifacts and their verified two-version outputs, generated through `aggregate_pr(..., shard_count=4)`. These exercise the real aggregator but represent four synthetic tests per version, **not actual repository tests or GitHub Runner execution**.
- Tests extend the existing negative-case matrix to both counts: old Full CLI, collection-before-sharding, disjoint exhaustive module partitions, failed/missing/cancelled shards, duplicate execution, receipt/dependency/input tampering, cross-version/runtime/run/attempt identity, JUnit failures and missing observations. Additional cases cover skips, missing completion, extra directories, receipt counts and cross-version full-collection drift.
- Final sharding, trial, PR risk and immutable identity contract suite: **245 passed** in 115.86 seconds. The committed synthetic receipt replay additionally passed (1 test, 0.38 seconds).
- Local complete security suite on final sources: **315 passed** in 372.47 seconds, no failures or skips. Command: `python -m pytest tests/security -q -o addopts= --basetemp=local-ci/p04-security-release --junitxml=local-ci/p04-security-release.xml`. Local runtime: Python 3.12; this is offline security verification, not either target GitHub runtime.
- `actionlint` 1.7.12: **PASS** for PR and unchanged Full workflows. Initial execution caught a malformed YAML printf literal; it was corrected before the successful lint execution. Shellcheck is disabled because it is not installed in this local runtime.
- An initial pytest invocation failed because its explicit temporary directory's parent was absent; creating the evidence parent corrected that setup issue. An intermediate complete run reported 290 passed / 4 failed, all four from the YAML syntax issue above. The next complete run reported 312 passed / 1 failed against an already-loaded static-three-shard matrix assertion; that assertion was updated for the trusted parameterized matrix and passed in the subsequent 245-test contract suite. A complete final-state rerun follows those fixes.

## Bounded performance model

`projection.json` records exact module assignments and weights. The existing timing source is run [42508208514](https://github.com/Wind-Maker1001/FactorLab/actions/runs/42508208514), artifact `extended-3.13`, subject `df3a0e054b8f60bf21b15e682ef8900b1964849e`, Python 3.13.17. It contains 189 measured modules. No P01/P02/P03 evidence or matching performance branches were available in the fetched repository and GitHub PR search at this inspection; P03 calibration remains required before a final performance conclusion.

| Projection | Three-shard maximum | Four-shard maximum | Budget left out of 900 seconds |
| --- | ---: | ---: | ---: |
| Historical measured modules only | 725.058 s | 543.793 s | 356.207 s |
| Current candidate module envelope | 10813.227 s | 8109.921 s | −7209.921 s |

The first row predicts a 25% test-load critical-path reduction (181.265 seconds), not end-to-end acceptance. The current tracked-file candidate envelope has 336 modules, including 147 without weights. It reserves 205.881 seconds for each unknown module using the existing conservative estimator. This is a static candidate envelope, **not a collected inventory or a certified upper bound on actual runtime**. Module membership and real weights must be measured; the stale measured subset cannot justify activation. Python 3.14 is not independently calibrated by these weights.

### Actual three-shard reference

`reference-run.json` preserves job/step timestamps from [43721499381](https://github.com/Wind-Maker1001/FactorLab/actions/runs/43721499381), an existing successful `pull_request_target` run, not a P04 execution. Its workflow envelope is 1405 seconds. The App publisher job completes 1404 seconds after trigger; exact status publication is within that job rather than proven by the workflow completion timestamp.

The critical Python 3.14 shard takes 1253 seconds: test-command step 1135 seconds, installation 89 seconds and remaining job overhead 29 seconds. Risk selection takes 119 seconds, including 104 seconds of risk analysis. Coverage aggregation takes 15 seconds and publisher execution 7 seconds, with inter-job scheduling adding more time. Fast takes 486 seconds, packaging 307 seconds, and locking 101–106 seconds, all parallel after risk.

A deliberately optimistic 3/4 scaling of that critical test step gives 851.25 seconds. Adding the observed non-test critical-path allowance (about 269 seconds through publisher completion) gives **about 1120 seconds**, still above 900. This is a sensitivity calculation, not a forecast based on real four-shard assignments. At that overhead, the test-command budget is about **631 seconds**. Four shards alone therefore do not establish the target: P01/P02 improvements to risk/dependency/critical-path work and P03 current measured balancing must be incorporated.

### Runner cost

Extended Windows job count rises from 6 to 8 (+33.3%). Total useful test work is retained; each additional job repeats collection, checkout, setup, installation and artifact handling. If the reference critical shard's 118-second non-test overhead were representative, the two additional jobs add about 236 Windows runner-seconds (3.93 raw runner-minutes), before repeated collection overhead, billing rounding and actual per-version variation. This is not a currency quote. More concurrency can also increase queue time; measure all eight job start timestamps during acceptance. Full CI job count is unchanged.

No new cache is needed for correctness. A later trusted-main cache producer with restore-only PR consumers could reduce installation time, but must be evaluated independently; no PR can populate a main-reusable cache through this change. Never cache trading state, ledgers, credentials or external datasets.

## Real four-shard acceptance

**NOT RUN / NOT ACCEPTED.** Required evidence is both versions' complete nonhistorical PASS coverage, eight verified receipts from one immutable run/attempt, successful formal control review and official trusted status, and trigger-to-status time ≤900 seconds. Preserve slower results and analyze the remaining critical path instead of weakening coverage, reducing timeout or changing gate requirements.

NO LIVE MONEY.
