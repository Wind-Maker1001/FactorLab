"""Production integration on openly synthetic input, with no network calls."""

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pandas as pd
import pytest
import requests
from pandas.testing import assert_frame_equal

from examples.research import forward_evaluation_synthetic_demo as demo
from qbot.research.forward.integrity import ForwardIntegrityError
from qbot.research.forward.signal_recorder import load_signal_record
from qbot.research.reports.forward import (
    ForwardEvaluationReportError, load_forward_evaluation_report,
)


def _no_network(*args, **kwargs):
    raise AssertionError("Synthetic software demonstration attempted network access")


@pytest.fixture(scope="module")
def completed_demo(tmp_path_factory):
    destination = tmp_path_factory.mktemp("forward-evaluation-synthetic") / "output"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket, "create_connection", _no_network)
        patch.setattr(requests.sessions.Session, "request", _no_network)
        manifest = demo.run_demo(destination, seed=42)
    return destination, manifest


def _predictions(manifest):
    return pd.read_parquet(Path(manifest["evaluation_report"]) / "forward_predictions.parquet")


def test_real_production_pipeline_with_synthetic_flags_and_completion(completed_demo):
    destination, manifest = completed_demo
    assert json.loads((destination / "manifest.json").read_text(encoding="utf-8")) == manifest
    assert demo.WARNING in (destination / "SYNTHETIC_SOFTWARE_DEMO_ONLY.txt").read_text(encoding="utf-8")
    assert demo.WARNING in (destination / "README.md").read_text(encoding="utf-8")
    for name, value in demo.FLAGS.items():
        assert manifest[name] is value
    assert manifest["published_records"] == 5
    assert manifest["matched_records"] == 3
    assert manifest["prediction_rows"] == 18
    assert manifest["label_status_counts"] == {"MATURE": 5, "MATURE_MISSING_PRICE": 1, "IMMATURE": 12}
    assert manifest["report_completion_verified"] is True
    assert manifest["original_signal_bytes_unchanged"] is True
    assert manifest["signal_git_commit_is_synthetic"] is True
    assert manifest["run_git_commit_is_actual_environment"] is True
    assert manifest["as_of"] == "2099-01-22"
    report = load_forward_evaluation_report(Path(manifest["evaluation_report"]))
    assert report.directory.parent == destination / "evaluations"
    assert report.directory.name == manifest["evaluation_run_id"]
    assert report.manifest_sha256 == manifest["evaluation_manifest_sha256"]
    assert report.artifact_sha256 == manifest["evaluation_artifact_sha256"]
    assert demo.WARNING in (report.directory / "summary.md").read_text(encoding="utf-8")
    for name, value in demo.FLAGS.items():
        assert report.manifest["provenance"][name] is value


def test_selection_uses_first_on_time_not_late_or_better_reverse_variant(completed_demo):
    _, manifest = completed_demo
    chosen = manifest["selected"]
    assert [row["signal_date"] for row in chosen] == ["2099-01-13", "2099-01-15", "2099-01-21"]
    assert [row["version"] for row in chosen] == ["v000001"] * 3
    assert {row["reason"] for row in manifest["excluded"]} == {"NOT_FIRST_ON_TIME", "LATE_REISSUE"}
    assert {row["version"] for row in manifest["excluded"]} == {"v000002", "v000003"}
    frame = _predictions(manifest)
    for row in chosen:
        original = load_signal_record(row["directory"]).predictions
        matched = frame.loc[frame.signal_date == row["signal_date"]].reset_index(drop=True)
        assert_frame_equal(matched[list(original.columns)], original, check_dtype=False)
        assert matched["score"].tolist() == [6, 5, 4, 3, 2, 1]
        assert matched["rank"].tolist() == [1, 2, 3, 4, 5, 6]
        assert matched["symbol"].tolist() == list(demo.SYMBOLS)


def test_missing_original_top_rank_is_not_replaced_and_immature_coverage_is_zero(completed_demo):
    _, manifest = completed_demo
    frame = _predictions(manifest)
    missing = frame.loc[(frame.signal_date == "2099-01-13") & (frame.symbol == "000001.SZ")].iloc[0]
    assert missing.label_status == "MATURE_MISSING_PRICE"
    assert missing["rank"] == 1
    assert missing.score == 6
    assert missing.entry_date == pd.Timestamp("2099-01-14")
    assert missing.exit_date == pd.Timestamp("2099-01-21")
    assert pd.isna(missing.EXCESS_RET5)
    immature = frame.loc[frame.signal_date.isin(["2099-01-15", "2099-01-21"])]
    assert len(immature) == 12
    assert immature.label_status.eq("IMMATURE").all()
    assert immature.EXCESS_RET5.isna().all()
    daily = pd.read_parquet(Path(manifest["evaluation_report"]) / "prediction_daily.parquet")
    assert daily.iloc[0].top_count == 3
    assert daily.iloc[0].top_label_count == 2
    assert daily.iloc[0].top_label_coverage == pytest.approx(2 / 3)
    assert daily.iloc[0].coverage == pytest.approx(5 / 6)
    # The chosen artificial ranks are intentionally inferior to the reverse
    # variant. Honest selection is not permitted to improve this negative IC.
    assert daily.iloc[0].prediction_rank_ic == pytest.approx(-1.0)
    assert daily.iloc[1:].coverage.eq(0).all()
    assert daily.iloc[1:].prediction_rank_ic.isna().all()


def test_actual_environment_manifest_and_all_input_signal_hashes(completed_demo):
    destination, manifest = completed_demo
    producer = json.loads(Path(manifest["evaluation_run_manifest"]).read_text(encoding="utf-8"))
    assert producer["command"] == "evaluate-forward"
    assert producer["status"] == "success"
    assert producer["config"]["forward"]["enabled"] is False
    assert producer["config"]["live_trading"] is False
    assert producer["random_seed"] == 42
    assert producer["git_commit"] != "c" * 40
    assert producer["python_version"]
    assert producer["package_versions"]["pandas"]
    assert "git_dirty" in producer
    assert producer["forward_evaluation_artifact_sha256"] == manifest["evaluation_artifact_sha256"]
    assert producer["outputs"]["forward_evaluation_report"] == manifest["evaluation_report"]
    for relative, digest in manifest["synthetic_input_sha256"].items():
        assert hashlib.sha256((destination / relative).read_bytes()).hexdigest() == digest
    for relative, digest in manifest["original_signal_sha256"].items():
        assert hashlib.sha256((Path(manifest["signals_root"]) / relative).read_bytes()).hexdigest() == digest
    stock = pd.read_parquet(destination / "stock_bars.parquet")
    assert not ((stock.symbol == "000001.SZ") & (stock.date == pd.Timestamp("2099-01-14"))).any()
    benchmark = pd.read_parquet(destination / "benchmark_bars.parquet")
    assert all(pd.Timestamp(day).dayofweek < 5 for day in demo.SIGNAL_DATES)
    assert set(pd.Timestamp(day) for day in demo.SIGNAL_DATES).issubset(set(benchmark.date))


def test_repeat_run_refuses_and_preserves_every_existing_byte(completed_demo):
    destination, _ = completed_demo
    before = demo._file_snapshot(destination)
    with pytest.raises(FileExistsError, match="non-empty"):
        demo.run_demo(destination)
    assert demo._file_snapshot(destination) == before


def test_completion_failure_marks_evaluation_failed_and_preserves_orphan(tmp_path, monkeypatch):
    destination = tmp_path / "completion-failure"

    def fail_completion(*args, **kwargs):
        raise OSError("simulated synthetic receipt disk failure")

    monkeypatch.setattr(demo, "finalize_forward_evaluation_report", fail_completion)
    with pytest.raises(OSError, match="receipt disk failure"):
        demo.run_demo(destination)
    runs = [json.loads(path.read_text(encoding="utf-8"))
            for path in (destination / "runtime" / "runs").glob("*/manifest.json")]
    evaluation = next(run for run in runs if run["command"] == "evaluate-forward")
    assert evaluation["status"] == "failed"
    assert "receipt disk failure" in evaluation["error"]
    with pytest.raises(ForwardEvaluationReportError, match="orphan"):
        load_forward_evaluation_report(Path(evaluation["outputs"]["forward_evaluation_report"]))
    assert not (destination / "manifest.json").exists()


def test_completed_report_reader_rejects_an_orphan(tmp_path, completed_demo):
    _, manifest = completed_demo
    source = Path(manifest["evaluation_report"])
    target = tmp_path / source.name
    target.mkdir()
    for file in source.iterdir():
        if file.is_file():
            with (target / file.name).open("xb") as stream:
                stream.write(file.read_bytes())
    with pytest.raises(ForwardEvaluationReportError, match="completion"):
        load_forward_evaluation_report(target)
    assert load_forward_evaluation_report(target, require_completion=False).manifest_sha256 == manifest["evaluation_manifest_sha256"]


def test_existing_file_and_nonempty_directory_rejected_without_writes(tmp_path):
    file = tmp_path / "file"
    file.write_bytes(b"keep me")
    with pytest.raises(FileExistsError):
        demo.run_demo(file)
    assert file.read_bytes() == b"keep me"
    directory = tmp_path / "nonempty"
    directory.mkdir()
    marker = directory / "user-data"
    marker.write_bytes(b"preserve user data")
    with pytest.raises(FileExistsError):
        demo.run_demo(directory)
    assert list(directory.iterdir()) == [marker]
    assert marker.read_bytes() == b"preserve user data"


def test_output_links_and_parent_links_are_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "link"
    try:
        linked.symlink_to(actual, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Environment cannot create directory symlinks")
    for destination in (linked, linked / "child"):
        with pytest.raises(ForwardIntegrityError, match="symlink|reparse"):
            demo.run_demo(destination)
    assert list(actual.iterdir()) == []


@pytest.mark.parametrize("seed", [True, -1, 1.5, 2 ** 32])
def test_invalid_seed_does_not_create_output(tmp_path, seed):
    destination = tmp_path / "invalid-seed"
    with pytest.raises(ValueError, match="seed"):
        demo.run_demo(destination, seed=seed)
    assert not destination.exists()


def test_seed_controls_generated_inputs_reproducibly():
    first_stock, first_index = demo.build_synthetic_bars(42)
    repeated_stock, repeated_index = demo.build_synthetic_bars(42)
    other_stock, other_index = demo.build_synthetic_bars(43)
    assert_frame_equal(first_stock, repeated_stock)
    assert_frame_equal(first_index, repeated_index)
    assert not first_stock.adj_open.equals(other_stock.adj_open)
    assert_frame_equal(first_index, other_index)


def test_module_cli_runs_real_production_apis_from_another_cwd(tmp_path):
    destination = tmp_path / "cli-demo"
    repo = Path(demo.__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repo) + os.pathsep + environment.get("PYTHONPATH", "")
    process = subprocess.run([
        sys.executable, "-m", "examples.research.forward_evaluation_synthetic_demo",
        "--output", str(destination), "--seed", "42",
    ], cwd=str(tmp_path), env=environment, capture_output=True, text=True, timeout=60)
    assert process.returncode == 0, process.stdout + process.stderr
    assert demo.WARNING in process.stderr
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["prediction_rows"] == 18
    assert load_forward_evaluation_report(Path(manifest["evaluation_report"])).completion_path.is_dir()
