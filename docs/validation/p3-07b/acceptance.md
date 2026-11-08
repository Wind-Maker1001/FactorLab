# P3-07B engineering acceptance

P307B_ENGINEERING_ACCEPTED

Baseline: `5646890dda069c90c707d3f83265a73957b848ad`.
Runtime: existing `D:/Environments/factorlab-py313/Scripts/python.exe`.

The synthetic chain uses DataHub version-pinned queries, Registry V2 MOM20/REV1,
DatasetBuilder chunked partitions, WalkForwardRunner.run_partitioned and its
existing model factory, train-only selection/directions, PortfolioOptimizer,
AShareExecutionBacktester.run_target_weights, ResearchEvaluation and
ExperimentTracker. Governance remains an optional selected DataHub adapter;
archiving original supplier evidence is not recreated inside the pipeline.

The example has 24 securities, 270 generated business-day sessions, 65 pre-research
warmup sessions, a five-session label horizon and three expanding Test folds.
Every model produced 1,552 OOS prediction rows. Dynamic selection is frozen from
Train; hyperparameters use Validation, not Test. Purge/embargo are five sessions.
Each allocation uses at most 40 trailing observed raw-close returns available at
the signal close, then executes at the next benchmark-session open.

| Model | OOS RankIC | Executed trades | Rejections |
|---|---:|---:|---:|
| FactorScore | 0.3204645789 | 123 | 2 |
| Ridge | 0.3239975676 | 123 | 2 |
| LightGBM | 0.3080240711 | 123 | 2 |
| XGBoost | 0.3113658965 | 123 | 2 |

The predeclared minimum-variance arm invests 80% with a 15% single-security cap
and 20% diagonal shrinkage. It is risk-only: different model scores need not
produce different minimum-variance targets. Equal and nonnegative percentile-score
allocation are separately exercised through the same optimizer adapter. Scores
are never supplied as expected returns.

Each model ended at NAV 0.9862016348 and cash CNY 45,752.9429463. These are
synthetic software outputs, not historical strategy returns. All 295 target
comparison rows per model retain nonzero execution deviations. Maximum absolute
cash + marked positions versus NAV-capital reconciliation error is
4.37e-11 CNY. Tests also reconcile cumulative buys, sells and actual costs to cash,
verify integer shares/nonnegative cash, and pass actual execution NAV/trades to
the evaluation engine. Missing/uncomputable metrics preserve their reasons.

Eight new tests passed in 17.40 seconds. The affected existing contract suites
passed 93 tests in 40.77 seconds (partitioned Walk-forward, optimizer, execution,
research evaluation and experiment tracking). The future-perturbation test changes
all future raw/adjusted OHLC values and proves earlier Ridge scores, model/risk
identities, target weights and execution instructions are unchanged. A separate
three-objective test proves future risk observations do not affect allocation.
Real-candidate input without qualification is rejected by existing admission.
The first integration attempts exposed a dynamic-weighting option mismatch, an
unsupported tree option and the execution-day evaluation origin; these were
corrected in the adapter/example, without changing domain implementations.

The one small end-to-end measurement was 11.235 seconds. No large benchmark,
legacy-wide run, second Python matrix or independent review was run.

Four immutable experiments/runs were queried and compared. Index reconstruction
and artifact-backed reproduction lookup pass. `synthetic-result.json` contains
the run summary, data identity, reproduction receipt and reconciliation evidence;
`pipeline-tests.xml` is the targeted test receipt. Full execution artifacts and authoritative ExperimentTracker JSON records are
preserved in `evidence/`; the SQLite index is rebuildable. The original local
artifact paths remain in the records to preserve the observed run identity.
Dataset partitions are regenerable and excluded from the evidence bundle. The run's code identity explicitly records the baseline commit
and dirty-worktree fingerprint of the implementation under test.

## Reproduce

From the repository root in Git Bash, choose an unused output directory:

```sh
PYTHONPATH=src D:/Environments/factorlab-py313/Scripts/python.exe \
  -m examples.research.research_pipeline_synthetic --output research/p307b-reproduction
PYTHONPATH=src D:/Environments/factorlab-py313/Scripts/python.exe \
  -m pytest tests/research/test_research_pipeline.py -q
```

## Deliberately unintegrated inputs

- TODO: add explicit historically available expected-return inputs before enabling mean-variance.
- TODO: wire actual incremental execution holdings before enabling optimizer turnover limits.
- TODO: bind PIT industry/classification inputs for industry-constrained optimization.
- TODO: integrate qualified corporate-action execution input when available; no dividends or adjustments are invented.
- Formal real-data research remains subject to the unchanged qualification gate.

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED
