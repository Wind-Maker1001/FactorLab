# ExperimentTracker

An optional local recorder, independent of training and backtesting.

```python
from factorlab.experiments import ExperimentTracker

tracker = ExperimentTracker("research/experiment-store")
tracker.create_experiment("ridge-v1", {
    "model": "ridge", "parameters": {"alpha": 1.0},
    "factors": [{"factor_id": "REV1", "version": "1",
                 "definition_hash": factor_definition_hash}],
    "dataset": {"source": "synthetic", "sha256": dataset_hash},
    "label": {"name": "EXCESS_RET5", "definition_hash": label_hash},
    "windows": {"train": ["2020-01-01", "2020-04-30"],
                "validation": ["2020-05-01", "2020-06-30"],
                "test": ["2020-07-01", "2020-08-31"]},
    "walk_forward": {"purge_days": 5, "embargo_days": 5},
    "costs": {"mode": "NOT_APPLICABLE_PREDICTION"},
})
tracker.append_run("ridge-v1", "run-1", code=code_identity,
                   environment=environment, command=["python", "research.py"],
                   status="SUCCEEDED", qualification="SYNTHETIC",
                   prediction_metrics={"rank_ic": 0.02},
                   validation={"sample_hash": sample_hash,
                               "evaluation": evaluation_configuration})
rows = tracker.query(model="ridge", factor="REV1", status="SUCCEEDED")
receipt = tracker.reproduction("run-1", expected_code=code_identity)
```

`factorlab.experiments.provenance.capture_provenance(repo_root)` returns
`code` and `environment` keyword arguments, including commit/tree, dirty-worktree
fingerprint, Python/platform and installed distribution versions. Explicit null
commit/tree fields represent unavailable code identities.

Experiment definitions bind parameters, pinned factors, data/source identity,
labels, windows, splitter configuration and costs. Unknown windows are `None`.
Use full semantic label definitions and explicit zero-cost assumptions, not an
empty object. Multiple independent runs can reference one immutable experiment.
IDs are ASCII alphanumeric with hyphens/underscores, at most 128 characters.
Duplicate IDs fail; records cannot be updated through this API.

`append_run` accepts execution/test states `NOT_RUN`, `UNKNOWN`, `RUNNING`,
`SUCCEEDED`, `FAILED`. Execution and test status are independent. Missing metrics
are absent or null, never invented zeros. Portfolio metrics must describe an
actually executed portfolio simulation, separately from prediction metrics.
`SYNTHETIC`, `REAL_CANDIDATE`, `UNKNOWN` describe evidence provenance; no local
record or supplied receipt can grant `VERIFIED` or formal financial admission.

`walk_forward_metadata(result, top_k=..., minimum_cross_section=...)` from
`factorlab.experiments.adapters` returns keyword arguments for `append_run`:
existing prediction metrics, fold/selection audit metadata, sample identity,
evaluation options and model paths. Supply input identities and actual splitter
configuration independently. It neither trains models nor copies predictions.

`compare([run_id, ...])` returns report-ready JSON rows, not rankings. It rejects
unsuccessful runs, unknown identities, different datasets, labels, test windows,
splitter/cost assumptions, evaluated samples or metric configurations.

`reproduction` returns the definition, run environment, code identity, command
and validation summary without executing anything. Optional `expected_code`
and `expected_dataset` require exact equality. `verify_artifacts=True` requires
absolute paths and SHA-256 receipts for every referenced artifact and checks
current bytes. Hashes are integrity checks, not trusted attestations.

Authoritative records live in `experiments/*.json` and `runs/*.json`, with
checksummed envelopes and exclusive atomic publication. `index.sqlite3` is a
standard-library SQLite metadata index; no server is needed. Keep all JSON
records when backing up. `rebuild_index()` validates every record and binding
before transactionally replacing the index. Invalid input aborts reconstruction.
If publication succeeds but indexing fails, the record remains authoritative;
repair the index with `rebuild_index()`, not by reusing the ID. Queries use the
index to discover runs and read/validate their authoritative JSON records.
Use a single writer or externally serialize writes and rebuilds for a store.
External research files remain external and are never deleted by the tracker.

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED
