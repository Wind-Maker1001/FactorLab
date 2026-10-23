"""Fresh repair must not reuse or relabel an incompatible active cache."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess

import pandas as pd
import pytest

from examples.research import refetch_benchmark as example
from qbot.research.config import (
    DataConfig, PathsConfig, ProjectConfig, ResearchConfig, UniverseConfig,
    discover_repository_root,
)
from qbot.research.data.cache import ParquetBarCache
from qbot.research.data.schema import empty_bars
from qbot.research.errors import ConfigurationError, DataQualityError, SchemaError
from tests.research.test_real_factor_label_smoke import SyntheticProvider


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    import requests

    def forbidden(*args, **kwargs):
        raise AssertionError("No actual HTTP allowed in benchmark unit tests")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest.fixture
def inputs(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=85)
    config = ResearchConfig(
        project=ProjectConfig("synthetic-benchmark-repair-test-only"),
        data=DataConfig(dates[10].date(), dates[-1].date(), warmup_trading_days=5),
        universe=UniverseConfig(min_listed_days=5, data_quality_window_days=5,
                                zero_volume_window_days=5), factors=("REV1",),
        paths=PathsConfig(data_root=tmp_path / "data", results_root=tmp_path / "results"),
    )
    object.__setattr__(config, "repo_root", discover_repository_root())
    config.validate()
    return config, SyntheticProvider(dates), dates


def _destination(config, name="fresh"):
    return config.paths.data_root / "diagnostics" / "benchmark-refetch" / name


def test_fresh_cache_uses_real_cache_contract_and_preserves_old_bytes(inputs):
    config, provider, dates = inputs
    config.paths.benchmark_dir.mkdir(parents=True)
    old = config.paths.benchmark_dir / "000905.SH.metadata.json"
    old.write_bytes(b'{"metadata_version":1}')
    receipt = example._fresh_cache(config, _destination(config), provider)
    assert old.read_bytes() == b'{"metadata_version":1}'
    assert len(provider.calls) == 1
    assert provider.calls[0][0] == "benchmark"
    assert receipt["metadata"]["metadata_version"] == 3
    assert receipt["returned_rows"] == 85
    assert receipt["research_sessions"] == 75
    assert receipt["warmup_sessions"] == 5
    assert receipt["active_cache_changed"] is False
    assert receipt["http_request_count"] is None
    assert receipt["historical_availability_proven"] is False
    cache = ParquetBarCache(Path(receipt["cache_root"]))
    snapshot = cache.load_snapshot("000905.SH")
    assert receipt["metadata_sha256"] == snapshot.metadata_sha256
    assert json.loads((cache.root / "completion.json").read_text(encoding="utf-8")) == receipt


def test_existing_candidate_refused_before_provider_call(inputs):
    config, provider, _ = inputs
    destination = _destination(config)
    destination.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        example._fresh_cache(config, destination, provider)
    assert not provider.calls


@pytest.mark.parametrize("defect", ["empty", "start", "end", "warmup", "outside", "schema", "open"])
def test_invalid_fresh_response_never_publishes_cache(inputs, defect):
    config, provider, dates = inputs
    if defect == "empty":
        provider.benchmark = empty_bars()
    elif defect in ("start", "end"):
        removed = dates[10] if defect == "start" else dates[-1]
        provider.benchmark = provider.benchmark.loc[provider.benchmark.date != removed]
    elif defect == "warmup":
        provider.benchmark = provider.benchmark.loc[provider.benchmark.date >= dates[7]]
    elif defect == "outside":
        provider.get_index_bars = lambda *args: provider.benchmark.assign(date=dates[-1] + pd.Timedelta(days=1))
    elif defect == "schema":
        provider.benchmark.loc[provider.benchmark.index[-1], "high"] = -1.0
    else:
        provider.benchmark.loc[provider.benchmark.index[20], [
            "open", "high", "low", "close", "raw_open", "raw_high", "raw_low", "raw_close",
            "adj_open", "adj_high", "adj_low", "adj_close", "adj_factor",
        ]] = float("nan")
    destination = _destination(config)
    with pytest.raises((DataQualityError, SchemaError, example.FactorLabError)):
        example._fresh_cache(config, destination, provider)
    assert not list(destination.glob("*.parquet"))
    assert not (destination / "completion.json").exists()


@pytest.mark.parametrize("deadline", [True, 0, -1, 601, float("nan"), float("inf"), "120"])
def test_invalid_deadline_has_no_run_or_network(inputs, deadline):
    config, provider, _ = inputs
    with pytest.raises(ConfigurationError):
        example.run_refetch(config, deadline_seconds=deadline)
    assert not config.paths.runs_dir.exists()
    assert not provider.calls


def test_non_akshare_config_has_no_run(inputs):
    config, _, _ = inputs
    config = replace(config, data=replace(config.data, provider="local_parquet"))
    with pytest.raises(ConfigurationError, match="requires data.provider=akshare"):
        example.run_refetch(config)
    assert not config.paths.runs_dir.exists()


def _mock_child(monkeypatch, provider, behavior="success"):
    monkeypatch.setattr(example, "_provider", lambda config: provider)

    def execute(command, log_path, deadline, cwd):
        log_path.write_bytes(b"Synthetic SDK boundary only\n")
        if behavior == "timeout":
            raise subprocess.TimeoutExpired(command, deadline)
        if behavior == "failed":
            return subprocess.CompletedProcess(command, 1)
        worker_config = example.load_config(command[command.index("--config") + 1])
        destination = Path(command[command.index("--destination") + 1])
        example._fresh_cache(worker_config, destination, provider)
        if behavior in {"receipt-tamper", "statistics-tamper", "config-tamper"}:
            receipt_path = destination / "completion.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if behavior == "receipt-tamper":
                receipt["metadata_sha256"] = "0" * 64
            elif behavior == "statistics-tamper":
                receipt["research_sessions"] = 999
            else:
                Path(command[command.index("--config") + 1]).write_bytes(b"{}")
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(example, "_execute_child", execute)


def test_parent_verifies_generation_and_records_exact_frozen_config(inputs, monkeypatch):
    config, provider, _ = inputs
    _mock_child(monkeypatch, provider)
    result = example.run_refetch(config, deadline_seconds=7)
    manifest = result["manifest"]
    assert manifest["status"] == "success"
    assert manifest["worker_terminal"] is True
    assert manifest["worker_returncode"] == 0
    assert manifest["point_in_time_universe_verified"] is False
    assert manifest["active_cache_changed"] is False
    frozen = Path(manifest["outputs"]["worker_config"])
    assert hashlib.sha256(frozen.read_bytes()).hexdigest() == manifest["worker_config_sha256"]
    assert json.loads(frozen.read_text(encoding="utf-8")) == config.to_dict()
    assert not config.paths.benchmark_dir.exists()
    assert json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8")) == manifest


@pytest.mark.parametrize("behavior", ["timeout", "failed", "receipt-tamper", "statistics-tamper", "config-tamper"])
def test_parent_failure_is_terminal_not_success_or_active_cache_repair(inputs, monkeypatch, behavior):
    config, provider, _ = inputs
    _mock_child(monkeypatch, provider, behavior)
    result = example.run_refetch(config, deadline_seconds=7)
    manifest = result["manifest"]
    assert manifest["status"] == "failed"
    assert manifest["completed_at"] is not None
    assert manifest["worker_terminal"] is True
    assert manifest["active_cache_changed"] is False
    assert "actual_data" not in manifest
    assert not config.paths.benchmark_dir.exists()
    if behavior == "timeout":
        assert manifest["worker_timed_out"] is True


def test_real_child_deadline_is_reaped_and_log_retained(tmp_path):
    import sys

    log_path = tmp_path / "worker.log"
    # This real process has no network calls. The network boundary above is
    # synthetic; subprocess timeout behavior is tested with an actual child.
    with pytest.raises(subprocess.TimeoutExpired):
        example._execute_child([sys.executable, "-c", "import time; time.sleep(10)"], log_path, 0.05, tmp_path)
    assert log_path.exists()


def test_main_success_and_failure_exit_codes(inputs, monkeypatch):
    config, _, _ = inputs
    monkeypatch.setattr(example, "load_config", lambda path: config)
    monkeypatch.setattr(example, "run_refetch", lambda *args, **kwargs: {"manifest": {"status": "success"}})
    assert example.main([]) == 0
    monkeypatch.setattr(example, "run_refetch", lambda *args, **kwargs: {"manifest": {"status": "failed"}})
    assert example.main([]) == 1
    assert example.main(["--destination", "somewhere"]) == 1


def test_weekend_in_required_range_rejected_not_silently_removed(inputs):
    config, provider, _ = inputs
    extra = provider.benchmark.iloc[[20]].copy().assign(date=pd.Timestamp("2024-01-27"))
    provider.benchmark = pd.concat([provider.benchmark, extra], ignore_index=True)
    with pytest.raises(SchemaError, match="weekend"):
        example._fresh_cache(config, _destination(config), provider)
    assert not list(_destination(config).glob("*.parquet"))


def test_unused_weekend_preserved_as_source_evidence_not_required_session(inputs, monkeypatch):
    config, provider, _ = inputs
    extra = provider.benchmark.iloc[[0]].copy().assign(date=pd.Timestamp("2023-12-30"))
    provider.benchmark = pd.concat([extra, provider.benchmark], ignore_index=True)
    _mock_child(monkeypatch, provider)
    result = example.run_refetch(config)
    assert result["manifest"]["status"] == "success"
    data = result["manifest"]["actual_data"]
    assert data["returned_rows"] == 86
    assert data["research_sessions"] == 75
    assert data["warmup_sessions"] == 5
    assert data["unused_weekend_observations"] == ["2023-12-30"]
    assert data["required_weekend_observations"] == 0


def test_worker_cannot_target_active_cache_even_when_it_does_not_exist(inputs):
    config, provider, _ = inputs
    with pytest.raises(ConfigurationError, match="diagnostic directory"):
        example._fresh_cache(config, config.paths.benchmark_dir, provider)
    assert not provider.calls
    assert not config.paths.benchmark_dir.exists()
