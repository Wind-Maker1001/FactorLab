"""Synthetic public-CLI diagnostics cannot become source-qualified research.

No provider, cache, importer, dataset, evaluator, manifest, or report logic is
mocked. Only networking is prohibited. These fixtures are deliberately two
synthetic stocks, not an authentic CSI500 constituent history.
"""

import hashlib
import json
import os
from pathlib import Path

import baostock
import numpy as np
import pandas as pd
import pytest
import requests
import yaml

from factorlab import cli
from factorlab.config import load_config
from factorlab.data import ParquetBarCache
from factorlab.errors import FactorLabError


SYMBOLS = ("600000.SH", "000001.SZ")


def _bars(symbol, dates, *, phase, drift, base_price, adjusted_multiplier=1.0):
    position = np.arange(len(dates), dtype=float)
    raw_open = base_price * np.exp(
        drift * position
        + 0.018 * np.sin(position / (4.0 + phase) + phase)
        + 0.004 * np.cos(position / (2.7 + phase))
    )
    raw_close = raw_open * (1.0 + 0.003 * np.sin(position / (2.2 + phase)))
    raw_high = np.maximum(raw_open, raw_close) * 1.01
    raw_low = np.minimum(raw_open, raw_close) * 0.99
    volume = 1_000_000.0 + 11_000.0 * position + 90_000.0 * (1.0 + np.sin(position / (3.0 + phase)))
    return pd.DataFrame(
        {
            "date": dates,
            "symbol": symbol,
            "open": raw_open,
            "high": raw_high,
            "low": raw_low,
            "close": raw_close,
            "volume": volume,
            "amount": volume * raw_close,
            "turnover": 0.008 + 0.002 * (1.0 + np.cos(position / (5.0 + phase))),
            "adj_factor": adjusted_multiplier,
            "raw_open": raw_open,
            "raw_high": raw_high,
            "raw_low": raw_low,
            "raw_close": raw_close,
            "adj_open": raw_open * adjusted_multiplier,
            "adj_high": raw_high * adjusted_multiplier,
            "adj_low": raw_low * adjusted_multiplier,
            "adj_close": raw_close * adjusted_multiplier,
        }
    )


@pytest.fixture
def local_files(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("The fully local research pipeline attempted a network call")

    monkeypatch.setattr(requests.sessions.Session, "request", no_network)
    monkeypatch.setattr(baostock, "login", no_network)
    dates = pd.bdate_range("2024-01-02", periods=85)
    research_dates = dates[-20:]
    repository = Path(__file__).resolve().parents[2]
    document = yaml.safe_load(
        (repository / "configs/research/csi500_smoke.yaml").read_text(encoding="utf-8")
    )
    document["project"]["name"] = "synthetic-only-local-cli-acceptance"
    document["paths"] = {
        "data_root": str((tmp_path / "cache").resolve()),
        "results_root": str((tmp_path / "results").resolve()),
        "market_input_root": str((tmp_path / "market-input").resolve()),
    }
    document["data"].update(
        {
            "provider": "local_parquet",
            "start": research_dates.min().date().isoformat(),
            "end": research_dates.max().date().isoformat(),
            "warmup_trading_days": 65,
        }
    )
    document["universe"].update(
        {
            "provider": "local",
            "status_provider": "local",
            "expected_member_count": 2,
            "min_listed_days": 60,
            "data_quality_window_days": 60,
            "min_valid_ohlcv_ratio": 1.0,
        }
    )
    document["evaluation"].update({"quantiles": 2, "min_cross_section": 2})
    document["market_regime"]["enabled"] = False
    document["portfolio"]["top_k"] = 2
    config_path = tmp_path / "local.yaml"
    config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    stocks = pd.concat(
        [
            _bars(SYMBOLS[0], dates, phase=0.1, drift=0.0014, base_price=12.0, adjusted_multiplier=2.0),
            _bars(SYMBOLS[1], dates, phase=1.2, drift=0.0002, base_price=20.0, adjusted_multiplier=3.0),
        ],
        ignore_index=True,
    )
    stock_path = tmp_path / "synthetic-stocks.csv"
    stocks.to_csv(stock_path, index=False)
    benchmark_path = tmp_path / "synthetic-benchmark.parquet"
    _bars("000905.SH", dates, phase=0.35, drift=0.0007, base_price=5000.0).to_parquet(benchmark_path, index=False)
    membership = pd.MultiIndex.from_product(
        [research_dates, SYMBOLS], names=["date", "symbol"]
    ).to_frame(index=False)
    membership["is_member"] = 1
    membership_path = tmp_path / "synthetic-pit.csv"
    membership.to_csv(membership_path, index=False)
    status = membership[["date", "symbol"]].copy()
    status["trade_status"] = 1
    status["is_st"] = 0
    status_path = tmp_path / "synthetic-status.csv"
    status.to_csv(status_path, index=False)
    listings_path = tmp_path / "synthetic-listings.csv"
    pd.DataFrame({"symbol": SYMBOLS, "listing_date": ["1991-04-03", "1991-04-03"]}).to_csv(listings_path, index=False)
    return {
        "config_path": config_path,
        "config": document,
        "dates": dates,
        "research_dates": research_dates,
        "stocks": stocks,
        "stock_path": stock_path,
        "benchmark_path": benchmark_path,
        "membership_path": membership_path,
        "status_path": status_path,
        "listings_path": listings_path,
    }


def _commands(files):
    config = str(files["config_path"])
    return [
        ["import-market-data", "--config", config, "--stocks", str(files["stock_path"]), "--benchmark", str(files["benchmark_path"]), "--source", "synthetic-test-fixture-not-market-data", "--source-version", "synthetic-v1"],
        ["import-universe", "--config", config, "--input", str(files["membership_path"])],
        ["import-security-data", "--config", config, "--status", str(files["status_path"]), "--listings", str(files["listings_path"])],
        ["download-data", "--config", config],
        ["build-dataset", "--config", config],
        ["evaluate-factors", "--config", config],
    ]


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _run_payloads(files):
    return [
        _json(path)
        for path in Path(files["config"]["paths"]["results_root"]).joinpath("runs").glob("*/manifest.json")
    ]


def test_legacy_cli_imports_and_cache_remain_diagnostic_without_qualification(local_files):
    files = local_files
    commands = _commands(files)
    assert [cli.main(command) for command in commands] == [0, 0, 0, 0, 1, 1]
    config = load_config(files["config_path"])
    bundle = _json(config.paths.market_input_root / "manifest.json")
    assert bundle["source"] == "synthetic-test-fixture-not-market-data"
    assert bundle["source_declaration"] == "user-provided-not-independently-verified"
    assert bundle["stocks_rows"] == 170
    assert bundle["benchmark_rows"] == 85
    assert bundle["input_sha256"]["stocks"] == hashlib.sha256(files["stock_path"].read_bytes()).hexdigest()
    assert bundle["input_sha256"]["benchmark"] == hashlib.sha256(files["benchmark_path"].read_bytes()).hexdigest()
    stock_cache = ParquetBarCache(config.paths.raw_dir / "local_parquet/daily")
    assert not (config.paths.raw_dir / "akshare/daily").exists()
    for symbol in SYMBOLS:
        snapshot = stock_cache.load_snapshot(symbol)
        assert len(snapshot.frame) == 85
        assert snapshot.metadata["source"] == "LocalParquetProvider"
        assert bundle["bundle_version"] in snapshot.metadata["source_version"]
        assert snapshot.frame["date"].tolist() == files["dates"].tolist()
    assert _json(config.paths.raw_dir / "local_parquet/failed_symbols.json") == {}

    universe = _json(config.paths.universe_dir / "manifest.json")
    security = _json(config.paths.universe_dir / "security_data_manifest.json")
    assert universe["trading_dates"] == 20
    assert universe["expected_member_count"] == 2
    assert security["source"] == "local-import"
    assert security["universe_sha256"] == universe["sha256"]
    dataset_path = config.paths.processed_dir / "dataset.parquet"
    assert not dataset_path.exists()
    assert not (config.paths.processed_dir / "dataset_manifest.json").exists()
    assert len(config.factors) == 14
    for name in ("raw_factors.parquet", "processed_factors.parquet"):
        assert not (config.paths.features_dir / name).exists()
    for filename in ("summary.md", "factor_ic.png", "rank_ic.png", "factor_correlation.png", "quantile_returns.png"):
        assert not (config.paths.factor_results_dir / filename).exists()
    runs = _run_payloads(files)
    assert len(runs) == 6
    assert {run["command"] for run in runs} == {command[0] for command in commands}
    assert sum(run["status"] == "success" for run in runs) == 4
    assert sum(run["status"] == "failed" for run in runs) == 2
    assert all(run["random_seed"] == 42 for run in runs)
    assert all(len(run["git_commit"]) == 40 for run in runs)
    assert all(run["completed_at"] for run in runs)
    importer = next(run for run in runs if run["command"] == "import-market-data")
    assert importer["market_data_bundle_version"] == bundle["bundle_version"]
    assert importer["market_data_bundle_manifest"] == bundle
    build = next(run for run in runs if run["command"] == "build-dataset")
    evaluate = next(run for run in runs if run["command"] == "evaluate-factors")
    assert "v2 qualification" in build["error"]
    assert "v2 qualification" in evaluate["error"]
    assert build["outputs"] == evaluate["outputs"] == {}

    # A corrected source snapshot may not silently reuse an old, covered
    # cache. Diagnostic generations survive without producing a dataset.
    original_benchmark_metadata = ParquetBarCache(config.paths.benchmark_dir).read_metadata("000905.SH")
    changed_stocks = files["stocks"].copy()
    row = changed_stocks["symbol"].eq(SYMBOLS[0]) & changed_stocks["date"].eq(files["dates"][-1])
    for field in ("open", "high", "low", "close"):
        for prefix in ("", "raw_", "adj_"):
            changed_stocks.loc[row, prefix + field] *= 1.02
    changed_stocks.to_csv(files["stock_path"], index=False)
    corrected_command = commands[0].copy()
    corrected_command[-1] = "synthetic-v2"
    assert cli.main(corrected_command) == 0
    corrected_bundle = _json(config.paths.market_input_root / "manifest.json")
    assert corrected_bundle["bundle_version"] != bundle["bundle_version"]
    for role in ("stocks", "benchmark"):
        assert (config.paths.market_input_root / bundle[role + "_file"]).is_file()
    assert cli.main(["build-dataset", "--config", str(files["config_path"])]) == 1
    assert not dataset_path.exists()
    assert ParquetBarCache(config.paths.benchmark_dir).read_metadata("000905.SH") == original_benchmark_metadata
    failures = [run for run in _run_payloads(files) if run["status"] == "failed"]
    assert len(failures) == 3
    cache_failure = next(run for run in failures if "cache source identity mismatch" in run["error"])
    assert bundle["bundle_version"] in cache_failure["error"]
    assert corrected_bundle["bundle_version"] in cache_failure["error"]


def test_import_market_data_refuses_nonlocal_provider_without_network(local_files):
    files = local_files
    files["config"]["data"]["provider"] = "akshare"
    files["config_path"].write_text(yaml.safe_dump(files["config"], sort_keys=False), encoding="utf-8")

    assert cli.main(_commands(files)[0]) == 1

    assert not Path(files["config"]["paths"]["market_input_root"]).exists()
    runs = _run_payloads(files)
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert "import-market-data requires local_parquet" in runs[0]["error"]


def test_relative_import_paths_use_repository_root_from_another_cwd(
    local_files, tmp_path, monkeypatch
):
    files = local_files
    config = load_config(files["config_path"])
    relative_files = files.copy()
    for name in (
        "config_path",
        "stock_path",
        "benchmark_path",
        "membership_path",
        "status_path",
        "listings_path",
    ):
        # Exercise parent components even when the configured temp directory
        # happens to be inside the checkout on this machine.
        relative_files[name] = os.path.join("configs", "..", os.path.relpath(files[name], config.repo_root))
        assert not Path(relative_files[name]).is_absolute()
        assert ".." in Path(relative_files[name]).parts
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    commands = _commands(relative_files)
    assert [cli.main(command) for command in commands] == [0, 0, 0, 0, 1, 1]

    bundle = _json(config.paths.market_input_root / "manifest.json")
    assert bundle["input_files"] == {
        "stocks": str(files["stock_path"].resolve()),
        "benchmark": str(files["benchmark_path"].resolve()),
    }
    universe = _json(config.paths.universe_dir / "manifest.json")
    assert universe["source_file"] == str(files["membership_path"].resolve())
    assert universe["source_file_sha256"] == hashlib.sha256(
        files["membership_path"].read_bytes()
    ).hexdigest()
    security = _json(config.paths.universe_dir / "security_data_manifest.json")
    assert security["input_files"] == {
        "status": str(files["status_path"].resolve()),
        "listings": str(files["listings_path"].resolve()),
    }
    assert not (config.paths.processed_dir / "dataset.parquet").exists()
    runs = _run_payloads(files)
    assert len(runs) == 6
    assert sum(run["status"] == "success" for run in runs) == 4
    assert sum(run["status"] == "failed" for run in runs) == 2
    assert {run["command"] for run in runs} == {command[0] for command in commands}
    assert all(
        Path(output).is_absolute()
        for run in runs
        for output in run["outputs"].values()
    )
    build = next(run for run in runs if run["command"] == "build-dataset")
    assert "v2 qualification" in build["error"]
    assert not (unrelated_cwd / "data").exists()
    assert not (unrelated_cwd / "results").exists()


@pytest.mark.parametrize(
    "exception",
    [
        PermissionError("fixture access denied"),
        OSError("fixture filesystem failure"),
        RuntimeError("fixture symlink loop"),
        ValueError("fixture invalid path"),
    ],
    ids=["permission", "filesystem", "symlink-loop", "invalid-path"],
)
def test_input_path_resolution_failure_becomes_domain_error(
    local_files, monkeypatch, exception
):
    config = load_config(local_files["config_path"])

    def fail_resolve(unused_path, *args, **kwargs):
        raise exception

    monkeypatch.setattr(Path, "resolve", fail_resolve)
    with pytest.raises(FactorLabError, match="Cannot resolve imported input path") as caught:
        cli._resolve_input_path(config, "fixture.csv")

    assert caught.value.__cause__ is exception
