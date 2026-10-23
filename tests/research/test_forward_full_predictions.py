"""Full forward scores are contemporaneous immutable evidence, not replay.

All inputs here are deliberately synthetic local test fixtures. Tests never
activate a real experiment, obtain PIT data, fit a model or contact a network.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import hashlib
import io
import json

import numpy as np
import pandas as pd
import pytest

from qbot.research.forward import signal_recorder as module
from qbot.research.forward.signal_recorder import (
    FORWARD_PREDICTION_COLUMNS, FULL_ELIGIBLE_CROSS_SECTION, TOP_K_ONLY,
    SignalRecorder, SignalValidationError, SignalVersionConflictError,
    finalize_signal_record, load_prediction_record, load_signal_record, record_signals,
)


DAY = date(2026, 9, 24)


def _predictions():
    return pd.DataFrame({
        "symbol": ["600003.SH", "000001.SZ", "600000.SH", "600001.SH", "000002.SZ", "600002.SH"],
        "score": [-0.2, 0.9, 0.8, 0.6, 0.8, 0.2],
        "rank": [6, 1, 3, 4, 2, 5],
    })


def _signals():
    frame = _predictions().sort_values("rank").head(2).iloc[::-1].copy()
    frame["target_weight"] = 0.5
    return frame


def _kwargs(**updates):
    values = {
        "signal_date": DAY, "data_asof": DAY,
        "generated_at": "2026-09-24T16:10:00+08:00",
        "model_name": "ridge", "model_version": "test-only-synthetic-v1",
        "model_bundle_sha256": "a" * 64, "dataset_sha256": "b" * 64,
        "git_commit": "c" * 40, "run_id": "synthetic-forward-run",
        "universe_version": "d" * 64,
        "activation_id": "2026-09-24-eeeeeeeeeeeeeeee",
        "activation_manifest_sha256": "f" * 64,
        "research_contract_sha256": "e" * 64, "source_sha256": "1" * 64,
        "timeliness": "ON_TIME",
    }
    values.update(updates)
    return values


def _recorder(tmp_path):
    return SignalRecorder(tmp_path / "synthetic-signals", activation_date=DAY,
                          lock_timeout=10.0, lock_poll_interval=0.001)


def _record(tmp_path, **updates):
    return _recorder(tmp_path).record(
        _signals(), predictions=_predictions(), cross_section_size=6, **_kwargs(**updates)
    )


def _producer(tmp_path, record, **updates):
    manifest = json.loads(record.manifest_path.read_text(encoding="utf-8"))
    payload = {
        "run_id": manifest["run_id"], "command": "forward-signal", "status": "success",
        "completed_at": "2026-09-24T08:20:00+00:00", "signal_sha256": record.sha256,
        "signal_manifest_sha256": record.manifest_sha256, "signal_rows": record.row_count,
        "prediction_sha256": record.predictions_sha256,
        "prediction_rows": record.prediction_row_count,
        "outputs": {
            "forward_signals": str(record.signals_path),
            "forward_signal_manifest": str(record.manifest_path),
            "forward_predictions": str(record.predictions_path),
        },
    }
    payload.update(updates)
    path = tmp_path / (record.version + "-producer.json")
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _write_manifest(record, mutate):
    manifest = json.loads(record.manifest_path.read_text(encoding="utf-8"))
    mutate(manifest)
    content = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    record.manifest_path.write_bytes(content)
    (record.directory / "manifest.sha256").write_text(hashlib.sha256(content).hexdigest() + "\n", encoding="ascii")


def _rewrite_predictions(record, mutate):
    frame = pd.read_csv(io.BytesIO(record.predictions_path.read_bytes()),
                        keep_default_na=False, float_precision="round_trip")
    mutate(frame)
    content = frame.to_csv(index=False, lineterminator="\n", float_format="%.17g").encode("utf-8")
    record.predictions_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()

    def update_manifest(manifest):
        manifest["prediction_sha256"] = digest
        manifest["files"]["predictions.csv"] = {"sha256": digest, "bytes": len(content)}

    _write_manifest(record, update_manifest)


def test_full_prediction_publication_and_reading_are_canonical_and_hash_bound(tmp_path):
    record = _record(tmp_path)
    manifest = json.loads(record.manifest_path.read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert manifest["prediction_scope"] == FULL_ELIGIBLE_CROSS_SECTION
    assert manifest["cross_section_size"] == manifest["prediction_row_count"] == 6
    assert manifest["predictions_file"] == "predictions.csv"
    assert manifest["prediction_columns"] == list(FORWARD_PREDICTION_COLUMNS)
    assert set(manifest["files"]) == {"signals.csv", "predictions.csv"}
    content = record.predictions_path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == record.predictions_sha256 == manifest["prediction_sha256"]
    assert manifest["files"]["predictions.csv"] == {"bytes": len(content), "sha256": record.predictions_sha256}
    full = load_prediction_record(record.directory, require_completion=False)
    assert tuple(full.columns) == FORWARD_PREDICTION_COLUMNS
    assert "target_weight" not in full.columns
    assert full["symbol"].tolist() == ["000001.SZ", "000002.SZ", "600000.SH", "600001.SH", "600002.SH", "600003.SH"]
    assert full["rank"].tolist() == list(range(1, 7))
    assert full["run_id"].tolist() == ["synthetic-forward-run"] * 6
    loaded = load_signal_record(record.directory, require_completion=False)
    assert loaded.predictions_path == record.predictions_path
    assert loaded.prediction_row_count == 6
    assert loaded.predictions_sha256 == record.predictions_sha256
    assert loaded.prediction_scope == FULL_ELIGIBLE_CROSS_SECTION
    copy = loaded.predictions
    copy.loc[0, "score"] = -999.0
    assert loaded.predictions["score"].iloc[0] == 0.9


def test_full_getter_requires_successful_completion_and_binds_producer(tmp_path):
    record = _record(tmp_path)
    with pytest.raises(SignalValidationError, match="completion receipt"):
        load_prediction_record(record.directory)
    completed = finalize_signal_record(record.directory, _producer(tmp_path, record))
    assert completed.completion_path.exists()
    full = load_prediction_record(record.directory)
    assert len(full) == 6
    assert completed.predictions_sha256 == record.predictions_sha256


@pytest.mark.parametrize("updates", [
    {"status": "failed"}, {"prediction_sha256": "0" * 64},
    {"prediction_rows": 5}, {"prediction_rows": True}, {"prediction_rows": "6"},
    {"outputs": {"forward_predictions": "wrong-path"}},
])
def test_failed_or_inconsistent_producer_cannot_authorize_full_evidence(tmp_path, updates):
    record = _record(tmp_path)
    with pytest.raises(SignalValidationError, match="authorize"):
        finalize_signal_record(record.directory, _producer(tmp_path, record, **updates))
    assert not record.completion_path.exists()


def test_wrapper_supports_exact_same_full_predictions_api(tmp_path):
    record = record_signals(
        _signals(), tmp_path / "signals", activation_date=DAY,
        predictions=_predictions(), cross_section_size=6, **_kwargs(),
    )
    assert record.prediction_row_count == 6
    assert len(load_prediction_record(record.directory, require_completion=False)) == 6


@pytest.mark.parametrize("size", [None, 0, -1, True, False, 6.0, "6", 5, 7])
def test_expected_cross_section_size_is_mandatory_exact_integer_not_verified_flag(tmp_path, size):
    with pytest.raises(SignalValidationError, match="cross_section_size"):
        _recorder(tmp_path).record(_signals(), predictions=_predictions(),
                                  cross_section_size=size, **_kwargs())
    assert not (tmp_path / "synthetic-signals").exists()


def test_cross_section_size_without_predictions_is_invalid(tmp_path):
    with pytest.raises(SignalValidationError, match="requires full predictions"):
        _recorder(tmp_path).record(_signals(), cross_section_size=6, **_kwargs())


@pytest.mark.parametrize("mutate,match", [
    (lambda frame: frame.__setitem__("symbol", ["600003.sh", "000001.SZ", "600000.SH", "600001.SH", "000002.SZ", "600002.SH"]), "canonical"),
    (lambda frame: frame.__setitem__("symbol", ["600003.SH", "000001.SZ", "600000.SH", "600001.SH", "000002.SZ", "000002.SZ"]), "unique"),
    (lambda frame: frame.__setitem__("score", [np.nan, 0.9, 0.8, 0.6, 0.8, 0.2]), "finite"),
    (lambda frame: frame.__setitem__("score", [np.inf, 0.9, 0.8, 0.6, 0.8, 0.2]), "finite"),
    (lambda frame: frame.__setitem__("score", [True, 0.9, 0.8, 0.6, 0.8, 0.2]), "real number"),
    (lambda frame: frame.__setitem__("rank", [6, 1, 3, 4, 2, 4]), "unique"),
    (lambda frame: frame.__setitem__("rank", [7, 1, 3, 4, 2, 5]), "contiguous"),
    (lambda frame: frame.__setitem__("rank", [6.0, 1.0, 3.0, 4.0, 2.0, 5.0]), "integer"),
    (lambda frame: frame.__setitem__("rank", [6, 1, 2, 4, 3, 5]), "tie breaking"),
    (lambda frame: frame.__setitem__("signal_date", "2026-09-25"), "metadata"),
    (lambda frame: frame.__setitem__("run_id", "wrong-run"), "metadata"),
    (lambda frame: frame.__setitem__("model_bundle_sha256", "2" * 64), "metadata"),
])
def test_full_predictions_fail_fast_before_publication(tmp_path, mutate, match):
    predictions = _predictions()
    mutate(predictions)
    with pytest.raises(SignalValidationError, match=match):
        _recorder(tmp_path).record(_signals(), predictions=predictions,
                                  cross_section_size=6, **_kwargs())
    assert not (tmp_path / "synthetic-signals").exists()


@pytest.mark.parametrize("kind", ["score", "symbol", "bottom_selected"])
def test_signals_must_be_exact_prefix_not_post_hoc_selected_subset(tmp_path, kind):
    signals = _signals()
    if kind == "score":
        signals.loc[signals["rank"] == 1, "score"] = 0.91
    elif kind == "symbol":
        signals.loc[signals["rank"] == 1, "symbol"] = "000003.SZ"
    else:
        signals = _predictions().sort_values("rank").tail(2).copy()
        signals["rank"] = [1, 2]
        signals["target_weight"] = 0.5
    with pytest.raises(SignalValidationError, match="exactly match"):
        _recorder(tmp_path).record(signals, predictions=_predictions(), cross_section_size=6, **_kwargs())


def test_missing_and_duplicate_prediction_columns_rejected(tmp_path):
    missing = _predictions().drop(columns=["rank"])
    with pytest.raises(SignalValidationError, match="missing required"):
        _recorder(tmp_path).record(_signals(), predictions=missing, cross_section_size=6, **_kwargs())
    duplicated = pd.concat([_predictions(), _predictions()[["score"]]], axis=1)
    with pytest.raises(SignalValidationError, match="duplicate column"):
        _recorder(tmp_path).record(_signals(), predictions=duplicated, cross_section_size=6, **_kwargs())


def test_raw_prediction_csv_change_and_missing_file_are_fatal(tmp_path):
    changed = _record(tmp_path / "changed")
    changed.predictions_path.write_bytes(changed.predictions_path.read_bytes() + b"\n")
    with pytest.raises(SignalValidationError, match="byte count"):
        load_prediction_record(changed.directory, require_completion=False)
    missing = _record(tmp_path / "missing")
    missing.predictions_path.unlink()
    with pytest.raises(SignalValidationError, match="schema-declared files"):
        load_signal_record(missing.directory, require_completion=False)


@pytest.mark.parametrize("field,value,match", [
    ("prediction_scope", True, "prediction_scope"),
    ("prediction_scope", "TOP_K_ONLY", "prediction_scope"),
    ("cross_section_size", True, "positive integer"),
    ("cross_section_size", "6", "positive integer"),
    ("cross_section_size", 5, "equal cross_section_size"),
    ("prediction_row_count", True, "positive integer"),
    ("prediction_row_count", 5, "equal cross_section_size"),
    ("predictions_file", "../predictions.csv", "predictions_file"),
    ("prediction_columns", ["symbol", "score", "rank"], "prediction columns"),
    ("prediction_sha256", "2" * 64, "digests disagree"),
    ("manifest_version", True, "schema is invalid"),
])
def test_rehashed_manifest_still_requires_strict_schema_and_full_contract(tmp_path, field, value, match):
    record = _record(tmp_path)
    _write_manifest(record, lambda manifest: manifest.__setitem__(field, value))
    with pytest.raises(SignalValidationError, match=match):
        load_prediction_record(record.directory, require_completion=False)


@pytest.mark.parametrize("mutate,match", [
    (lambda frame: frame.loc.__setitem__((0, "run_id"), "other-run"), "metadata"),
    (lambda frame: frame.loc.__setitem__((0, "score"), 0.91), "exactly match"),
    (lambda frame: frame.loc.__setitem__((5, "rank"), 7), "contiguous"),
    (lambda frame: frame.loc.__setitem__((5, "symbol"), "600003.sh"), "canonical"),
])
def test_consistently_rehashed_prediction_rows_cannot_violate_metadata_or_prefix(tmp_path, mutate, match):
    record = _record(tmp_path)
    _rewrite_predictions(record, mutate)
    with pytest.raises(SignalValidationError, match=match):
        load_prediction_record(record.directory, require_completion=False)


def test_completion_manifest_hash_prevents_post_completion_rehashed_prediction_revision(tmp_path):
    record = _record(tmp_path)
    finalize_signal_record(record.directory, _producer(tmp_path, record))
    _rewrite_predictions(record, lambda frame: frame.loc.__setitem__((5, "score"), -0.3))
    with pytest.raises(SignalValidationError, match="completion receipt differs"):
        load_prediction_record(record.directory)


def test_legacy_top_k_records_are_explicit_and_never_gain_full_cross_section_evidence(tmp_path):
    recorder = _recorder(tmp_path)
    legacy = recorder.record(_signals(), **_kwargs())
    manifest = json.loads(legacy.manifest_path.read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 2
    assert manifest["prediction_scope"] == TOP_K_ONLY
    loaded = load_signal_record(legacy.directory, require_completion=False)
    assert loaded.prediction_scope == TOP_K_ONLY
    assert loaded.predictions_path is None
    with pytest.raises(SignalValidationError, match="TOP_K_ONLY"):
        load_prediction_record(legacy.directory, require_completion=False)
    # Old schema-2 files without the explicit scope remain strict read-only inputs.
    _write_manifest(legacy, lambda item: item.pop("prediction_scope"))
    assert load_signal_record(legacy.directory, require_completion=False).prediction_scope == TOP_K_ONLY
    with pytest.raises(SignalValidationError, match="TOP_K_ONLY"):
        load_prediction_record(legacy.directory, require_completion=False)
    (legacy.directory / "predictions.csv").write_bytes(_predictions().to_csv(index=False).encode("utf-8"))
    with pytest.raises(SignalValidationError, match="schema-declared files"):
        load_signal_record(legacy.directory, require_completion=False)


def test_full_prediction_versions_are_append_only_and_legacy_cannot_be_overwritten(tmp_path):
    recorder = _recorder(tmp_path)
    first = recorder.record(_signals(), **_kwargs())
    before = {path: path.read_bytes() for path in first.directory.iterdir()}
    with pytest.raises(SignalVersionConflictError, match="already exists"):
        recorder.record(_signals(), predictions=_predictions(), cross_section_size=6,
                        version=first.version, **_kwargs())
    second = recorder.record(
        _signals(), predictions=_predictions(), cross_section_size=6,
        **_kwargs(run_id="synthetic-late-run", generated_at="2026-09-25T09:00:00+08:00",
                  timeliness="LATE_REISSUE", reissue_reason="synthetic correction test"),
    )
    assert second.version == "v000002"
    assert {path: path.read_bytes() for path in first.directory.iterdir()} == before
    assert second.prediction_row_count == 6


@pytest.mark.parametrize("failure_name", ["predictions.csv", "manifest.json", "manifest.sha256"])
def test_failed_full_prediction_staging_never_leaves_published_or_orphaned_staging(tmp_path, monkeypatch, failure_name):
    original = module._write_exclusive

    def fail(path, content):
        if path.name == failure_name:
            raise OSError("synthetic staging failure")
        return original(path, content)

    monkeypatch.setattr(module, "_write_exclusive", fail)
    recorder = _recorder(tmp_path)
    with pytest.raises(OSError, match="staging failure"):
        recorder.record(_signals(), predictions=_predictions(), cross_section_size=6, **_kwargs())
    assert list((recorder.root / DAY.isoformat()).iterdir()) == []
    assert not recorder.lock_path(DAY).exists()


def test_concurrent_full_prediction_writers_publish_distinct_complete_versions(tmp_path):
    recorder = _recorder(tmp_path)

    def write(index):
        return recorder.record(_signals(), predictions=_predictions(), cross_section_size=6,
                               **_kwargs(run_id="synthetic-concurrent-%d" % index))

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(write, range(4)))
    assert sorted(record.version for record in records) == ["v000001", "v000002", "v000003", "v000004"]
    for record in records:
        loaded = load_signal_record(record.directory, require_completion=False)
        assert loaded.prediction_row_count == 6
        assert loaded.predictions_sha256 == record.predictions_sha256
    assert not recorder.lock_path(DAY).exists()
