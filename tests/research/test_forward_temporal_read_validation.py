"""Read-side semantic validation is not an external timestamp guarantee.

These synthetic local records deliberately rehash an *unfinalized* bundle.
The test isolates date/timeliness invariants from ordinary digest mismatch.
Completed records retain their separate producer-completion hash boundary;
no historical real prediction is generated or modified here.
"""

from datetime import date
import hashlib
import io
import json

import pandas as pd
import pytest

from factorlab.forward.signal_recorder import (
    SignalRecorder, SignalValidationError, finalize_signal_record,
    load_prediction_record, load_signal_record,
)


DAY = date(2026, 9, 24)


def _kwargs(**updates):
    values = {
        "signal_date": DAY, "data_asof": DAY,
        "generated_at": "2026-09-24T16:10:00+08:00",
        "model_name": "ridge", "model_version": "synthetic-test-only",
        "model_bundle_sha256": "a" * 64, "dataset_sha256": "b" * 64,
        "git_commit": "c" * 40, "run_id": "synthetic-temporal-run",
        "universe_version": "d" * 64,
        "activation_id": "2026-09-24-eeeeeeeeeeeeeeee",
        "activation_manifest_sha256": "f" * 64,
        "research_contract_sha256": "e" * 64, "source_sha256": "1" * 64,
        "timeliness": "ON_TIME",
    }
    values.update(updates)
    return values


def _record(tmp_path, full=True, **updates):
    predictions = pd.DataFrame({"symbol": ["000001.SZ", "000002.SZ", "600000.SH"],
                                "score": [0.9, 0.8, -0.2], "rank": [1, 2, 3]})
    signals = predictions.head(2).copy()
    signals["target_weight"] = 0.5
    extra = {"predictions": predictions, "cross_section_size": 3} if full else {}
    return SignalRecorder(tmp_path / "synthetic-records", activation_date=DAY).record(
        signals, **extra, **_kwargs(**updates)
    )


def _rehash(record, updates):
    manifest = json.loads(record.manifest_path.read_text(encoding="utf-8"))
    manifest.update(updates)
    for name in manifest["files"]:
        path = record.directory / name
        frame = pd.read_csv(io.BytesIO(path.read_bytes()), keep_default_na=False,
                            float_precision="round_trip", dtype={"symbol": str})
        for key, value in updates.items():
            frame[key] = value
        content = frame.to_csv(index=False, lineterminator="\n", float_format="%.17g").encode("utf-8")
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        manifest["files"][name] = {"bytes": len(content), "sha256": digest}
        manifest["sha256" if name == "signals.csv" else "prediction_sha256"] = digest
    content = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    record.manifest_path.write_bytes(content)
    (record.directory / "manifest.sha256").write_text(hashlib.sha256(content).hexdigest() + "\n", encoding="ascii")


INVALID = [
    ({"data_asof": "2026-09-25"}, "data_asof must not be later"),
    ({"generated_at": "2026-09-23T16:10:00+08:00"}, "local date must not be earlier"),
    ({"generated_at": "2026-09-24T01:00:00+14:00"}, "UTC date must not be earlier"),
    ({"generated_at": "2026-09-25T16:10:00+08:00"}, "ON_TIME requires"),
    ({"timeliness": "UNCLASSIFIED"}, "ON_TIME or LATE_REISSUE"),
    ({"timeliness": "ON_TIME "}, "canonical string"),
    ({"timeliness": True}, "canonical string"),
    ({"timeliness": "LATE_REISSUE", "reissue_reason": ""}, "reissue_reason"),
    ({"timeliness": "LATE_REISSUE", "reissue_reason": None}, "reissue_reason"),
    ({"reissue_reason": "unrecorded retrospective change"}, "must not include"),
    ({"generated_at": "2026-09-24T16:10:00"}, "UTC offset"),
]


@pytest.mark.parametrize("full", [False, True], ids=["legacy-schema2", "full-schema3"])
@pytest.mark.parametrize("updates,match", INVALID)
def test_consistently_rehashed_unfinalized_temporal_violation_is_rejected(tmp_path, full, updates, match):
    record = _record(tmp_path, full=full)
    _rehash(record, updates)
    with pytest.raises(SignalValidationError, match=match):
        load_signal_record(record.directory, require_completion=False)
    if full:
        with pytest.raises(SignalValidationError, match=match):
            load_prediction_record(record.directory, require_completion=False)


@pytest.mark.parametrize("updates,match", INVALID)
def test_writer_and_reader_share_existing_temporal_rules(tmp_path, updates, match):
    with pytest.raises(SignalValidationError, match=match):
        _record(tmp_path, **updates)
    assert not (tmp_path / "synthetic-records").exists()


@pytest.mark.parametrize("full", [False, True])
def test_reading_saved_past_on_time_record_does_not_require_current_clock(tmp_path, full):
    record = _record(tmp_path, full=full)
    loaded = load_signal_record(record.directory, require_completion=False)
    assert loaded.signal_date == DAY
    # Reading doesn't replace a completion receipt with a date-only check.
    with pytest.raises(SignalValidationError, match="completion receipt"):
        load_signal_record(record.directory)


@pytest.mark.parametrize("full", [False, True])
def test_saved_late_reissue_and_existing_explicit_offset_policy_are_retained(tmp_path, full):
    # Preserve the old represented-local-date policy; do not impose Shanghai
    # conversion in storage. CLI policy handles its configured market timezone.
    record = _record(tmp_path, full=full,
                     generated_at="2026-09-25T16:10:00+03:00",
                     timeliness="LATE_REISSUE", reissue_reason="synthetic delayed update")
    assert load_signal_record(record.directory, require_completion=False).signal_date == DAY
    assert json.loads(record.manifest_path.read_text(encoding="utf-8"))["generated_at"].endswith("+03:00")


def test_completed_full_record_still_requires_unchanged_producer_bound_manifest(tmp_path):
    record = _record(tmp_path)
    manifest = json.loads(record.manifest_path.read_text(encoding="utf-8"))
    producer = {
        "run_id": manifest["run_id"], "command": "forward-signal", "status": "success",
        "completed_at": "2026-09-24T08:20:00+00:00", "signal_sha256": record.sha256,
        "signal_manifest_sha256": record.manifest_sha256, "signal_rows": record.row_count,
        "prediction_sha256": record.predictions_sha256, "prediction_rows": record.prediction_row_count,
        "outputs": {"forward_signals": str(record.signals_path),
                    "forward_signal_manifest": str(record.manifest_path),
                    "forward_predictions": str(record.predictions_path)},
    }
    producer_path = tmp_path / "synthetic-producer.json"
    producer_path.write_text(json.dumps(producer), encoding="utf-8")
    finalize_signal_record(record.directory, producer_path)
    assert len(load_prediction_record(record.directory)) == 3
    _rehash(record, {"data_asof": "2026-09-25"})
    with pytest.raises(SignalValidationError, match="completion receipt differs"):
        load_prediction_record(record.directory)
