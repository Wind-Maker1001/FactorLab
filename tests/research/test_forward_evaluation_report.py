"""Artificial forward report contracts; no real signals, market or research claims."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from factorlab.reports import forward as report


def test_forward_date_plots_use_readable_rotated_ticks(monkeypatch):
    captured = []
    original = report.plt.subplots

    def capture_subplots(*args, **kwargs):
        figure, axis = original(*args, **kwargs)
        captured.append((figure, axis))
        return figure, axis

    monkeypatch.setattr(report.plt, "subplots", capture_subplots)
    daily = pd.DataFrame({
        "date": pd.to_datetime(["2099-01-13", "2099-01-15", "2099-01-21"]),
        "coverage": [5.0 / 6.0, 0.0, 0.0],
    })
    content = report._figure_bytes(daily, pd.DataFrame(), "coverage.png")
    assert content.startswith(b"\x89PNG\r\n\x1a\n")
    figure, axis = captured[0]
    assert all(tick.get_rotation() == 30.0 for tick in axis.get_xticklabels())
    assert all(tick.get_ha() == "right" for tick in axis.get_xticklabels())
    assert not report.plt.fignum_exists(figure.number)


def _inputs(immature=False):
    original = pd.DataFrame({
        "signal_date": ["2026-09-14"] * 4 + ["2026-09-15"] * 4,
        "symbol": ["000001.SZ", "000002.SZ", "600000.SH", "600001.SH"] * 2,
        "score": [0.10000000000000002, 0.03, -0.1, -0.2] * 2,
        "rank": [1, 2, 3, 4] * 2,
        "signal_version": ["v000001"] * 8,
        "signal_manifest_sha256": ["a" * 64] * 8,
        "timeliness": ["ON_TIME"] * 8,
    })
    predictions = original.copy(deep=True)
    predictions["date"] = pd.to_datetime(predictions["signal_date"])
    predictions["EXCESS_RET5"] = [np.nan] * 8 if immature else [0.02, 0.01, -0.01, -0.02] + [np.nan] * 4
    predictions["label_status"] = ["IMMATURE"] * 8 if immature else ["MATURE"] * 4 + ["IMMATURE"] * 4
    daily = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-14", "2026-09-15"]),
        "prediction_ic": [np.nan, np.nan] if immature else [0.9, np.nan],
        "prediction_rank_ic": [np.nan, np.nan] if immature else [1.0, np.nan],
        "coverage": [np.nan, np.nan] if immature else [1.0, 0.0],
        "top_label_coverage": [np.nan, np.nan] if immature else [1.0, 0.0],
        "bottom_label_coverage": [np.nan, np.nan] if immature else [1.0, 0.0],
        "top_k_future_return": [np.nan, np.nan] if immature else [0.015, np.nan],
        "bottom_k_future_return": [np.nan, np.nan] if immature else [-0.015, np.nan],
        "top_bottom_spread": [np.nan, np.nan] if immature else [0.03, np.nan],
    })
    summary = pd.DataFrame({
        "scope": ["OVERALL", "REGIME"], "period": ["ALL", "BULL"],
        "prediction_ic_mean": [np.nan, np.nan] if immature else [0.9, 0.9],
        "prediction_rank_ic_mean": [np.nan, np.nan] if immature else [1.0, 1.0],
        "top_bottom_spread": [np.nan, np.nan] if immature else [0.03, 0.03],
    })
    selected = pd.DataFrame({"signal_date": ["2026-09-14", "2026-09-15"], "version": ["v000001", "v000001"], "version_policy": ["FIRST_ON_TIME"] * 2})
    excluded = pd.DataFrame({"signal_date": ["2026-09-14"], "version": ["v000002"], "reason": ["LATE_REISSUE"], "version_policy": ["FIRST_ON_TIME"]})
    evaluation = SimpleNamespace(predictions=predictions, daily=daily, summary=summary, as_of="2026-09-22")
    evidence = SimpleNamespace(predictions=original, selected=selected, excluded=excluded)
    provenance = {"source_sha256": "b" * 64, "config": {"seed": 42, "live_trading": False}, "version_policy": "FIRST_ON_TIME", "as_of": "2026-09-22", "expected_signal_dates": ["2026-09-14", "2026-09-15", "2026-09-16"], "missing_signal_dates": ["2026-09-16"], "synthetic_for_testing_only": True}
    return evaluation, evidence, provenance


@pytest.fixture
def inputs():
    return _inputs()


def _write(tmp_path, inputs, run_id="synthetic-forward-eval"):
    evaluation, evidence, provenance = inputs
    return report.write_forward_evaluation_report(evaluation, evidence, tmp_path / run_id, run_id=run_id, provenance=provenance)


@pytest.fixture
def published(tmp_path, inputs):
    return _write(tmp_path, inputs)


def _producer(record):
    return {
        "run_id": record.manifest["run_id"], "command": "evaluate-forward", "status": "success",
        "completed_at": "2026-09-22T09:00:00+00:00",
        "forward_evaluation_manifest_sha256": record.manifest_sha256,
        "forward_evaluation_artifact_sha256": record.artifact_sha256,
        "outputs": {"forward_evaluation_report": str(record.directory)},
    }


def _save_producer(tmp_path, record, payload=None):
    path = tmp_path / "producer.json"
    path.write_text(json.dumps(_producer(record) if payload is None else payload), encoding="utf-8")
    return path


def test_report_keeps_full_cross_section_exact_floats_and_missing_labels(published, inputs):
    evaluation, evidence, provenance = inputs
    assert len(published.artifact_sha256) == 13
    assert published.manifest["version_policy"] == "FIRST_ON_TIME"
    assert published.manifest["provenance"] == provenance
    assert published.manifest["as_of"] == evaluation.as_of
    stored = pd.read_parquet(published.directory / "forward_predictions.parquet")
    pd.testing.assert_frame_equal(stored, evaluation.predictions)
    assert len(stored) == len(evidence.predictions) == 8
    csv = pd.read_csv(published.directory / "forward_predictions.csv", float_precision="round_trip")
    np.testing.assert_array_equal(csv["score"], evaluation.predictions["score"])
    assert csv["EXCESS_RET5"].isna().sum() == 4
    for name, digest in published.artifact_sha256.items():
        assert hashlib.sha256((published.directory / name).read_bytes()).hexdigest() == digest
    assert hashlib.sha256(published.manifest_path.read_bytes()).hexdigest() == published.manifest_sha256
    text = (published.directory / "summary.md").read_text(encoding="utf-8")
    assert "FIRST_ON_TIME" in text and "ON_TIME" in text
    assert "not realised portfolio returns" in text and "before transaction costs" in text
    assert "2026-09-16" in text and "IMMATURE: 4" in text and "MATURE: 4" in text
    assert "NaN" in text and "No orders" in text
    assert "SYNTHETIC SOFTWARE DEMO ONLY - NOT A RESEARCH RESULT" in text


@pytest.mark.parametrize("name,value", [
    ("synthetic", True), ("software_demo_only", True),
    ("synthetic_for_testing_only", True), ("research_result", False),
])
def test_each_demo_provenance_flag_is_visible_in_standalone_summary(tmp_path, inputs, name, value):
    inputs[2].pop("synthetic_for_testing_only")
    inputs[2][name] = value
    record = _write(tmp_path, inputs)
    text = (record.directory / "summary.md").read_text(encoding="utf-8")
    assert "SYNTHETIC SOFTWARE DEMO ONLY - NOT A RESEARCH RESULT" in text
    assert "Artificial inputs do not demonstrate real forward prediction performance" in text


def test_report_without_demo_flags_does_not_invent_synthetic_status(tmp_path, inputs):
    inputs[2].pop("synthetic_for_testing_only")
    record = _write(tmp_path, inputs)
    assert "SYNTHETIC SOFTWARE DEMO ONLY" not in (record.directory / "summary.md").read_text(encoding="utf-8")


def test_default_reader_rejects_orphan_even_if_mtime_looks_recent(published):
    published.manifest_path.touch()
    with pytest.raises(report.ForwardEvaluationReportError, match="orphan"):
        report.load_forward_evaluation_report(published.directory)
    assert report.load_forward_evaluation_report(published.directory, require_completion=False).manifest_sha256 == published.manifest_sha256


def test_existing_run_cannot_be_rewritten_and_original_bytes_remain(published, inputs):
    before = {path.name: path.read_bytes() for path in published.directory.iterdir()}
    with pytest.raises(report.ForwardEvaluationReportError, match="already exists"):
        _write(published.directory.parent, inputs)
    assert {path.name: path.read_bytes() for path in published.directory.iterdir()} == before


def test_distinct_run_appends_and_does_not_change_first_report(published, inputs):
    second = _write(published.directory.parent, inputs, "synthetic-second-run")
    assert second.directory != published.directory
    assert report.load_forward_evaluation_report(published.directory, require_completion=False).manifest_sha256 == published.manifest_sha256


def test_completion_binds_success_snapshot_and_never_rereads_mutable_producer(published, tmp_path):
    producer_path = _save_producer(tmp_path, published)
    producer_bytes = producer_path.read_bytes()
    completed = report.finalize_forward_evaluation_report(published.directory, producer_path)
    assert completed.manifest_sha256 == published.manifest_sha256
    assert (completed.completion_path / "run_manifest.json").read_bytes() == producer_bytes
    receipt = json.loads((completed.completion_path / "receipt.json").read_bytes())
    assert receipt["run_manifest_sha256"] == hashlib.sha256(producer_bytes).hexdigest()
    producer_path.write_bytes(b"mutable producer subsequently changed")
    assert report.load_forward_evaluation_report(completed.directory).manifest_sha256 == completed.manifest_sha256
    with pytest.raises(report.ForwardEvaluationReportError, match="already exists"):
        report.finalize_forward_evaluation_report(completed.directory, producer_path)


def test_finalize_reads_producer_once_and_pins_that_exact_snapshot(published, tmp_path, monkeypatch):
    path = _save_producer(tmp_path, published)
    original = path.read_bytes()
    real_read = report._read
    calls = []
    def change_after_read(candidate):
        content = real_read(candidate)
        if Path(candidate) == path:
            calls.append(candidate)
            path.write_bytes(b"changed after exact read")
        return content
    monkeypatch.setattr(report, "_read", change_after_read)
    completed = report.finalize_forward_evaluation_report(published.directory, path)
    assert len(calls) == 1
    assert (completed.completion_path / "run_manifest.json").read_bytes() == original


@pytest.mark.parametrize("field,bad", [
    ("command", "forward-signal"), ("status", "running"), ("run_id", "other-run"),
    ("forward_evaluation_manifest_sha256", "f" * 64),
    ("forward_evaluation_artifact_sha256", {}), ("completed_at", "2026-09-22T09:00:00"),
    ("outputs", {"forward_evaluation_report": "other-directory"}),
])
def test_invalid_producer_cannot_certify_report(published, tmp_path, field, bad):
    payload = _producer(published)
    payload[field] = bad
    path = _save_producer(tmp_path, published, payload)
    with pytest.raises(report.ForwardEvaluationReportError):
        report.finalize_forward_evaluation_report(published.directory, path)
    assert not published.completion_path.exists()


def test_all_artifact_byte_tampering_is_rejected(published):
    for name in published.artifact_sha256:
        path = published.directory / name
        original = path.read_bytes()
        path.write_bytes(original + b"tamper")
        with pytest.raises(report.ForwardEvaluationReportError, match="artifact SHA-256 mismatch"):
            report.load_forward_evaluation_report(published.directory, require_completion=False)
        path.write_bytes(original)


def test_extra_inventory_or_nested_artifact_cannot_be_ignored(published):
    unknown = published.directory / "unexpected.csv"
    unknown.write_text("hidden", encoding="utf-8")
    with pytest.raises(report.ForwardEvaluationReportError, match="inventory"):
        report.load_forward_evaluation_report(published.directory, require_completion=False)
    unknown.unlink()
    artifact = published.directory / "coverage.png"
    artifact.unlink()
    artifact.mkdir()
    with pytest.raises(report.ForwardEvaluationReportError, match="regular file"):
        report.load_forward_evaluation_report(published.directory, require_completion=False)


def _rewrite_manifest(record, mutate):
    payload = record.manifest
    mutate(payload)
    content = (json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    record.manifest_path.write_bytes(content)
    (record.directory / "manifest.sha256").write_bytes((hashlib.sha256(content).hexdigest() + "\n").encode("ascii"))


@pytest.mark.parametrize("mutate", [
    lambda payload: payload.update(extra_field=True),
    lambda payload: payload.update(version_policy="LATEST_REISSUE"),
    lambda payload: payload.update(schema_version=True),
    lambda payload: payload["tables"]["forward_predictions"].update(rows=1),
    lambda payload: payload["tables"]["forward_predictions"].update(dtypes=["object"] * 10),
    lambda payload: payload["artifact_sha256"].update({"../outside.csv": "a" * 64}),
])
def test_rehashed_but_invalid_manifest_schema_is_not_accepted(published, mutate):
    _rewrite_manifest(published, mutate)
    with pytest.raises(report.ForwardEvaluationReportError):
        report.load_forward_evaluation_report(published.directory, require_completion=False)


def test_rehashed_csv_cannot_disagree_with_parquet(published):
    path = published.directory / "forward_predictions.csv"
    original = path.read_text(encoding="utf-8")
    path.write_bytes(original.replace("0.10000000000000002", "0.8").encode("utf-8"))
    _rewrite_manifest(published, lambda payload: payload["artifact_sha256"].update({path.name: hashlib.sha256(path.read_bytes()).hexdigest()}))
    with pytest.raises(report.ForwardEvaluationReportError, match="table bytes differ"):
        report.load_forward_evaluation_report(published.directory, require_completion=False)


def test_completion_snapshot_tamper_is_rejected(published, tmp_path):
    completed = report.finalize_forward_evaluation_report(published.directory, _save_producer(tmp_path, published))
    path = completed.completion_path / "run_manifest.json"
    path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(report.ForwardEvaluationReportError, match="snapshot SHA-256 mismatch"):
        report.load_forward_evaluation_report(completed.directory)


def test_partial_completion_inventory_is_not_success(published):
    published.completion_path.mkdir()
    (published.completion_path / "receipt.json").write_text("{}", encoding="utf-8")
    with pytest.raises(report.ForwardEvaluationReportError, match="Completion receipt inventory"):
        report.load_forward_evaluation_report(published.directory)


def test_all_immature_labels_stay_nan_and_plots_use_insufficient_data(tmp_path, monkeypatch):
    placeholders = []
    real_placeholder = report._placeholder
    def remember(axis, title):
        placeholders.append(title)
        return real_placeholder(axis, title)
    monkeypatch.setattr(report, "_placeholder", remember)
    record = _write(tmp_path, _inputs(immature=True))
    assert len(placeholders) == 4
    assert pd.read_parquet(record.directory / "forward_predictions.parquet")["EXCESS_RET5"].isna().all()
    assert pd.read_parquet(record.directory / "prediction_daily.parquet")["top_k_future_return"].isna().all()
    assert "IMMATURE: 8" in (record.directory / "summary.md").read_text(encoding="utf-8")
    for name in report._PLOTS:
        assert (record.directory / name).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.parametrize("change", ["top-k-only", "score", "rank", "duplicate"])
def test_evaluation_cannot_drop_or_change_recorded_predictions(tmp_path, inputs, change):
    evaluation, evidence, _ = inputs
    if change == "top-k-only":
        evaluation.predictions = evaluation.predictions.iloc[:2].copy()
    elif change == "duplicate":
        evidence.predictions.loc[1, "symbol"] = evidence.predictions.loc[0, "symbol"]
    else:
        evaluation.predictions.loc[0, change] = 123
    with pytest.raises(report.ForwardEvaluationReportError):
        _write(tmp_path, inputs)
    assert not (tmp_path / "synthetic-forward-eval").exists()


@pytest.mark.parametrize("value", [np.nan, np.inf, {"unsupported"}])
def test_nonfinite_or_nonjson_provenance_fails_before_publication(tmp_path, inputs, value):
    inputs[2]["bad_value"] = value
    with pytest.raises(report.ForwardEvaluationReportError, match="finite JSON"):
        _write(tmp_path, inputs)
    assert not (tmp_path / "synthetic-forward-eval").exists()


def test_staging_write_failure_removes_only_owned_files(tmp_path, inputs, monkeypatch):
    original = report._write_owned
    count = 0
    def fail(path, content, owned):
        nonlocal count
        count += 1
        if count == 3:
            raise OSError("artificial staging failure")
        return original(path, content, owned)
    monkeypatch.setattr(report, "_write_owned", fail)
    with pytest.raises(report.ForwardEvaluationReportError, match="Cannot publish"):
        _write(tmp_path, inputs)
    assert not (tmp_path / "synthetic-forward-eval").exists()
    assert not list(tmp_path.glob(".*.report.tmp-*"))
    assert not list(tmp_path.glob("*.lock"))


def test_unknown_file_in_owned_stage_is_not_deleted_recursively(tmp_path, inputs, monkeypatch):
    original = report._write_owned
    count = 0
    def inject_unknown(path, content, owned):
        nonlocal count
        count += 1
        if count == 2:
            (path.parent / "unknown-user-file.txt").write_text("retain me", encoding="utf-8")
            raise OSError("artificial interruption with unknown file")
        return original(path, content, owned)
    monkeypatch.setattr(report, "_write_owned", inject_unknown)
    with pytest.raises(report.ForwardEvaluationReportError):
        _write(tmp_path, inputs)
    stages = list(tmp_path.glob(".*.report.tmp-*"))
    assert len(stages) == 1
    assert {path.name for path in stages[0].iterdir()} == {"unknown-user-file.txt"}
    assert (stages[0] / "unknown-user-file.txt").read_text(encoding="utf-8") == "retain me"


def test_stage_collision_does_not_remove_someone_elses_stage(tmp_path, inputs, monkeypatch):
    monkeypatch.setattr(report.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    stage = tmp_path / (".synthetic-forward-eval.report.tmp-" + "a" * 32)
    stage.mkdir()
    marker = stage / "unrelated.txt"
    marker.write_text("belongs to another writer", encoding="utf-8")
    with pytest.raises(report.ForwardEvaluationReportError):
        _write(tmp_path, inputs)
    assert marker.read_text(encoding="utf-8") == "belongs to another writer"


def test_linked_output_parent_is_rejected_before_artifacts(tmp_path, inputs):
    external = tmp_path / "external"
    external.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("This host cannot create symlinks")
    with pytest.raises(report.ForwardEvaluationReportError, match="symlink or reparse"):
        _write(link, inputs)
    assert not list(external.iterdir())


def test_returned_manifest_and_hash_maps_are_defensive_copies(published):
    copy = published.manifest
    copy["provenance"]["config"]["seed"] = -1
    hashes = published.artifact_sha256
    hashes.clear()
    assert published.manifest["provenance"]["config"]["seed"] == 42
    assert len(published.artifact_sha256) == 13


def test_reader_rejects_rehashed_nonfinite_metadata(published):
    payload = published.manifest
    payload["provenance"]["bad"] = float("nan")
    content = json.dumps(payload, allow_nan=True).encode("utf-8")
    published.manifest_path.write_bytes(content)
    (published.directory / "manifest.sha256").write_bytes((hashlib.sha256(content).hexdigest() + "\n").encode("ascii"))
    with pytest.raises(report.ForwardEvaluationReportError, match="strict JSON"):
        report.load_forward_evaluation_report(published.directory, require_completion=False)
