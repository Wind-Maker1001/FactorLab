"""Phase 4 forward signals are validated and published append-only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import hashlib
import json

import pandas as pd
import pytest

from qbot.research.forward import signal_recorder as recorder_module
from qbot.research.forward.signal_recorder import (
    FORWARD_SIGNAL_COLUMNS,
    SignalLockTimeoutError,
    SignalRecorder,
    SignalValidationError,
    SignalVersionConflictError,
    finalize_signal_record,
    load_signal_record,
)


COMMIT = "a" * 40
ACTIVATION_MANIFEST = "b" * 64
RESEARCH_CONTRACT = "c" * 64
MODEL_BUNDLE = "d" * 64
DATASET = "e" * 64
UNIVERSE = "f" * 64
SOURCE = "1" * 64
SIGNAL_DAY = date(2026, 9, 24)


def _signals():
    # Deliberately not stored in rank order; publication is canonical.
    return pd.DataFrame(
        {
            "symbol": ["000002.SZ", "600000.SH"],
            "score": [0.25, 0.75],
            "rank": [2, 1],
            "target_weight": [0.4, 0.6],
        }
    )


def _record_kwargs(**updates):
    values = {
        "signal_date": SIGNAL_DAY,
        "data_asof": SIGNAL_DAY,
        "generated_at": datetime(2026, 9, 24, 16, 10, tzinfo=timezone.utc),
        "model_name": "ridge",
        "model_version": "ridge-forward-v1",
        "model_bundle_sha256": MODEL_BUNDLE,
        "dataset_sha256": DATASET,
        "git_commit": COMMIT,
        "run_id": "forward-run-001",
        "universe_version": UNIVERSE,
        "activation_id": "2026-09-24-cccccccccccccccc",
        "activation_manifest_sha256": ACTIVATION_MANIFEST,
        "research_contract_sha256": RESEARCH_CONTRACT,
        "source_sha256": SOURCE,
        "timeliness": "ON_TIME",
    }
    values.update(updates)
    return values


def _recorder(tmp_path, **updates):
    options = {
        "activation_date": SIGNAL_DAY,
        "lock_timeout": 1.0,
        "lock_poll_interval": 0.001,
    }
    options.update(updates)
    return SignalRecorder(tmp_path / "forward", **options)


def test_record_publishes_canonical_content_and_sha256_manifest(tmp_path):
    result = _recorder(tmp_path).record(_signals(), **_record_kwargs())

    assert result.version == "v000001"
    assert result.directory == tmp_path / "forward" / "2026-09-24" / "v000001"
    assert result.signals_path.is_file()
    assert result.manifest_path.is_file()
    assert (result.directory / "manifest.sha256").is_file()

    content = result.signals_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert result.sha256 == digest == manifest["sha256"]
    assert result.manifest_sha256 == hashlib.sha256(
        result.manifest_path.read_bytes()
    ).hexdigest()
    assert manifest["files"]["signals.csv"]["sha256"] == digest
    assert manifest["files"]["signals.csv"]["bytes"] == len(content)
    assert manifest["row_count"] == 2
    assert manifest["activation_date"] == "2026-09-24"
    assert manifest["live_trading"] is False
    assert manifest["run_id"] == "forward-run-001"
    assert manifest["model_bundle_sha256"] == MODEL_BUNDLE
    assert manifest["dataset_sha256"] == DATASET
    assert manifest["activation_manifest_sha256"] == ACTIVATION_MANIFEST
    assert manifest["research_contract_sha256"] == RESEARCH_CONTRACT
    assert manifest["source_sha256"] == SOURCE
    assert manifest["timeliness"] == "ON_TIME"

    recorded = pd.read_csv(result.signals_path, dtype={"symbol": str})
    assert tuple(recorded.columns) == FORWARD_SIGNAL_COLUMNS
    assert recorded["symbol"].tolist() == ["600000.SH", "000002.SZ"]
    assert recorded["rank"].tolist() == [1, 2]
    assert recorded["run_id"].tolist() == ["forward-run-001"] * 2
    assert recorded["activation_id"].nunique() == 1
    assert recorded["timeliness"].tolist() == ["ON_TIME"] * 2
    loaded = load_signal_record(result.directory, require_completion=False)
    assert loaded.sha256 == result.sha256
    assert loaded.manifest_sha256 == result.manifest_sha256


def test_loader_rejects_manifest_csv_and_inventory_tampering(tmp_path):
    first = _recorder(tmp_path / "manifest").record(
        _signals(), **_record_kwargs()
    )
    first.manifest_path.write_bytes(first.manifest_path.read_bytes() + b" ")
    with pytest.raises(SignalValidationError, match="manifest SHA-256"):
        load_signal_record(first.directory, require_completion=False)

    second = _recorder(tmp_path / "csv").record(_signals(), **_record_kwargs())
    second.signals_path.write_bytes(second.signals_path.read_bytes() + b"\n")
    with pytest.raises(SignalValidationError, match="byte count"):
        load_signal_record(second.directory, require_completion=False)

    third = _recorder(tmp_path / "inventory").record(
        _signals(), **_record_kwargs()
    )
    (third.directory / "unexpected.txt").write_text("tamper", encoding="utf-8")
    with pytest.raises(SignalValidationError, match="contain exactly"):
        load_signal_record(third.directory, require_completion=False)


def test_formal_loader_requires_successful_producer_completion_receipt(tmp_path):
    result = _recorder(tmp_path).record(_signals(), **_record_kwargs())
    with pytest.raises(SignalValidationError, match="completion receipt"):
        load_signal_record(result.directory)

    run_manifest = tmp_path / "runs" / "forward-run-001" / "manifest.json"
    run_manifest.parent.mkdir(parents=True)
    run_manifest.write_text(
        json.dumps(
            {
                "run_id": "forward-run-001",
                "command": "forward-signal",
                "status": "success",
                "completed_at": "2026-09-24T08:20:00+00:00",
                "signal_sha256": result.sha256,
                "signal_manifest_sha256": result.manifest_sha256,
                "signal_rows": result.row_count,
                "outputs": {
                    "forward_signals": str(result.signals_path),
                    "forward_signal_manifest": str(result.manifest_path),
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    finalized = finalize_signal_record(result.directory, run_manifest)
    assert finalized.completion_path.is_dir()
    loaded = load_signal_record(result.directory)
    assert loaded.sha256 == result.sha256


def test_failed_producer_run_cannot_finalize_signal(tmp_path):
    result = _recorder(tmp_path).record(_signals(), **_record_kwargs())
    run_manifest = tmp_path / "failed.json"
    run_manifest.write_text(
        json.dumps(
            {
                "run_id": "forward-run-001",
                "command": "forward-signal",
                "status": "failed",
                "completed_at": "2026-09-24T08:20:00+00:00",
                "signal_sha256": result.sha256,
                "signal_manifest_sha256": result.manifest_sha256,
                "signal_rows": result.row_count,
                "outputs": {
                    "forward_signals": str(result.signals_path),
                    "forward_signal_manifest": str(result.manifest_path),
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SignalValidationError, match="does not authorize"):
        finalize_signal_record(result.directory, run_manifest)
    assert not result.completion_path.exists()


def test_same_signal_date_rerun_gets_a_new_version_without_mutation(tmp_path):
    recorder = _recorder(tmp_path)
    first = recorder.record(_signals(), **_record_kwargs())
    original_signals = first.signals_path.read_bytes()
    original_manifest = first.manifest_path.read_bytes()

    second = recorder.record(
        _signals(),
        **_record_kwargs(
            generated_at="2026-09-25T09:00:00+08:00",
            run_id="forward-run-002",
            timeliness="LATE_REISSUE",
            reissue_reason="upstream data correction was approved",
        ),
    )

    assert second.version == "v000002"
    assert second.directory != first.directory
    assert first.signals_path.read_bytes() == original_signals
    assert first.manifest_path.read_bytes() == original_manifest
    late_manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert late_manifest["timeliness"] == "LATE_REISSUE"
    assert late_manifest["reissue_reason"] == "upstream data correction was approved"


def test_explicit_existing_version_is_a_conflict_not_an_overwrite(tmp_path):
    recorder = _recorder(tmp_path)
    first = recorder.record(_signals(), version="daily-release", **_record_kwargs())
    original = first.signals_path.read_bytes()

    with pytest.raises(SignalVersionConflictError, match="already exists"):
        recorder.record(
            _signals().assign(score=[0.1, 0.9]),
            version="daily-release",
            **_record_kwargs(),
        )
    assert first.signals_path.read_bytes() == original


@pytest.mark.parametrize(
    "signals, message",
    [
        (_signals().assign(symbol=["000002.sz", "600000.SH"]), "canonical"),
        (_signals().assign(rank=[1, 1]), "ranks must be unique"),
        (_signals().assign(rank=[3, 1]), "contiguous"),
        (_signals().assign(rank=[1, 2]), "descending score"),
        (_signals().assign(target_weight=[0.4, 0.4]), "sum to 1"),
        (_signals().assign(target_weight=[-0.1, 1.1]), "between 0 and 1"),
        (_signals().assign(score=[float("nan"), 0.75]), "finite"),
    ],
)
def test_signal_values_are_strictly_validated(tmp_path, signals, message):
    with pytest.raises(SignalValidationError, match=message):
        _recorder(tmp_path).record(signals, **_record_kwargs())
    assert not (tmp_path / "forward").exists()


@pytest.mark.parametrize(
    "recorder_updates, record_updates, message",
    [
        (
            {"activation_date": "2026-09-25"},
            {},
            "historical backfill is forbidden",
        ),
        ({}, {"data_asof": "2026-09-25"}, "data_asof"),
        ({}, {"generated_at": datetime(2026, 9, 24, 16, 0)}, "UTC offset"),
        (
            {},
            {"generated_at": "2026-09-23T23:59:00+00:00"},
            "local date",
        ),
        (
            {},
            {"generated_at": "2026-09-24T00:30:00+14:00"},
            "UTC date",
        ),
        (
            {},
            {
                "generated_at": "2026-09-25T00:01:00+08:00",
                "timeliness": "ON_TIME",
            },
            "ON_TIME requires",
        ),
        (
            {},
            {"timeliness": "LATE_REISSUE"},
            "reissue_reason",
        ),
    ],
)
def test_activation_generation_and_timeliness_contracts(
    tmp_path, recorder_updates, record_updates, message
):
    with pytest.raises(SignalValidationError, match=message):
        _recorder(tmp_path, **recorder_updates).record(
            _signals(), **_record_kwargs(**record_updates)
        )


def test_existing_exclusive_lock_times_out_clearly(tmp_path):
    recorder = _recorder(tmp_path, lock_timeout=0.0)
    recorder.root.mkdir(parents=True)
    lock_path = recorder.lock_path(SIGNAL_DAY)
    lock_path.write_text("owned-by-another-process\n", encoding="utf-8")

    with pytest.raises(SignalLockTimeoutError, match="timed out acquiring"):
        recorder.record(_signals(), **_record_kwargs())
    assert lock_path.read_text(encoding="utf-8") == "owned-by-another-process\n"


def test_concurrent_writers_allocate_distinct_complete_versions(tmp_path):
    recorder = _recorder(tmp_path, lock_timeout=5.0)

    def publish(index):
        return recorder.record(
            _signals(),
            **_record_kwargs(run_id="forward-run-%03d" % index),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(publish, range(4)))

    assert sorted(result.version for result in results) == [
        "v000001",
        "v000002",
        "v000003",
        "v000004",
    ]
    assert all(result.signals_path.is_file() for result in results)
    assert all(result.manifest_path.is_file() for result in results)
    assert not recorder.lock_path(SIGNAL_DAY).exists()


def test_failed_staging_write_never_publishes_a_formal_version(tmp_path, monkeypatch):
    original_write = recorder_module._write_exclusive
    calls = []

    def fail_manifest(path, content):
        calls.append(path.name)
        if path.name == "manifest.json":
            raise OSError("simulated disk failure")
        return original_write(path, content)

    monkeypatch.setattr(recorder_module, "_write_exclusive", fail_manifest)
    recorder = _recorder(tmp_path)
    with pytest.raises(OSError, match="simulated disk failure"):
        recorder.record(_signals(), **_record_kwargs())

    date_directory = recorder.root / SIGNAL_DAY.isoformat()
    assert calls == ["signals.csv", "manifest.json"]
    assert date_directory.is_dir()
    assert list(date_directory.iterdir()) == []
    assert not recorder.lock_path(SIGNAL_DAY).exists()
