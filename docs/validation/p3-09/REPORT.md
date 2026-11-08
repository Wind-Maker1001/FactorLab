# P3-09: synthetic signal propagation

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

## Experimental contract

Baseline: `e1ebe0dec9eb0b585972565167eff0c29a7f1d62`.

The experiment uses `make_synthetic_data(seed=20261014, stock_count=27, sessions=270)` and excludes its three deliberately gapped securities. The configuration follows the P3-07B static-factor fixture: MOM20/REV1, five-session next-open labels, fixed FactorScore weights and Ridge/LightGBM/XGBoost candidate sets. Factor selection is not performed in this experiment. Hyperparameters are chosen by the existing runner on Validation, never Test. All four models use one dataset and common walk-forward folds. The existing runner invokes the model factory; there is no separate model implementation.

All three allocation objectives use the complete prediction universe, 80% investment, 15% per-security cap, 40-session trailing close-known risk, 20% covariance shrinkage and five-prediction-session rebalancing. `equal` and `minimum_variance` are deliberately signal-independent controls. `score` uses the existing P3-07B transformation `rank(method='average', pct=True)`; these positive ranks are allocation preferences, not expected returns. Ties receive average ranks and symbols are ordered deterministically.

Targets retain decision dates. The existing execution engine advances them to the next trading session. Its `target_comparison.target_shares` is the rounded desired position, **not a submitted order quantity**: actual buy/sell demand depends on previous holdings. Trades and rejections are the authoritative execution records, including their requested-share quantities and partial-fill outcomes. Target weights and execution-time actual weights remain distinct.

Portfolio evaluation uses one complete execution calendar across sparse Test windows, retaining holdings through the intervening gaps; prediction evaluation uses only actual OOS prediction dates. This calendar convention is identical across all models and modes. Fees use the engine's default A-share schedule. The synthetic status fixture includes a suspension; rejection counts include all engine execution constraints.

## Transmission diagnostics

Each comparable event is one unordered model pair at a common decision date within a portfolio mode. Scores and target weights are aligned by security. Scores and NAV use absolute tolerance `1e-8`, with no relative tolerance; target differences use total absolute weight distance above `1e-8`. Ranking compares average cross-sectional ranks. Orders compare engine-rounded desired positions; trades compare net signed filled shares per security. Holdings and NAV are compared on that event's execution date, not on the signal date.

For each adjacent layer, the denominator is the count of events differing in the preceding layer. The numerator is the number of those events also differing in the following layer. A zero denominator produces JSON `null`, with both counts retained. These are purpose-defined software diagnostics, not standard financial statistics. The direct target-to-trades diagnostic also reports whether distinct targets survive execution constraints. Changes in levels can persist from previous events; the pairwise diagnostics are observational. Controlled score swaps with fixed risk and a common starting account provide the causal evidence.

Distinguish four claims: scores can be supplied to the optimizer; scores can alter target weights; those weights can alter actual fills; changed holdings can alter NAV. Equal/minimum-variance results need not differ when model predictions differ. Constant ranks and differences below board-lot sizes can also erase changes without a software defect. No assertion assumes shuffled predictions must worsen returns.

## Reproduction

Run from the task worktree in Git Bash, with a new output directory on each invocation:

```bash
PYTHONPATH=src D:/Environments/factorlab-py313/Scripts/python.exe -m examples.research.signal_propagation_synthetic --output <new-output-directory>
D:/Environments/factorlab-py313/Scripts/python.exe -m pytest -q tests/research/test_signal_propagation.py tests/research/test_portfolio_optimizer.py tests/research/test_portfolio_execution.py tests/research/test_research_evaluation.py tests/research/test_experiment_tracker.py tests/research/test_model_factory.py tests/research/test_walk_forward_runner_phase2.py
```

The CLI writes prediction/fit identities, rankings, decision receipts, targets, engine target comparisons, trades, rejections, holdings, NAV, ResearchEvaluation results and ExperimentTracker records. The checked-in evidence is a compact selection; full generated datasets and duplicate per-mode predictions are not archived. Synthetic RankIC and returns are software-test outputs, not evidence of real-market alpha.

## Evidence retention

`evidence/manifest.json` binds every originally committed evidence file by path, SHA256, byte size and line count, and inventories both original full runs. Removed generated tables remain retrievable verbatim from commit `99dafc24d6290164e33fadfef67559404173e660` using `git show <commit>:docs/validation/p3-09/evidence/<path>`. This is an existing published preservation location, not a replacement experiment.

`evidence/original-runs.json` preserves the original experiment definitions and run records for both accepted runs, including dependency versions, commands, factor identities, artifact hashes and code provenance. The actual runs report baseline commit `e1ebe0dec9eb0b585972565167eff0c29a7f1d62` with `git_dirty=true`; the published implementation commit must not be represented as their clean executed HEAD. The recorded diff hash identifies the dirty state but is not itself a saved source patch. Exact historical source reconstruction therefore remains qualified by that provenance.

Original aggregate results, accounting and reproduction receipts remain unchanged. Two score-mode model chains retain rankings, decisions, targets, rounded desired positions, trades, rejections, holdings and NAV; all four models retain fit audits, folds, decisions and NAV. The remaining repeated evaluation objects and generated tables are represented by their original identities rather than rewritten values. Synthetic outputs can be regenerated through the reproduction entry above; original acceptance receipts and provenance are historical records, not regenerable acceptance claims.

The latest P3-07B `prediction_targets()` is a separate research-pipeline adapter. P3-09 retains its local synthetic adapter; no cross-module refactor or business-code change is required. A merge-tree check against main `a33d8e323fc6c0313f907e022c9e5dc32bfe3147` found no file conflicts.

## Compaction verification

**P309_EVIDENCE_COMPACTED**. All tracker-bound artifacts in both original runs passed SHA256 verification. The 106 original checked-in evidence files are individually inventoried; 38 remain verbatim and 68 generated files are represented by their hashes and preserved original commit. Windows original bytes and normalized Git blobs have separate hash fields. Retained JSON/CSV parsing, report evidence references, historical retrieval hashes and reproduction-entry syntax passed. Seven P3-09 intervention tests passed again in 23.98 seconds. Business code is unchanged, so the 492-test historical regression receipt is retained without rerunning that suite.

`evidence/original-ci.json` is a snapshot for original HEAD `99dafc24d6290164e33fadfef67559404173e660`, not validation of the compaction commit. Package, both lock acceptance jobs and native lock diagnosis succeeded at capture; Current Software V2 and historical Legacy were still running. Historical Legacy failures are not automatically new regressions. New-HEAD CI must be evaluated separately before merge readiness. Main has no branch protection or repository rulesets at inspection.

## Acceptance

The four-model / three-objective experiment completed. Seven new deterministic intervention tests passed (25.38 seconds); the directly related regression suite passed 492 tests in the earlier combined run. That initial combined run also passed six new tests, with the remaining future-data intervention subsequently corrected and passed: realized future labels must be allowed to change, while prior decision scores and instructions must remain unchanged.

| Model | OOS RankIC | Score-mode final NAV | Fills | Costs | Cumulative daily turnover |
| --- | ---: | ---: | ---: | ---: | ---: |
| FactorScore | 0.083608 | 0.981999 | 188 | 1380.48 | 3.208672 |
| Ridge | 0.323998 | 1.025242 | 136 | 989.44 | 2.408305 |
| LightGBM | 0.305897 | 1.026796 | 137 | 974.94 | 2.304094 |
| XGBoost | 0.313322 | 1.026127 | 161 | 1179.62 | 2.835093 |

All score-mode runs recorded two rejections. All four equal-mode runs produced NAV 0.989443554, 35 fills and costs 230.70. All four minimum-variance runs produced NAV 0.986201635, 123 fills and costs 866.32. Different predictions are therefore correctly ignored by the two signal-independent objectives rather than mistaken for a propagation defect.

In score mode, all 78 model-pair/decision-date events differed in rankings and targets, and those differences were observable in rounded desired positions, filled shares, actual holdings and execution-date NAV. This 78/78 result describes this fixture only. The manual small-capital test separately proves that different targets can yield the same empty fills due to lot constraints. Manual score swaps prove causal weight/order/fill/NAV response with fixed risk and account state, including exact accounting after fees. Future-data perturbation rebuilds the dataset and reruns all four models, then checks prior scores, target instructions and trades.

**P309_ENGINEERING_ACCEPTED**. Two full runs produced exactly matching summaries and 139 exactly matching parsed CSV tables. All 12 portfolios reconciled sequential trade cash, final cash, daily holdings market value plus cash, and normalized NAV with absolute tolerance `1e-7`. Reproduction and accounting receipts are in `evidence/reproduction.json` and `evidence/accounting.json`.

The four layers are independently demonstrated: (1) model predictions are passed as nonnegative ranks into OptimizationInput; (2) score mode changes target weights while risk controls do not; (3) score-mode differences change actual fills, with lot/suspension effects separately tested; (4) changed holdings change NAV in the synthetic runs and the hand-calculable causal case. No model is selected for deployment from these Test results.
