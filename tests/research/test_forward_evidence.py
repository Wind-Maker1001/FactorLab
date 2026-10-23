"""Deterministic evidence selection from synthetic completed local records."""

from dataclasses import FrozenInstanceError
from datetime import date, datetime
import inspect
import json
from pathlib import Path

import pandas as pd
import pytest

from factorlab.forward import evidence as module
from factorlab.forward.evidence import (
    ForwardEvidenceError, ForwardPredictionEvidence, SELECTION_COLUMNS,
    select_forward_prediction_records,
)
from factorlab.forward.signal_recorder import SignalRecorder, finalize_signal_record


DAY = date(2026, 9, 24)
IDENTITY = {
    "activation_id": "2026-09-24-eeeeeeeeeeeeeeee",
    "activation_manifest_sha256": "f" * 64,
    "research_contract_sha256": "e" * 64, "source_sha256": "1" * 64,
    "model_name": "ridge", "model_version": "synthetic-test-only",
    "model_bundle_sha256": "a" * 64,
}


def _publish(tmp_path, *, day=DAY, generated_at=None, version=None,
             full=True, late=False, completed=True, scores=None, **identity):
    values = dict(IDENTITY, **identity)
    predictions = pd.DataFrame({"symbol": ["000001.SZ", "000002.SZ", "600000.SH"],
                                "score": scores or [0.9, 0.8, -0.2], "rank": [1, 2, 3]})
    signals = predictions.head(2).copy()
    signals["target_weight"] = 0.5
    kwargs = dict(values, signal_date=day, data_asof=day,
                  generated_at=generated_at or day.isoformat() + "T16:00:00+08:00",
                  dataset_sha256="b" * 64, git_commit="c" * 40,
                  run_id="synthetic-" + (version or "auto"), universe_version="d" * 64,
                  timeliness="LATE_REISSUE" if late else "ON_TIME",
                  reissue_reason="synthetic late update" if late else None,
                  version=version)
    if full:
        kwargs.update(predictions=predictions, cross_section_size=len(predictions))
    record = SignalRecorder(tmp_path / "signals", activation_date=DAY).record(signals, **kwargs)
    if not completed:
        return record
    manifest = json.loads(record.manifest_path.read_text(encoding="utf-8"))
    producer = {
        "run_id": manifest["run_id"], "command": "forward-signal", "status": "success",
        "completed_at": day.isoformat() + "T20:20:00+00:00",
        "signal_sha256": record.sha256, "signal_manifest_sha256": record.manifest_sha256,
        "signal_rows": record.row_count,
        "outputs": {"forward_signals": str(record.signals_path), "forward_signal_manifest": str(record.manifest_path)},
    }
    if full:
        producer.update(prediction_sha256=record.predictions_sha256, prediction_rows=record.prediction_row_count)
        producer["outputs"]["forward_predictions"] = str(record.predictions_path)
    path = tmp_path / (day.isoformat() + "-" + record.version + "-producer.json")
    path.write_text(json.dumps(producer), encoding="utf-8")
    return finalize_signal_record(record.directory, path)


def _select(tmp_path, **updates):
    args = dict(IDENTITY, start=DAY, end=DAY)
    args.update(updates)
    return select_forward_prediction_records(tmp_path / "signals", **args)


def _bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_selects_first_on_time_not_highest_score_or_late_and_preserves_originals(tmp_path):
    first = _publish(tmp_path, generated_at="2026-09-24T15:31:00+08:00")
    _publish(tmp_path, generated_at="2026-09-24T18:00:00+08:00", scores=[90.0, 80.0, 70.0])
    _publish(tmp_path, generated_at="2026-09-25T18:00:00+08:00", late=True, scores=[900.0, 800.0, 700.0])
    before = _bytes(tmp_path)
    result = _select(tmp_path)
    assert tuple(result.selected.columns) == SELECTION_COLUMNS
    assert result.selected["version"].tolist() == [first.version]
    assert result.selected["prediction_sha256"].tolist() == [first.predictions_sha256]
    assert result.predictions["score"].tolist() == [0.9, 0.8, -0.2]
    assert result.predictions["signal_version"].tolist() == [first.version] * 3
    assert result.predictions["signal_manifest_sha256"].tolist() == [first.manifest_sha256] * 3
    assert set(result.excluded["reason"]) == {"NOT_FIRST_ON_TIME", "LATE_REISSUE"}
    assert _bytes(tmp_path) == before


def test_numeric_sequential_tie_rule_and_custom_non_tied_timestamp(tmp_path):
    _publish(tmp_path, version="v000010")
    smallest = _publish(tmp_path, version="v000002")
    assert _select(tmp_path).selected["version"].tolist() == [smallest.version]
    other = tmp_path / "custom"
    _publish(other, version="aaa", generated_at="2026-09-24T17:00:00+08:00")
    earliest = _publish(other, version="zzz", generated_at="2026-09-24T16:00:00+08:00")
    assert _select(other).selected["version"].tolist() == [earliest.version]


@pytest.mark.parametrize("versions", [("custom-a", "custom-b"), ("v000001", "custom-b"), ("v000001", "v0000001"), ("v000000", "v000001")])
def test_same_time_ambiguous_nonsequential_or_noncanonical_versions_fail(tmp_path, versions):
    for version in versions:
        _publish(tmp_path, version=version)
    with pytest.raises(ForwardEvidenceError, match="Same-time ON_TIME"):
        _select(tmp_path)


def test_legacy_topk_is_visible_exclusion_and_never_rescored(tmp_path):
    legacy = _publish(tmp_path, full=False, generated_at="2026-09-24T15:31:00+08:00")
    actual = _publish(tmp_path, generated_at="2026-09-24T16:00:00+08:00")
    before = _bytes(tmp_path)
    evidence = _select(tmp_path)
    assert evidence.selected["version"].tolist() == [actual.version]
    assert evidence.excluded["version"].tolist() == [legacy.version]
    assert evidence.excluded["reason"].tolist() == ["TOP_K_ONLY"]
    assert evidence.excluded["prediction_rows"].tolist() == [0]
    assert not (legacy.directory / "predictions.csv").exists()
    assert _bytes(tmp_path) == before


def test_changed_legacy_manifest_after_verification_is_not_silently_excluded(tmp_path, monkeypatch):
    legacy = _publish(tmp_path, full=False)
    _publish(tmp_path, generated_at="2026-09-24T18:00:00+08:00")
    genuine_load = module.load_signal_record
    def changed_after_verification(directory):
        record = genuine_load(directory)
        if record.prediction_scope == "TOP_K_ONLY":
            legacy.manifest_path.write_bytes(b"{}")
        return record
    monkeypatch.setattr(module, "load_signal_record", changed_after_verification)
    with pytest.raises(ForwardEvidenceError, match="changed after verification"):
        _select(tmp_path)


def test_identity_mismatch_in_legacy_or_late_is_not_ignored(tmp_path):
    _publish(tmp_path)
    _publish(tmp_path, full=False, source_sha256="2" * 64)
    with pytest.raises(ForwardEvidenceError, match="Mixed forward experiment"):
        _select(tmp_path)


@pytest.mark.parametrize("name", ["2026-9-24", "2026-02-30", "unknown"])
def test_noncanonical_date_directory_is_not_hidden(tmp_path, name):
    _publish(tmp_path)
    (tmp_path / "signals" / name).mkdir()
    with pytest.raises(ForwardEvidenceError):
        _select(tmp_path)


def test_different_registered_activation_is_a_strict_experiment_mismatch(tmp_path):
    _publish(tmp_path)
    with pytest.raises(ForwardEvidenceError, match="activation_id"):
        _select(tmp_path, activation_id="2026-09-23-eeeeeeeeeeeeeeee")


@pytest.mark.parametrize("mode", ["legacy", "late", "missing", "empty"])
def test_no_eligible_evidence_never_returns_fake_empty_ic(tmp_path, mode):
    if mode == "legacy":
        _publish(tmp_path, full=False)
    elif mode == "late":
        _publish(tmp_path, late=True, generated_at="2026-09-25T16:00:00+08:00")
    elif mode == "empty":
        (tmp_path / "signals").mkdir()
    with pytest.raises(ForwardEvidenceError):
        _select(tmp_path)


@pytest.mark.parametrize("kind", ["prediction_hash", "completion_missing", "extra_file", "orphan_staging", "date_file", "root_extra", "signal_lock", "empty_date"])
def test_any_in_range_damaged_loser_or_orphan_fails_closed(tmp_path, kind):
    _publish(tmp_path, generated_at="2026-09-24T15:31:00+08:00")
    loser = _publish(tmp_path, generated_at="2026-09-24T17:00:00+08:00", completed=kind != "completion_missing")
    if kind == "prediction_hash":
        loser.predictions_path.write_bytes(loser.predictions_path.read_bytes() + b"\n")
    elif kind == "extra_file":
        (loser.directory / "extra.txt").write_text("unknown", encoding="utf-8")
    elif kind == "orphan_staging":
        (loser.directory.parent / ".v000003.tmp-orphan").mkdir()
    elif kind == "date_file":
        (loser.directory.parent / "not-a-version.csv").write_text("unknown", encoding="utf-8")
    elif kind == "root_extra":
        (tmp_path / "signals" / "ignore-me.txt").write_text("unknown", encoding="utf-8")
    elif kind == "signal_lock":
        (tmp_path / "signals" / ".2026-09-24.signal.lock").write_text("active-or-abandoned", encoding="utf-8")
    elif kind == "empty_date":
        (tmp_path / "signals" / "2026-09-25").mkdir()
    with pytest.raises(ForwardEvidenceError):
        _select(tmp_path, end=date(2026, 9, 25))


@pytest.mark.parametrize("field,value", [
    ("activation_manifest_sha256", "2" * 64), ("source_sha256", "2" * 64),
    ("model_name", "factor_score"), ("model_version", "other-version"),
    ("model_bundle_sha256", "2" * 64),
    ("research_contract_sha256", "2" * 64),
])
def test_identity_mismatch_is_not_silently_removed_from_experiment(tmp_path, field, value):
    _publish(tmp_path)
    kwargs = {field: value}
    if field == "research_contract_sha256":
        kwargs["activation_id"] = "2026-09-24-" + value[:16]
    _publish(tmp_path, generated_at="2026-09-24T18:00:00+08:00", **kwargs)
    with pytest.raises(ForwardEvidenceError, match="Mixed forward experiment"):
        _select(tmp_path)


@pytest.mark.parametrize("generated", ["2026-09-24T14:59:59+08:00", "2026-09-24T21:00:00+03:00"])
def test_storage_local_on_time_must_also_be_after_close_same_shanghai_date(tmp_path, generated):
    _publish(tmp_path, generated_at=generated)
    with pytest.raises(ForwardEvidenceError, match="Shanghai market close"):
        _select(tmp_path)


def test_different_offset_same_utc_time_has_same_selection_order(tmp_path):
    _publish(tmp_path, generated_at="2026-09-24T12:00:00+03:00", version="v000010")
    first = _publish(tmp_path, generated_at="2026-09-24T17:00:00+08:00", version="v000001")
    assert _select(tmp_path).selected["version"].tolist() == [first.version]


def test_gaps_do_not_invent_predictions_and_sorted_dates_remain_in_range(tmp_path):
    _publish(tmp_path, day=date(2026, 9, 28))
    _publish(tmp_path, day=DAY)
    _publish(tmp_path, day=date(2026, 9, 29), completed=False)
    evidence = _select(tmp_path, end=date(2026, 9, 28))
    assert evidence.selected["signal_date"].tolist() == ["2026-09-24", "2026-09-28"]
    assert len(evidence.predictions) == 6


def test_exact_close_is_valid_and_custom_close_is_frozen_input(tmp_path):
    _publish(tmp_path, generated_at="2026-09-24T15:30:00+08:00")
    assert len(_select(tmp_path).selected) == 1
    with pytest.raises(ForwardEvidenceError, match="Shanghai market close"):
        _select(tmp_path, market_close_hour=15, market_close_minute=31)


def test_later_same_time_custom_versions_cannot_change_unambiguous_first_choice(tmp_path):
    first = _publish(tmp_path, version="first-custom", generated_at="2026-09-24T15:30:00+08:00")
    _publish(tmp_path, version="later-a", generated_at="2026-09-24T17:00:00+08:00")
    _publish(tmp_path, version="later-b", generated_at="2026-09-24T17:00:00+08:00")
    result = _select(tmp_path)
    assert result.selected["version"].tolist() == [first.version]
    assert result.excluded["reason"].tolist() == ["NOT_FIRST_ON_TIME"] * 2


def test_frozen_container_and_same_byte_dataframes_are_defensive_copies(tmp_path):
    _publish(tmp_path)
    result = _select(tmp_path)
    changed = result.predictions
    changed.loc[0, "score"] = 999.0
    plan = result.selected
    plan.loc[0, "version"] = "forged"
    assert result.predictions["score"].iloc[0] == 0.9
    assert result.selected["version"].iloc[0] == "v000001"
    with pytest.raises(FrozenInstanceError):
        result._selected = plan
    source = pd.DataFrame({"symbol": ["000001.SZ"]})
    container = ForwardPredictionEvidence(source, source, source)
    source.loc[0, "symbol"] = "forged"
    assert container.predictions["symbol"].iloc[0] == "000001.SZ"
    assert container is not result and container != result


def test_full_frame_comes_from_same_verified_record_not_a_second_csv_read(tmp_path, monkeypatch):
    record = _publish(tmp_path)
    genuine_load = module.load_signal_record
    def after_verified(directory):
        verified = genuine_load(directory)
        # Simulate a non-cooperating edit after the loader has pinned bytes.
        record.predictions_path.write_bytes(b"do not reopen these later bytes")
        return verified
    monkeypatch.setattr(module, "load_signal_record", after_verified)
    result = _select(tmp_path)
    assert result.predictions["score"].tolist() == [0.9, 0.8, -0.2]
    assert result.selected["prediction_sha256"].iloc[0] == record.predictions_sha256


def test_selector_interface_has_no_outcomes_or_retrospective_rescoring_input():
    parameters = inspect.signature(select_forward_prediction_records).parameters
    assert not {"labels", "returns", "model", "provider", "test_performance"}.intersection(parameters)


@pytest.mark.parametrize("updates", [
    {"start": "2026-9-24"}, {"start": datetime(2026, 9, 24, 10)},
    {"end": "2026-09-23"}, {"source_sha256": "A" * 64},
    {"source_sha256": True}, {"market_close_hour": True}, {"market_close_minute": 60},
    {"version_policy": "BEST_RETURN"}, {"model_name": " ridge"},
])
def test_invalid_request_contract_is_rejected(tmp_path, updates):
    with pytest.raises(ForwardEvidenceError):
        _select(tmp_path, **updates)


def test_linked_root_and_version_rejected(tmp_path):
    record = _publish(tmp_path)
    link = tmp_path / "linked-signals"
    try:
        link.symlink_to(tmp_path / "signals", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Host cannot create symlinks")
    with pytest.raises(ForwardEvidenceError, match="symlink or reparse"):
        select_forward_prediction_records(link, start=DAY, end=DAY, **IDENTITY)
    (record.directory.parent / "another-version").symlink_to(record.directory, target_is_directory=True)
    with pytest.raises(ForwardEvidenceError, match="symlink or reparse"):
        _select(tmp_path)
