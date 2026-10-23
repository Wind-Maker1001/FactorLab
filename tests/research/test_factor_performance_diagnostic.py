"""Measurement correctness, isolation and immutable synthetic benchmark evidence."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pandas as pd
import pytest

from examples.research import benchmark_factor_evaluation as benchmark
from examples.research import scale_factor_pipeline as scale
from examples.research.factor_performance import PerformanceRecorder
from factorlab.config import PathsConfig, load_config
from factorlab.metrics import factor
from tests.research.test_factor_metrics_golden import golden_input
from tests.research.reference.compare import assert_evaluation_equal


@pytest.fixture
def config(tmp_path, monkeypatch):
    original = load_config("configs/research/csi500_5y.yaml")
    configured = replace(original, paths=PathsConfig(data_root=tmp_path / "data", results_root=tmp_path / "results"))
    object.__setattr__(configured, "repo_root", original.repo_root)
    object.__setattr__(configured, "config_path", original.config_path)
    monkeypatch.setattr(scale, "load_config", lambda unused: configured)
    monkeypatch.setattr(benchmark, "load_config", lambda unused: configured)
    def forbidden(*args, **kwargs):
        raise AssertionError("Performance diagnostics must never access network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    return configured


def create_source(config, flags=()):
    assert scale.main(["--stocks", "20", "--research-days", "20", "--warmup-days", "130", *flags]) == 0
    return sorted(config.paths.runs_dir.glob("*/manifest.json"))[-1]


def test_instrumentation_preserves_tables_and_restores_functions():
    frame, dates = golden_input("infinity")
    expected = factor.evaluate_factors(frame, ["F1", "F2"], calendar=dates, minimum_cross_section=5)
    original = factor._daily_ic
    with PerformanceRecorder() as recorder, recorder.instrument_evaluator(factor):
        actual = factor.evaluate_factors(frame, ["F1", "F2"], calendar=dates, minimum_cross_section=5)
    assert_evaluation_equal(actual, expected)
    assert factor._daily_ic is original
    timings = recorder.payload()["stage_timings_inclusive_seconds_do_not_add_nested_kernels"]
    assert timings["prepare_workspace"]["calls"] == 1
    assert timings["_autocorrelation"]["calls"] == 1
    assert {"pearson_ic", "rank_ic", "autocorrelation_rank"}.issubset(timings)


def test_instrumentation_restores_functions_after_failure():
    original = factor._daily_ic
    with pytest.raises(RuntimeError), PerformanceRecorder() as recorder, recorder.instrument_evaluator(factor):
        raise RuntimeError("injected failure")
    assert factor._daily_ic is original


def test_memory_sampling_is_explicit_about_phase_maximum_and_lifetime_peak():
    recorder = PerformanceRecorder()
    recorder.probe = SimpleNamespace(metric="test resident bytes", snapshot=lambda: {
        "resident_bytes": 100, "process_lifetime_peak_resident_bytes": 300})
    recorder.current_phase = "evaluation"
    recorder._sample()
    payload = recorder.payload()["process_memory"]
    assert payload["phase_sampled_maxima_not_exact_peaks"]["evaluation"]["sampled_max_resident_bytes"] == 100
    assert payload["at_recording_end"]["process_lifetime_peak_resident_bytes"] == 300
    recorder.phase_memory["evaluation"]["sampled_max_resident_bytes"] = 999
    assert payload["phase_sampled_maxima_not_exact_peaks"]["evaluation"]["sampled_max_resident_bytes"] == 100


def test_paired_benchmark_runs_both_implementations_in_fresh_processes(config):
    path = create_source(config)
    assert benchmark.main(["--source-manifest", str(path), "--repeats", "1",
                           "--scopes", "all", "--warmup-days", "2"]) == 0
    comparison = next((config.paths.results_root / "diagnostics" / "factor-performance-benchmark").glob("*/comparison.json"))
    payload = json.loads(comparison.read_text(encoding="utf-8"))
    assert payload["summaries"]["all"]["golden_all_pairs_passed"] is True
    workers = payload["workers"]
    assert [(w["run_kind"], w["implementation"]) for w in workers] == [
        ("first", "reference"), ("first", "optimized"), ("repeat", "optimized"), ("repeat", "reference")]
    assert [w["warmup_days"] for w in workers] == [0, 0, 2, 2]
    assert len({w["worker_directory"] for w in workers}) == 4


def test_detailed_scale_run_keeps_all_reports_and_bound_artifacts(config):
    path = create_source(config, ["--evaluation-scope", "regimes", "--timing-detail", "--measure-memory", "--report", "--profile"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    measured = json.loads(Path(payload["outputs"]["measurements"]).read_text(encoding="utf-8"))
    root = Path(payload["outputs"]["measurements"]).parent
    assert measured["profiled_not_normal_wall_time"] is True
    assert measured["measurements"]["daily_ic_rows"] == 560
    assert "_daily_ic" in measured["performance_details"]["stage_timings_inclusive_seconds_do_not_add_nested_kernels"]
    assert len(list((root / "factor-report").glob("*.png"))) == 5
    assert len(list((root / "factor-report").glob("*.csv"))) == 6
    completion = json.loads((root / "performance-completion.json").read_text(encoding="utf-8"))
    assert completion["success_manifest_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert completion["total_seconds_through_success_receipt"] >= measured["timings_seconds"]["total_before_output_hashing"]
    for artifact in payload["output_artifacts"].values():
        assert artifact["sha256"] == hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest()
    assert not config.paths.data_root.exists()


def test_benchmark_worker_checks_source_tables_reports_and_memory(config, tmp_path):
    path = create_source(config)
    args = SimpleNamespace(worker_output=str(tmp_path / "worker"), implementation="optimized", scope="regimes",
                           source_manifest=str(path), input_case="sparse", worker_warmup_days=2)
    assert benchmark.worker(args) == 0
    payload, result = benchmark.load_result(args.worker_output)
    assert payload["warmup_days"] == 2 and payload["rows"] < 400
    assert payload["sources_sha256"] == payload["sources_sha256_at_end"]
    assert payload["factor_evaluation_seconds"] > 0
    assert len(result.daily_ic) > 0
    assert (Path(args.worker_output) / "factor-report" / "regime_performance.png").exists()


def test_benchmark_refuses_corrupt_synthetic_input(config, tmp_path):
    path = create_source(config)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["output_artifacts"]["synthetic-dataset.parquet"]["sha256"] = "0" * 64
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="content hash"):
        benchmark.input_bundle(forged)


def test_worker_collision_preserves_old_output(config, tmp_path):
    output = tmp_path / "worker"
    output.mkdir()
    sentinel = output / "sentinel"
    sentinel.write_bytes(b"unchanged")
    args = SimpleNamespace(worker_output=str(output))
    with pytest.raises(FileExistsError):
        benchmark.worker(args)
    assert sentinel.read_bytes() == b"unchanged"
