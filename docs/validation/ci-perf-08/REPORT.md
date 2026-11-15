# CI-PERF-08 — controlled four-shard acceptance

**Decision: NOT_YET_ACCEPTED. Recommendation: retain the default three shards pending reviewed CI-PERF-06 migration and controlled real evidence. NO LIVE MONEY.**

## Experiment boundary

This delivery prepares collection, receipt replay and performance analysis. It does not authorize or perform a repository-wide four-shard switch. Changes are confined to this evidence directory. CI-PERF-05 candidates and CI-PERF-06 trusted migration remain separate dependencies.

Before a real trial, record links and exact SHAs for all of the following:

| Prerequisite | Evidence required | Current disposition |
|---|---|---|
| CI-PERF-06 formally reviewed and on main | Control approval, merge commit, ancestry | Pending |
| Execution and aggregation use identical trusted weights/plans | Trusted base, file digest, per-version plan digests, successful replay | Pending post-migration run |
| Current Python 3.13/3.14 collection reconfirmed | Full inventories, historical classification, dependency/runtime identities | Pending post-migration run |
| Three/four-shard contracts pass | Exact source SHA and test result | Existing implementation; trial-specific confirmation pending |
| Repository-variable impact controlled | Active PR/run inventory, authorized window and restoration plan | No switch authorized |

Prefer a naturally occurring eligible high-risk PR. Add at most one dedicated four-shard trial only if necessary and separately authorized. Do not duplicate CI, reduce test scope, relax gates or increase shard count beyond four. Preserve failures, cancellations and results above 900 seconds. Subsequent natural observations may establish stability; a best single run is not a general guarantee.

## Reproduction

Run commands from a reviewed checkout with production timing JSON equal to the run's trusted base (checkout line-ending conversion is permitted); the recorded weight digest uses the trusted Git blob bytes. External evidence directories must be new and empty; do not run interpreters from inside them.

```text
python -I docs/validation/ci-perf-08/collect.py --run RUN_ID --attempt ATTEMPT --output NEW_EXTERNAL_DIRECTORY
python -I docs/validation/ci-perf-08/analyze.py --source EXTERNAL_DIRECTORY --output NEW_ANALYSIS_DIRECTORY --shards 3 --publisher-bot-id 354189164
python -I docs/validation/ci-perf-08/analyze.py --source FOUR_SHARD_DIRECTORY --output NEW_FOUR_ANALYSIS_DIRECTORY --shards 4 --publisher-bot-id 354189164 --baseline THREE_ANALYSIS_DIRECTORY/analysis.json
python -m pytest -q docs/validation/ci-perf-08/test_analyze.py docs/validation/ci-perf-03/test_profile.py
```

The protected workflow pins App ID `5670895`. Its observed status creator is `factorlab-ci-gate[bot]`, numeric bot user ID `354189164`; these are different identity namespaces. Independently reconfirm the publisher identity before a future acceptance review. The collector queries commit statuses for the run HEAD, not check runs, and retains all pages. The analyzer requires the run URL and publisher bot ID to match and selects the latest matching status. The analyzer also requires publication inside this attempt's successful merge-gate job interval. Formal review still establishes the protected App-to-bot relationship; a matching status alone is not control authorization.

`reference-43735048156/provenance.json` retains the source run/job/artifact/status metadata and file hashes; original downloaded evidence can be reproduced using the pinned run and attempt. The collector retains attempt-scoped jobs and run-wide artifact metadata. Duplicate artifact names or mixed receipt attempts block replay. It downloads original archives separately from extracted evidence. Artifacts are data, not executable analysis modules.

`analysis.json` includes full per-shard test node lists, exact validation identity, trusted weight digest, per-shard module-plan digest, JUnit testcase/suite times, wrapper and Actions step times, installation and pre-test job times, and risk-finish-to-job-start intervals. Existing `aggregate_pr` performs both-version exact coverage verification; uploaded aggregate receipts and JUnit are checked separately. The analysis is research evidence, not a new trusted gate, and deliberately cannot self-certify formal control review or financial admission.

`waterfall.csv` lists every executed job and timed step relative to workflow creation. Pre-start intervals combine dependency waiting, dispatch and runner queueing: public job timestamps do not establish pure runner queue time. Do not subtract parallel step durations as if they were a serial critical path. The final protected status timestamp, rather than workflow `updated_at`, ends the acceptance clock.

Runner compute seconds sum all executed job intervals. Modeled minutes round each job upward independently; they are not an account billing statement and apply no OS multiplier, discounts or free allowance. Obtain billing records separately if actual charged minutes are required. Comparison explicitly marks changed/unavailable weights or inventories as noncomparable and retains descriptive deltas rather than implying a controlled speedup.

## Real evidence and comparison

### Historical real three-shard reference

[Run 43735048156, attempt 1](https://github.com/Wind-Maker1001/FactorLab/actions/runs/43735048156) succeeded with all 14 jobs. It predates completion of the optimization/migration sequence and is **not** a post-all-merges baseline.

- HEAD: `0c5dfe89962fdf056f7f4eb66ab3a7fc8cd66f64`.
- Executed merge subject: `847e807d7a3257a8eef0d6d5ed2804d0df0b2c40`.
- Trusted base/event: `27180431626e6395dfb7907bab5e81f74cbb20fe`.
- Trusted weights Git-blob SHA-256: `de71ae1b832427adead95fb31d33b45e7dd13db6e90abd6625139af2ce1a191c`.
- Workflow created `2026-11-15T16:21:46Z`; protected success status `58968673960` published `2026-11-15T16:42:16Z` by bot user `354189164`.
- **End-to-end: 1,230 seconds; target exceeded by 330 seconds.** Workflow completion is 1,234 seconds after creation and is not substituted for the gate clock.
- Existing trusted aggregator replay passed for **7,811 tests per Python version**, disjoint and complete, all six shards successful. Uploaded aggregate identity/receipt/JUnit checks also passed. Exact nodes and plans are in `reference-43735048156/analysis.json`; original metadata and per-file provenance are in `provenance.json`.
- Financial receipt: 415 passed, zero failures/errors/skips; its legacy `trusted_base_sha` is null, recorded without rewriting it. Subject/event/HEAD/run/attempt match the current receipts. The control-review, Fast, both locking jobs, package and coverage jobs all succeeded. This historical outcome does not replace post-migration financial/history control review.
- Total job compute: **5,862 seconds**; modeled independently rounded job minutes: **107**. Actual billed minutes and a comparable four-shard delta remain unmeasured.

| Python / shard | JUnit testcase sum | Pytest suite | Wrapper | Actions test step | Install | Pre-test job | Risk finish → job start |
|---|---:|---:|---:|---:|---:|---:|---:|
| 3.13 / 0 | 563.569 | 576.913 | 581.930 | 583 | 63 | 81 | 1 |
| 3.13 / 1 | 605.677 | 626.002 | 630.828 | 633 | 87 | 111 | 1 |
| 3.13 / 2 | 587.144 | 601.980 | 605.796 | 607 | 72 | 91 | 1 |
| 3.14 / 0 | 790.383 | 807.740 | 812.685 | 819 | 93 | 117 | 2 |
| 3.14 / 1 | 427.246 | 442.666 | 446.435 | 448 | 73 | 94 | 1 |
| 3.14 / 2 | 1011.133 | 1031.555 | 1036.070 | 1038 | 86 | 115 | 3 |

All durations are seconds. Install is a component of pre-test job time, not an additional serial term. Risk-finish intervals include dispatch/queueing rather than pure queue time.

| Stage | Start offset | Duration | Finish offset |
|---|---:|---:|---:|
| Risk | 3 | 41 | 44 |
| Fast | 46 | 505 | 551 |
| Formal control review | 594 | 4 | 598 |
| Locking Ubuntu | 46 | 88 | 134 |
| Locking Windows | 46 | 124 | 170 |
| Package | 45 | 309 | 354 |
| Last current shard: 3.14 / 2 | 47 | 1160 | 1207 |
| Coverage aggregation | 1209 | 14 | 1223 |
| Merge-gate job | 1228 | 5 | 1233 |
| Protected success status | 1230 | — | 1230 |

The observed critical chain is initial dispatch (3), risk (41), shard dispatch (3), shard pre-test (115), current-test step (1038), shard tail (7), coverage dispatch (2), coverage (14), gate dispatch (5), and status publication (2): **1,230 seconds**. The 3.14/2 test step alone exceeds the full 900-second budget; splitting/rebalancing and the pending measured optimizations must be evaluated together. Other parallel jobs did not determine this run's finish. Full job/step waterfall data, including setup steps, is retained in `reference-43735048156/waterfall.csv`.

### Three/four-shard acceptance comparison

No post-CI-PERF-06 four-shard run has been accepted here. Earlier #175/#173 success must not be relabeled as a post-all-merges baseline.

| Measurement | Three shards | Four shards |
|---|---|---|
| Required successful current shards | 6 | 8 |
| Post-migration trusted full-collection evidence | Pending | Pending |
| Final protected App status and ≤900 s | Historical reference only | Not run |
| Actual cost delta | Requires comparable trial | Not available |

The historical #175 study predicted four independently balanced maxima of 598.43 s / 672.11 s for Python 3.13 / 3.14; these are testcase-sum projections, not new Runner results. It also modeled approximately 285.28 additional compute seconds and six rounded runner minutes. Neither projection establishes the end-to-end target after subsequent optimizations.

## Variable custody

Read-only observation on 2026-11-16: repository variable listing returned `total_count=0`; direct `CI_PR_SHARD_COUNT` lookup returned HTTP 404. The existing workflow defaults an unset shard variable to three. No value was written, so no restoration was necessary. This does not establish future or organization-level variable state.

Before a later authorized window, record the effective original value and source (unset versus explicit `3`), observation/approval time, active PRs and workflows in scope, intended change time, responsible controller, permitted run IDs and restoration deadline. A repository-level `CI_PR_SHARD_COUNT=4` is not branch-scoped. Restore the exact original state (delete if originally unset), record restoration time and read-back evidence, and account for runs dispatched during the window. Do not leave an ambiguous global configuration.

## Tool and contract verification

Python 3.12.7: **175 passed in 38.32 seconds** across the new analysis tests, existing P03 timing tests, `tests/security/test_four_shard_contract.py` and `tests/security/test_validation_v2.py`. Cases include full synthetic analyzer integration, missing/duplicate coverage, attempt/SHA/weight/runtime drift, skipped JUnit, incorrect publisher/run/status, 899/900/901-second status timing, cancelled jobs, per-job cost rounding and archive path safety. Synthetic fixtures establish parser/contract behavior only, not Runner performance.
## Final acceptance review

ACCEPTED requires a real successful four-shard Actions attempt, eight successful current receipts, complete nonhistorical collections on both Python versions, successful actual trusted coverage aggregation, all required controls and financial/history checks unchanged, and final protected App merge gate SUCCESS within 900 seconds of workflow creation. Also require the reviewed CI-PERF-06 migration and controlled-variable authorization record. Timing success without any one of these is NOT_YET_ACCEPTED.

For an over-budget trial, retain the measured excess, full waterfall, queue/dispatch observations, runner cost and critical-path explanation. Do not repeatedly rerun to erase a negative result.
