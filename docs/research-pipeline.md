# Research pipeline

`factorlab.application.research_pipeline` exposes `ResearchPipeline`,
`ResearchPipelineConfig`, `ResearchPipelineResult` and `HubQuery`.

```python
from factorlab.application.research_pipeline import ResearchPipeline

result = ResearchPipeline(config).run(
    hub, tracker=tracker, repo_root=repo_root,
    command=["python", "-m", "my_research", "--config", "pinned.json"],
    qualification_provider=provider,
)
```

The caller supplies a V2 `DatasetBuilder`, an existing expanding splitter,
predeclared model options, optimizer/execution configurations, and six query
groups: bars, benchmark, membership, status, listings and calendar. Per-security
queries are tuples; each pins a source and exact returned version. Configure
warmup through query bounds before `research_start`; retain enough calendar
beyond `research_end` for labels and next-open execution. DatasetBuilder retains
its completeness and label rules. `models` options are passed directly to
WalkForwardRunner, including its train-only selector and dynamic directions.
No model is chosen from Test performance.

Bind governed sources with `GovernedTableAdapter` in the supplied DataHub;
archive acquisition remains separate, using the original source evidence.
The pipeline neither creates vintages from arbitrary frames nor changes their
historical-knowledge restrictions. `REAL_CANDIDATE` invokes the existing formal
market admission function with execution qualification and exact input tables.
`SYNTHETIC` requires synthetic provenance on every provider response. Neither
mode grants financial admission.

`prediction_targets` accepts completed OOS predictions and historical returns.
Equal, nonnegative percentile-score and minimum-variance allocations use the
existing optimizer. Every risk window ends no later than the signal session
close. Percentile scores are not expected returns. Mean-variance is rejected
until a separate historical expected-return contract is integrated. Turnover
constraints are rejected until actual incremental execution holdings can be
supplied; previous targets are not holdings. Industry constraints require a
separate point-in-time classification input and are not currently wired.

Raw execution prices are not manufactured from adjusted prices. The execution
engine applies its existing fees, slippage, integer-share rules and restrictions.
No corporate-action ledger is synthesized. Results retain predictions in memory,
targets, optimizer receipts, execution ledgers and ResearchEvaluation results.
Only compact diagnostics, execution ledgers and evaluation reports are persisted;
training models and duplicate prediction matrices are not saved. Partitioned
feature data are regenerable and live under the selected output directory.

ExperimentTracker stores immutable definitions and JSON run records plus its
rebuildable SQLite index. The run binds source envelopes, V2 factor identities,
label definition, splitter, model/portfolio/execution settings, code/environment,
metrics, artifact paths and reproduction command. Use a fresh output directory
and experiment ID for another run. Comparisons reject mismatched observation,
fold, benchmark or cost identities rather than ranking a winner.

Synthetic example (from repository root, using the installed project or
`PYTHONPATH=src`):

```sh
python -m examples.research.research_pipeline_synthetic --output research/p307b-demo
```

The generated prices are software fixtures, not historical strategy evidence.

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED
