"""Exercise the public single-stock diagnostic with genuine core components.

The provider is deliberately synthetic; no fake core, fake provenance,
historical-index membership, or real-market conclusion is asserted.
"""

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from examples.research import real_factor_label_smoke as example
from factorlab.config import (
    DataConfig, PathsConfig, ProjectConfig, ResearchConfig, UniverseConfig,
    discover_repository_root,
)
from factorlab.data.providers.base import MarketDataProvider
from factorlab.data.cache import ParquetBarCache
from factorlab.data.schema import canonicalize_bars, empty_bars
from factorlab.errors import (
    CacheError, ConfigurationError, DataQualityError, ProviderError,
    ProviderNetworkError, SchemaError,
)
from factorlab.factors.registry import FACTOR_REGISTRY
from factorlab.reproducibility import validate_provenance


def _bars(symbol, dates, scale=1.0):
    positions = np.arange(len(dates), dtype=float)
    prices = (15.0 + positions * 0.03 + np.sin(positions / 4.0) * 0.1) * scale
    frame = pd.DataFrame({
        "date": dates, "symbol": symbol,
        "open": prices, "high": prices + scale,
        "low": prices - scale, "close": prices + scale * 0.01,
        "volume": 10000.0 + positions, "amount": prices * (10000.0 + positions),
        "turnover": 0.01 + positions * 0.00001,
    })
    for name in ("open", "high", "low", "close"):
        frame["raw_" + name] = frame[name]
        frame["adj_" + name] = frame[name] * (2.0 if symbol == "600000.SH" else 1.0)
    frame["adj_factor"] = 2.0 if symbol == "600000.SH" else 1.0
    return canonicalize_bars(frame)


class SyntheticProvider(MarketDataProvider):
    """Fake only the network boundary, never cache/schema/factors/labels."""

    def __init__(self, dates):
        self.stocks = _bars("600000.SH", dates)
        self.benchmark = _bars("000905.SH", dates, 100.0)
        self.calls = []
        self.error = None
        self.contract = "synthetic-diagnostic-fixture-v1"

    def cache_source_version(self, symbol, is_index):
        return self.contract + (";index" if is_index else ";stock")

    def _fetch(self, role, symbol, start, end):
        self.calls.append((role, symbol, pd.Timestamp(start), pd.Timestamp(end)))
        if self.error is not None:
            raise self.error
        frame = self.benchmark if role == "benchmark" else self.stocks
        return frame.loc[frame["date"].between(pd.Timestamp(start), pd.Timestamp(end))].copy()

    def get_daily_bars(self, symbol, start, end):
        return self._fetch("stock", symbol, start, end)

    def get_index_bars(self, symbol, start, end):
        return self._fetch("benchmark", symbol, start, end)

    def get_trade_calendar(self, start, end):
        raise AssertionError("The diagnostic must derive the calendar from pinned benchmark bars")

    def get_index_constituents(self, symbol, as_of=None):
        raise AssertionError("No current or historical constituent queries in this diagnostic")


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    import requests
    import baostock

    def forbidden(*args, **kwargs):
        raise AssertionError("Network/BaoStock forbidden in synthetic unit tests")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(baostock, "login", forbidden)


@pytest.fixture
def inputs(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=85)
    config = ResearchConfig(
        project=ProjectConfig("synthetic-diagnostic-test-only"),
        data=DataConfig(dates[0].date(), dates[-1].date()),
        universe=UniverseConfig(), factors=tuple(FACTOR_REGISTRY),
        paths=PathsConfig(data_root=tmp_path / "data", results_root=tmp_path / "results"),
    )
    # Keep actual repo/runtime provenance. This is not a mocked Git signature.
    object.__setattr__(config, "repo_root", discover_repository_root())
    config.validate()
    return config, SyntheticProvider(dates), dates


def _run(inputs, **kwargs):
    config, provider, dates = inputs
    return example.run_smoke(
        config, start=dates[0].date(), end=dates[-1].date(), provider=provider, **kwargs
    )


def _manifest_files(config):
    return list(config.paths.runs_dir.glob("*/manifest.json"))


def test_real_core_pipeline_has_14_factors_exact_timing_and_immature_tail(inputs):
    config, provider, dates = inputs
    result = _run(inputs)
    stored = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert stored == result["manifest"]
    assert stored["status"] == "success"
    validate_provenance(stored)
    assert stored["diagnostic_only"] is True
    assert stored["research_conclusions_permitted"] is False
    assert stored["point_in_time_universe_verified"] is False
    assert stored["security_status_verified"] is False
    assert stored["rank_ic_evaluated"] is False
    assert stored["provider_injected_for_testing"] is True
    assert stored["cross_sectional_preprocessing_applied"] is False
    assert stored["warning"] == example.WARNING
    assert stored["configured_formal_warmup_applied"] is False
    assert stored["actual_data"]["benchmark_sessions"] == 85
    assert stored["actual_data"]["mature_labels"] == 79
    assert len(stored["actual_data"]["valid_factor_rows"]) == 14
    assert min(stored["actual_data"]["valid_factor_rows"].values()) > 0
    factors = pd.read_parquet(stored["outputs"]["factors"])
    labels = pd.read_parquet(stored["outputs"]["labels"])
    assert factors["MOM60"].iloc[:60].isna().all()
    assert factors["MOM60"].iloc[60:].notna().all()
    assert labels["EXCESS_RET5"].iloc[-6:].isna().all()
    assert labels["EXCESS_RET5"].iloc[:-6].notna().all()
    assert labels["entry_date"].iloc[0] == dates[1]
    assert labels["exit_date"].iloc[0] == dates[6]
    stock = provider.stocks["adj_open"]
    benchmark = provider.benchmark["adj_open"]
    assert labels["EXCESS_RET5"].iloc[0] == pytest.approx(
        stock.iloc[6] / stock.iloc[1] - benchmark.iloc[6] / benchmark.iloc[1]
    )
    assert stored["data_start"] == dates[0].date().isoformat()
    assert stored["data_end"] == dates[-1].date().isoformat()
    assert not config.paths.processed_dir.exists()
    assert not config.paths.universe_dir.exists()
    assert not config.paths.factor_results_dir.exists()
    assert not config.paths.forward_dir.exists()
    assert not config.paths.raw_dir.exists()


def test_output_and_full_cache_metadata_hashes_bind_consumed_generations(inputs):
    result = _run(inputs)
    stored = result["manifest"]
    for role, evidence in stored["cache_evidence"].items():
        metadata = evidence["metadata"]
        root = Path(evidence["cache_root"])
        pointer = ParquetBarCache(root).metadata_path(metadata["symbol"])
        assert hashlib.sha256(pointer.read_bytes()).hexdigest() == evidence["metadata_sha256"]
        assert json.loads(pointer.read_text(encoding="utf-8")) == metadata
        data_path = root / metadata["data_file"]
        assert hashlib.sha256(data_path.read_bytes()).hexdigest() == metadata["content_sha256"]
        consumed = pd.read_parquet(stored["outputs"][role + "_bars"])
        pd.testing.assert_frame_equal(consumed, pd.read_parquet(data_path))
    for artifact in stored["output_artifacts"].values():
        path = Path(artifact["path"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
        assert len(pd.read_parquet(path)) == artifact["row_count"]


def test_second_run_has_distinct_immutable_outputs_and_verified_cache_hits(inputs):
    _, provider, _ = inputs
    first = _run(inputs)
    old_paths = [Path(first["manifest_path"])] + [
        Path(value["path"]) for value in first["manifest"]["output_artifacts"].values()
    ]
    before = {path: path.read_bytes() for path in old_paths}
    calls = list(provider.calls)
    second = _run(inputs)
    assert second["manifest"]["run_id"] != first["manifest"]["run_id"]
    assert second["manifest"]["outputs"]["factors"] != first["manifest"]["outputs"]["factors"]
    assert provider.calls == calls
    assert {path: path.read_bytes() for path in old_paths} == before
    assert second["manifest"]["cache_evidence"] == first["manifest"]["cache_evidence"]


def test_force_refresh_really_refetches_and_preserves_existing_outputs(inputs):
    _, provider, _ = inputs
    first = _run(inputs)
    first_factors = Path(first["manifest"]["outputs"]["factors"])
    original_bytes = first_factors.read_bytes()
    second = _run(inputs, force_refresh=True)
    assert len(provider.calls) == 4
    assert second["manifest"]["arguments"]["force_refresh"] is True
    assert first_factors.read_bytes() == original_bytes
    assert second["manifest"]["cache_evidence"]["stock"]["metadata"]["generation_id"] != (
        first["manifest"]["cache_evidence"]["stock"]["metadata"]["generation_id"]
    )


def test_missing_stock_session_is_not_filled_or_removed(inputs):
    _, provider, dates = inputs
    provider.stocks = provider.stocks.loc[provider.stocks["date"] != dates[75]].copy()
    result = _run(inputs)
    bars = pd.read_parquet(result["manifest"]["outputs"]["stock_bars"])
    factors = pd.read_parquet(result["manifest"]["outputs"]["factors"])
    labels = pd.read_parquet(result["manifest"]["outputs"]["labels"])
    assert len(bars) == 85
    assert bars.loc[bars["date"] == dates[75], "adj_open"].isna().all()
    assert factors.loc[factors["date"].isin(dates[75:77]), "REV1"].isna().all()
    assert labels.loc[labels["date"].isin([dates[69], dates[74]]), "EXCESS_RET5"].isna().all()
    assert result["manifest"]["actual_data"]["missing_stock_open_rows"] == 1
    assert result["manifest"]["actual_data"]["mature_labels"] == 77


@pytest.mark.parametrize("which", ["first", "last"])
def test_missing_benchmark_boundary_fails_and_writes_failed_manifest(inputs, which):
    config, provider, dates = inputs
    excluded = dates[0] if which == "first" else dates[-1]
    provider.benchmark = provider.benchmark.loc[provider.benchmark["date"] != excluded].copy()
    with pytest.raises(DataQualityError, match="boundaries"):
        _run(inputs)
    stored = json.loads(_manifest_files(config)[0].read_text(encoding="utf-8"))
    assert stored["status"] == "failed"
    assert stored["error_type"] == "DataQualityError"
    assert stored["diagnostic_only"] is True
    assert stored["point_in_time_universe_verified"] is False
    assert [call[0] for call in provider.calls] == ["benchmark"]


def test_insufficient_within_window_factor_warmup_fails(inputs):
    config, provider, dates = inputs
    with pytest.raises(DataQualityError, match="warm-up: 60/61"):
        example.run_smoke(config, start=dates[0].date(), end=dates[59].date(), provider=provider)
    stored = json.loads(_manifest_files(config)[0].read_text(encoding="utf-8"))
    assert stored["status"] == "failed"
    assert [call[0] for call in provider.calls] == ["benchmark"]


def test_no_mature_labels_fails(inputs):
    config, provider, dates = inputs
    smaller = replace(config, factors=("REV1",))
    object.__setattr__(smaller, "repo_root", config.repo_root)
    with pytest.raises(DataQualityError, match="no matured"):
        example.run_smoke(smaller, start=dates[0].date(), end=dates[5].date(), provider=provider)
    stored = json.loads(_manifest_files(config)[0].read_text(encoding="utf-8"))
    assert stored["status"] == "failed"


def test_missing_turnover_cannot_claim_all_factors_valid(inputs):
    config, provider, _ = inputs
    provider.stocks["turnover"] = np.nan
    with pytest.raises(DataQualityError, match="TURN5.*TURN20"):
        _run(inputs)
    stored = json.loads(_manifest_files(config)[0].read_text(encoding="utf-8"))
    assert stored["status"] == "failed"
    assert "output_artifacts" not in stored


@pytest.mark.parametrize("error_type", [ProviderNetworkError, ProviderError, SchemaError, RuntimeError])
def test_provider_errors_preserve_original_class_and_failed_manifest(inputs, error_type):
    config, provider, _ = inputs
    error = error_type("deliberate fixture failure")
    provider.error = error
    with pytest.raises(error_type) as caught:
        _run(inputs)
    assert caught.value is error
    stored = json.loads(_manifest_files(config)[0].read_text(encoding="utf-8"))
    assert stored["status"] == "failed"
    assert stored["error_type"] == error_type.__name__
    assert stored["completed_at"] is not None


def test_changed_source_contract_cannot_reuse_cache(inputs):
    config, provider, _ = inputs
    _run(inputs)
    provider.contract = "synthetic-diagnostic-fixture-v2"
    with pytest.raises(CacheError, match="source identity mismatch"):
        _run(inputs)
    stored = [json.loads(path.read_text(encoding="utf-8")) for path in _manifest_files(config)]
    assert sorted(value["status"] for value in stored) == ["failed", "success"]
    assert len(provider.calls) == 2


def test_cache_corruption_fails_instead_of_redownloading_or_skipping(inputs):
    config, provider, _ = inputs
    result = _run(inputs)
    evidence = result["manifest"]["cache_evidence"]["stock"]
    artifact = Path(evidence["cache_root"]) / evidence["metadata"]["data_file"]
    artifact.write_bytes(b"deliberately corrupt test-owned cache")
    with pytest.raises(CacheError, match="content hash mismatch"):
        _run(inputs)
    assert len(provider.calls) == 2
    stored = [json.loads(path.read_text(encoding="utf-8")) for path in _manifest_files(config)]
    assert sorted(value["status"] for value in stored) == ["failed", "success"]


@pytest.mark.parametrize("kwargs,match", [
    ({"symbol": "600000"}, "canonical"),
    ({"retry_count": -1}, "non-negative"),
    ({"retry_count": True}, "non-negative"),
    ({"timeout_seconds": 0}, "finite and positive"),
    ({"timeout_seconds": np.inf}, "finite and positive"),
    ({"timeout_seconds": np.nan}, "finite and positive"),
    ({"timeout_seconds": True}, "finite and positive"),
])
def test_invalid_overrides_fail_before_network(inputs, kwargs, match):
    _, provider, _ = inputs
    with pytest.raises(ConfigurationError, match=match):
        _run(inputs, **kwargs)
    assert not provider.calls


def test_retry_timeout_overrides_are_explicit_and_no_proxy_mutation(inputs, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://must-not-change.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "http://must-not-change.invalid")
    monkeypatch.setenv("NO_PROXY", "example.test")
    import os
    before = dict(os.environ)
    result = _run(inputs, retry_count=0, timeout_seconds=2.5)
    stored = result["manifest"]
    assert stored["network_controls"]["retry_count"] == 0
    assert stored["network_controls"]["stock_connection_read_timeout_seconds"] == 2.5
    assert stored["network_controls"]["benchmark_native_timeout_supported"] is False
    assert stored["network_controls"]["whole_task_wall_clock_deadline_supported"] is False
    assert stored["network_controls"]["proxy_settings_modified"] is False
    assert stored["config"]["network"]["max_retries"] == 3
    assert dict(os.environ) == before


def test_main_from_other_cwd_resolves_repo_relative_config(inputs, tmp_path, monkeypatch):
    config, provider, dates = inputs
    # The loader is real; only the provider and selected artifact destinations
    # are injected after it resolves the YAML relative to the actual repo.
    original = example.load_config
    loaded_paths = []

    def load_and_relocate(path):
        loaded = original(path)
        loaded_paths.append(loaded.config_path)
        moved = replace(loaded, paths=config.paths)
        object.__setattr__(moved, "repo_root", loaded.repo_root)
        object.__setattr__(moved, "config_path", loaded.config_path)
        return moved

    monkeypatch.setattr(example, "load_config", load_and_relocate)
    monkeypatch.setattr(example, "_provider", lambda *args: provider)
    monkeypatch.chdir(tmp_path)
    assert example.main([
        "--config", "configs/research/csi500_smoke.yaml",
        "--start", dates[0].date().isoformat(), "--end", dates[-1].date().isoformat(),
        "--retry-count", "0", "--timeout-seconds", "2.5",
    ]) == 0
    assert loaded_paths == [config.repo_root / "configs/research/csi500_smoke.yaml"]
    stored = json.loads(_manifest_files(config)[0].read_text(encoding="utf-8"))
    assert stored["git_commit"] is not None
    assert stored["config_path"] == str(loaded_paths[0])


def test_main_returns_nonzero_for_provider_failure_and_keeps_manifest(inputs, monkeypatch):
    config, provider, dates = inputs
    provider.error = ProviderNetworkError("synthetic transport failure")
    monkeypatch.setattr(example, "load_config", lambda path: config)
    monkeypatch.setattr(example, "_provider", lambda *args: provider)
    assert example.main([
        "--start", dates[0].date().isoformat(), "--end", dates[-1].date().isoformat(),
    ]) == 1
    stored = json.loads(_manifest_files(config)[0].read_text(encoding="utf-8"))
    assert stored["status"] == "failed"
    assert stored["error_type"] == "ProviderNetworkError"


def test_force_refresh_empty_response_does_not_use_old_cache_to_claim_success(inputs):
    config, provider, _ = inputs
    _run(inputs)
    provider.benchmark = empty_bars()
    with pytest.raises(DataQualityError, match="Forced diagnostic refresh returned no bars"):
        _run(inputs, force_refresh=True)
    stored = [json.loads(path.read_text(encoding="utf-8")) for path in _manifest_files(config)]
    assert sorted(value["status"] for value in stored) == ["failed", "success"]


def _cache_root(result):
    return Path(result["manifest"]["cache_evidence"]["benchmark"]["cache_root"]).parent


def test_read_only_mode_never_fetches_updates_or_changes_existing_cache(inputs):
    _, provider, _ = inputs
    first = _run(inputs)
    root = _cache_root(first)
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    calls = list(provider.calls)
    provider.error = AssertionError("Read-only mode must never call a provider endpoint")
    second = _run(inputs, read_only_cache_root=root)
    manifest = second["manifest"]
    assert manifest["status"] == "success"
    assert manifest["data_access"] == "existing-verified-cache-only"
    assert manifest["input_root"] == str(root.resolve())
    assert manifest["cache_evidence"] == first["manifest"]["cache_evidence"]
    assert manifest["actual_data"]["benchmark_sessions"] == 85
    assert manifest["actual_data"]["mature_labels"] == 79
    assert provider.calls == calls
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("missing", ["root", "daily", "benchmark"])
def test_read_only_missing_root_or_cache_directory_fails_without_creating_it(inputs, tmp_path, missing):
    config, provider, _ = inputs
    root = tmp_path / "missing-input"
    if missing != "root":
        root.mkdir()
        present = "benchmark" if missing == "daily" else "daily"
        (root / present).mkdir()
    before = set(root.rglob("*")) if root.exists() else set()
    with pytest.raises(DataQualityError, match="not an existing directory|directory is missing"):
        _run(inputs, read_only_cache_root=root)
    assert not provider.calls
    assert (set(root.rglob("*")) if root.exists() else set()) == before
    assert root.exists() is (missing != "root")
    stored = json.loads(_manifest_files(config)[0].read_text(encoding="utf-8"))
    assert stored["status"] == "failed"
    assert stored["data_access"] == "existing-verified-cache-only"
    assert stored["input_root"] == str(root.resolve())


def test_read_only_empty_symbol_cache_fails_without_provider_fallback(inputs, tmp_path):
    config, provider, dates = inputs
    root = tmp_path / "empty-stock-cache"
    (root / "daily").mkdir(parents=True)
    benchmark = ParquetBarCache(root / "benchmark")
    benchmark.update(
        "000905.SH", provider.benchmark, dates[0], dates[-1],
        provider.__class__.__name__, provider.cache_source_version("000905.SH", True),
    )
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(DataQualityError, match="no verified bars: 600000.SH"):
        _run(inputs, read_only_cache_root=root)
    assert not provider.calls
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    stored = json.loads(_manifest_files(config)[0].read_text(encoding="utf-8"))
    assert stored["status"] == "failed"


@pytest.mark.parametrize("role", ["daily", "benchmark"])
def test_read_only_partial_coverage_fails_without_silent_fetch(inputs, tmp_path, role):
    config, provider, dates = inputs
    root = tmp_path / ("partial-coverage-" + role)
    for name, symbol, bars, is_index in (
        ("daily", "600000.SH", provider.stocks, False),
        ("benchmark", "000905.SH", provider.benchmark, True),
    ):
        end = dates[80] if name == role else dates[-1]
        selected = bars.loc[bars["date"] <= end]
        cache = ParquetBarCache(root / name)
        cache.update(
            symbol, selected, dates[0], end, provider.__class__.__name__,
            provider.cache_source_version(symbol, is_index),
        )
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(DataQualityError, match="does not cover the complete requested window"):
        _run(inputs, read_only_cache_root=root)
    assert not provider.calls
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    stored = json.loads(_manifest_files(config)[0].read_text(encoding="utf-8"))
    assert stored["status"] == "failed"
    assert stored["data_access"] == "existing-verified-cache-only"


def test_read_only_gap_in_declared_coverage_is_not_treated_as_fully_covered(inputs, tmp_path):
    _, provider, dates = inputs
    root = tmp_path / "gap-cache"
    daily = ParquetBarCache(root / "daily")
    daily.update(
        "600000.SH", provider.stocks, dates[0], dates[-1],
        provider.__class__.__name__, provider.cache_source_version("600000.SH", False),
    )
    benchmark = ParquetBarCache(root / "benchmark")
    for start, end in ((dates[0], dates[40]), (dates[42], dates[-1])):
        benchmark.update(
            "000905.SH", provider.benchmark.loc[provider.benchmark["date"].between(start, end)],
            start, end, provider.__class__.__name__, provider.cache_source_version("000905.SH", True),
        )
    with pytest.raises(DataQualityError, match="does not cover the complete requested window"):
        _run(inputs, read_only_cache_root=root)
    assert not provider.calls


def test_read_only_relative_root_resolves_against_repository_not_cwd(inputs, tmp_path, monkeypatch):
    import os
    config, provider, dates = inputs
    first = _run(inputs)
    absolute_root = _cache_root(first)
    # Keep actual repository/Git provenance while resolving a test-owned input
    # outside it; read-only input paths are not required to live in the repo.
    relative_root = Path(os.path.relpath(absolute_root, config.repo_root))
    different_cwd = tmp_path / "another-cwd"
    different_cwd.mkdir()
    monkeypatch.chdir(different_cwd)
    provider.error = AssertionError("No endpoint calls in read-only mode")
    result = example.run_smoke(
        config, start=dates[0].date(), end=dates[-1].date(),
        provider=provider, read_only_cache_root=relative_root,
    )
    assert result["manifest"]["input_root"] == str(absolute_root.resolve())
    assert result["manifest"]["arguments"]["read_only_cache_root"] == str(relative_root)
    validate_provenance(result["manifest"])
    assert len(provider.calls) == 2


def test_read_only_rejects_force_refresh_without_endpoint_calls(inputs):
    config, provider, _ = inputs
    first = _run(inputs)
    with pytest.raises(ConfigurationError, match="cannot be combined"):
        _run(inputs, read_only_cache_root=_cache_root(first), force_refresh=True)
    assert len(provider.calls) == 2
    stored = [json.loads(path.read_text(encoding="utf-8")) for path in _manifest_files(config)]
    assert sorted(value["status"] for value in stored) == ["failed", "success"]


def test_read_only_wrong_source_identity_is_fatal_without_endpoint_calls(inputs):
    _, provider, _ = inputs
    first = _run(inputs)
    provider.contract = "different-diagnostic-contract"
    with pytest.raises(CacheError, match="source identity mismatch"):
        _run(inputs, read_only_cache_root=_cache_root(first))
    assert len(provider.calls) == 2


@pytest.mark.parametrize("role", ["stock", "benchmark"])
@pytest.mark.parametrize("file_kind", ["metadata", "data"])
def test_read_only_missing_committed_file_fails_without_repairing_it(inputs, role, file_kind):
    config, provider, _ = inputs
    first = _run(inputs)
    evidence = first["manifest"]["cache_evidence"][role]
    cache = ParquetBarCache(Path(evidence["cache_root"]))
    if file_kind == "metadata":
        missing = cache.metadata_path(evidence["metadata"]["symbol"])
    else:
        missing = cache.root / evidence["metadata"]["data_file"]
    missing.unlink()  # Only the explicitly test-owned temporary cache file.
    expected_error = DataQualityError if file_kind == "metadata" else CacheError
    with pytest.raises(expected_error):
        _run(inputs, read_only_cache_root=_cache_root(first))
    assert not missing.exists()
    assert len(provider.calls) == 2
    stored = [json.loads(path.read_text(encoding="utf-8")) for path in _manifest_files(config)]
    assert sorted(value["status"] for value in stored) == ["failed", "success"]


@pytest.mark.parametrize("process_no_proxy_all", [False, True])
def test_process_no_proxy_all_flag_is_boolean_only_and_environment_unchanged(inputs, monkeypatch, process_no_proxy_all):
    import os
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setenv("NO_PROXY", "*" if process_no_proxy_all else "localhost")
    monkeypatch.setenv("HTTPS_PROXY", "http://sensitive-proxy.invalid")
    before = dict(os.environ)
    result = _run(inputs)
    assert result["manifest"]["network_controls"]["process_no_proxy_all"] is process_no_proxy_all
    assert "sensitive-proxy.invalid" not in json.dumps(result["manifest"])
    assert dict(os.environ) == before
