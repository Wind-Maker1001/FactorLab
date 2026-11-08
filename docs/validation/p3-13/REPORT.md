# P3-13: dynamic execution feedback

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

Baseline: `72571efeac498515a0765b6fa344ced296307daf` (`origin/main`).

## Design and evidence

`ResearchPipelineConfig.execution_mode` defaults to `batch`. The optional
`incremental_feedback` path creates one optimizer, ExecutionSession and
ExecutionFeedback per model. WalkForwardRunner remains responsible for training,
validation selection and OOS prediction. The score adapter remains nonnegative
percentile preference; it does not claim expected returns.

Each selected prediction date is a 15:00 local decision. Risk observations stop
at that date. ExecutionFeedback injects snapshot security weights and known
share-sale floors, then queues orders for the next session at 09:30. Every
benchmark session advances the same cash/FIFO book, including test-fold gaps;
days without signals submit no orders. No next-open fill is read by the prior
close decision. Explicit dated sale restrictions are filtered by knowledge time
and execution validity; infeasible restrictions or turnover budgets propagate an
OptimizationError without relaxation.

Decisions retain actual weights, previous targets, cash, NAV and estimated gross
security turnover. Execution results retain actual trades, fees, rejections,
positions, amount-based turnover and target deviation. ResearchEvaluation reads
these outputs directly. Experiment definitions hash mode, restriction evidence,
optimizer/turnover settings, factor/model/data/window/cost bindings and source
hashes for the pipeline, feedback, optimizer, session and execution engine.
Incremental experiment IDs have a mode suffix; batch IDs remain compatible.

## Deterministic acceptance

The new orchestration tests establish:

- Without a turnover limit, target weights and all execution fields match the
  batch engine. Signals are compared by column name. Incremental execution adds
  `gross_security_turnover`; `optimizer_turnover` is new holdings-aware telemetry,
  so identical telemetry is attached to batch replay for field-level comparison.
  There is no observed economic execution difference.
- Current marked security weights, not the preceding target, determine predicted
  gross turnover. Initial budget `.499` cannot fund investment `.5` and is rejected.
- A 100%-investment order with a CNY 5 fee fills 900 of 1000 shares; cash is 995,
  NAV 9995, and the next optimizer receives weight `9000/9995` rather than 1.
- A suspended outside-target old position persists; its liquidation contributes
  to the proposed turnover. Known locked outside-target holdings are infeasible.
- Future risk and next-open price perturbations leave earlier targets, decision
  receipts and instructions unchanged. Future sale evidence is not consulted.
- No-signal sessions preserve holdings, update marks and reconcile cash + market
  value = portfolio value and portfolio value / initial cash = NAV.

Existing execution-feedback tests cover T+1 sellability, integer lots, partial
cash fills, explicit share locks, suspension, rejected orders, future observations
and infeasible constraints. Existing pipeline tests cover all four models,
validation/train isolation, future-price prediction invariance, evaluation,
tracker reproduction and formal rejection of unqualified real data.

The related regression run passed 36 existing tests. The final new-test run
passed 7 tests. One four-model incremental end-to-end run used 24 securities,
MOM20/REV1 and three test folds, with turnover limit 0.8. Each model produced 1552
OOS predictions, 123 fills and 2 rejections. Minimum-variance allocation gives the
same portfolio across models despite different prediction ICs; that objective
uses covariance, not score magnitude. Final NAV was 0.9862016347695403 and cash
45752.942946311836 for each independent book. The run also asserted every cash,
market-value and NAV reconciliation, execution timing and proposed turnover.

`receipt.json` retains the compact raw summary, distinct experiment bindings,
source identities and artifact hashes; regenerable datasets/CSVs are not committed.

## Reproduction (Git Bash, repository root)

```sh
export PYTHONPATH=src
PY=/d/Environments/factorlab-py313/Scripts/python.exe
$PY -m pytest tests/research/test_pipeline_feedback.py tests/research/test_execution_feedback.py tests/research/test_research_pipeline.py -q
# Choose a fresh output directory; experiments and model artifacts are immutable.
$PY -m examples.research.research_pipeline_synthetic --output "$OUTPUT" --execution-mode incremental_feedback --turnover-limit 0.8
$PY -m examples.research.pipeline_feedback_receipt "$OUTPUT" "$RECEIPT"
```

`OUTPUT` is a caller-selected temporary or local research directory; `RECEIPT`
is the desired receipt JSON filename. No real data or financial admission is
required or conferred.

Outcome: **P313_ENGINEERING_ACCEPTED**. No unresolved economic execution
difference was observed in the exercised batch/incremental comparison; schema
additions above are intentional telemetry.
