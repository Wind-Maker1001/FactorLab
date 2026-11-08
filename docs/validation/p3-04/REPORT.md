# P3-04 engineering acceptance

Baseline: `d165c65a17c1ff4b7d174a063a6607589cd38522` (fetched main).
Branch: `feat/p3-04-experiment-tracker`.

The tracker is opt-in. Immutable checksummed JSON records remain the source of
truth; a local SQLite index can be reconstructed without training data or model
copies. Exclusive hard-link publication prevents duplicate-ID overwrites.
Queries validate source records, and comparisons require matching data, labels,
Test windows, splitter/cost assumptions, sample receipts and metric options.
Prediction and portfolio metrics occupy separate namespaces.

The Walk-forward adapter was exercised against the real Ridge runner on the
existing small `_dataset` fixture. No default runner, DatasetBuilder, model
factory or backtest behavior changes are required. Existing dataset sidecar
checksums and prediction evaluation are reused.

Validation uses the existing `D:/Environments/factorlab-py313` environment,
CPython 3.13.9. An initially discovered Python 3.14 environment lacked numpy and
pandas and failed collection; it was not used for acceptance. First 3.13 run:
27 passed, one Windows SQLite file-lock failure (`initial-tests.log`). The fix
closes connections explicitly after transactions. The related suite subsequently
passed 97 tests (`targeted-tests.log`). Final tracker-only validation is recorded
in `final-tracker-tests.log`: 16 passed in 5.29 seconds.

Engineering outcome: **P304_ENGINEERING_ACCEPTED**.

Reproduce from the repository root:

```text
D:/Environments/factorlab-py313/Scripts/python.exe -m pytest tests/research/test_experiment_tracker.py tests/research/test_prediction_metrics_phase2.py tests/research/test_walk_forward_runner_phase2.py tests/research/test_dataset_registry_v2.py -q
```

Storage is intended for a serialized local writer; writes and reconstruction
must not run concurrently. Hashes detect integrity changes, not malicious
re-signing. Artifact existence/checksums are verified only when explicitly
requested. No automatic historical training, central service, migration of old
research inventories or formal financial certification is provided.

The fresh checkout showed a pre-existing CRLF/LF-only difference in `demo.py`;
that file is excluded from this change. Commit identity is recorded by Git and
the Draft PR rather than embedding a self-referential commit hash in this file.

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED
