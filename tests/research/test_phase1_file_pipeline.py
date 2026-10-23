"""Offline file-level acceptance test for the complete Phase-1 CLI pipeline.

This deliberately exercises the real importers, cache metadata contract,
configuration loader, dataset builder, factor evaluator, report writer, and
run manifests. The provider factory and explicit financial boundary test
double keep this synthetic software acceptance offline and unqualified.
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

pytestmark = pytest.mark.usefixtures("synthetic_cli_admission")

import qbot.research.cli as research_cli
from qbot.research.data import ParquetBarCache


class OfflineFileProvider:
    """Provider identity used only to bind prebuilt offline cache artifacts."""

    def cache_source_version(self, symbol, is_index):
        kind = "benchmark" if is_index else "equity"
        return "offline-phase1-file-pipeline-v1;%s" % kind


def _bars(symbol, dates, *, phase, drift, base_price):
    position = np.arange(len(dates), dtype=float)
    open_price = base_price * np.exp(
        drift * position
        + 0.018 * np.sin(position / (4.0 + phase) + phase)
        + 0.004 * np.cos(position / (2.7 + phase))
    )
    close_price = open_price * (
        1.0 + 0.003 * np.sin(position / (2.2 + phase) + 0.5 * phase)
    )
    high_price = np.maximum(open_price, close_price) * 1.01
    low_price = np.minimum(open_price, close_price) * 0.99
    volume = (
        1_000_000.0
        + 11_000.0 * position
        + 90_000.0 * (1.0 + np.sin(position / (3.0 + phase)))
    )
    amount = volume * close_price
    turnover = 0.008 + 0.002 * (1.0 + np.cos(position / (5.0 + phase)))
    return pd.DataFrame(
        {
            "date": dates,
            "symbol": symbol,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
            "amount": amount,
            "turnover": turnover,
            "adj_factor": 1.0,
            "raw_open": open_price,
            "raw_high": high_price,
            "raw_low": low_price,
            "raw_close": close_price,
            "adj_open": open_price,
            "adj_high": high_price,
            "adj_low": low_price,
            "adj_close": close_price,
        }
    )


def _write_config(tmp_path, research_dates, warmup_days):
    repository = Path(__file__).resolve().parents[2]
    template = yaml.safe_load(
        (repository / "configs" / "research" / "csi500_smoke.yaml").read_text(
            encoding="utf-8"
        )
    )
    template["project"]["name"] = "qbot-factorlab-offline-file-pipeline-test"
    template["paths"] = {
        "data_root": str((tmp_path / "data").resolve()),
        "results_root": str((tmp_path / "results").resolve()),
    }
    template["data"].update(
        {
            "start": research_dates.min().date().isoformat(),
            "end": research_dates.max().date().isoformat(),
            "warmup_trading_days": warmup_days,
        }
    )
    template["universe"].update(
        {
            "provider": "local",
            "status_provider": "local",
            "expected_member_count": 2,
            "min_listed_days": 5,
            "zero_volume_window_days": 5,
            "data_quality_window_days": 20,
            "min_valid_ohlcv_ratio": 1.0,
        }
    )
    template["evaluation"].update({"quantiles": 2, "min_cross_section": 2})
    template["market_regime"]["enabled"] = False
    template["portfolio"].update({"top_k": 2, "rebalance_days": 5})
    config_path = tmp_path / "phase1_file_pipeline.yaml"
    config_path.write_text(
        yaml.safe_dump(template, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return config_path, template


def _seed_cache(cache, provider, symbol, bars, *, is_index):
    source_version = provider.cache_source_version(symbol, is_index)
    cache.update(
        symbol,
        bars,
        covered_start=bars["date"].min(),
        covered_end=bars["date"].max(),
        source=provider.__class__.__name__,
        source_version=source_version,
    )
    metadata = cache.read_metadata(symbol)
    assert metadata["metadata_version"] == 3
    assert metadata["source"] == "OfflineFileProvider"
    assert metadata["source_version"] == source_version


def test_phase1_cli_file_pipeline_is_fully_offline(tmp_path, monkeypatch):
    warmup_days = 65
    all_dates = pd.bdate_range("2024-01-02", periods=85)
    research_dates = all_dates[-20:]
    symbols = ("600000.SH", "000001.SZ")
    config_path, config = _write_config(tmp_path, research_dates, warmup_days)
    data_root = Path(config["paths"]["data_root"])
    results_root = Path(config["paths"]["results_root"])

    provider = OfflineFileProvider()
    monkeypatch.setattr(research_cli, "_provider", lambda unused_config: provider)

    benchmark_cache = ParquetBarCache(data_root / "benchmark")
    _seed_cache(
        benchmark_cache,
        provider,
        "000905.SH",
        _bars(
            "000905.SH",
            all_dates,
            phase=0.35,
            drift=0.0007,
            base_price=5_000.0,
        ),
        is_index=True,
    )
    stock_cache = ParquetBarCache(data_root / "raw" / "akshare" / "daily")
    _seed_cache(
        stock_cache,
        provider,
        symbols[0],
        _bars(
            symbols[0],
            all_dates,
            phase=0.1,
            drift=0.0014,
            base_price=12.0,
        ),
        is_index=False,
    )
    _seed_cache(
        stock_cache,
        provider,
        symbols[1],
        _bars(
            symbols[1],
            all_dates,
            phase=1.2,
            drift=0.0002,
            base_price=20.0,
        ),
        is_index=False,
    )

    membership = pd.DataFrame(
        [
            {
                "date": trading_date.date().isoformat(),
                "symbol": symbol,
                "is_member": 1,
            }
            for trading_date in research_dates
            for symbol in symbols
        ]
    )
    membership_path = tmp_path / "pit_membership.csv"
    membership.to_csv(membership_path, index=False)

    status = membership.loc[:, ["date", "symbol"]].copy()
    status["trade_status"] = 1
    status["is_st"] = 0
    status_path = tmp_path / "security_status.csv"
    status.to_csv(status_path, index=False)
    listings_path = tmp_path / "listing_dates.csv"
    pd.DataFrame(
        {
            "symbol": symbols,
            "listing_date": ("1991-04-03", "1991-04-03"),
        }
    ).to_csv(listings_path, index=False)

    commands = [
        [
            "import-universe",
            "--config",
            str(config_path),
            "--input",
            str(membership_path),
        ],
        [
            "import-security-data",
            "--config",
            str(config_path),
            "--status",
            str(status_path),
            "--listings",
            str(listings_path),
        ],
        ["build-dataset", "--config", str(config_path)],
        ["evaluate-factors", "--config", str(config_path)],
    ]
    assert [research_cli.main(command) for command in commands] == [0, 0, 0, 0]

    universe_manifest = json.loads(
        (data_root / "universe" / "manifest.json").read_text(encoding="utf-8")
    )
    assert universe_manifest["source"] == "local-import"
    assert universe_manifest["input_layout"] == "daily"
    assert universe_manifest["trading_dates"] == len(research_dates)
    assert universe_manifest["expected_member_count"] == 2

    security_manifest = json.loads(
        (data_root / "universe" / "security_data_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert security_manifest["source"] == "local-import"
    assert security_manifest["status_rows"] == len(research_dates) * len(symbols)
    assert set(security_manifest["input_sha256"]) == {"status", "listings"}
    assert security_manifest["universe_sha256"] == universe_manifest["sha256"]
    for name in ("status_file", "listings_file"):
        assert (data_root / "universe" / security_manifest[name]).is_file()

    dataset_path = data_root / "processed" / "dataset.parquet"
    dataset = pd.read_parquet(dataset_path)
    assert len(dataset) == len(research_dates) * len(symbols)
    assert dataset[["date", "symbol"]].duplicated().sum() == 0
    assert set(dataset["date"]) == set(research_dates)
    assert dataset["EXCESS_RET5"].notna().sum() == (
        len(research_dates) - 6
    ) * len(symbols)
    assert dataset.loc[
        dataset["date"].isin(research_dates[-6:]), "EXCESS_RET5"
    ].isna().all()
    assert dataset[list(config["factors"])].notna().all().all()
    assert (data_root / "features" / "raw_factors.parquet").is_file()
    assert (data_root / "features" / "processed_factors.parquet").is_file()
    dataset_manifest = json.loads(
        (data_root / "processed" / "dataset_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert dataset_manifest["schema_version"] == 4
    assert len(dataset_manifest["bar_data_version"]) == 64
    assert dataset_manifest["universe_version"] == universe_manifest["sha256"]
    assert dataset_manifest["dataset_sha256"] == hashlib.sha256(
        dataset_path.read_bytes()
    ).hexdigest()
    assert (
        dataset_manifest["security_data_version"]
        == security_manifest["security_data_version"]
    )
    assert dataset_manifest["factor_list"] == config["factors"]

    daily_ic = pd.read_csv(results_root / "factors" / "daily_ic.csv")
    report = pd.read_csv(results_root / "factors" / "factor_report.csv")
    assert daily_ic["RankIC"].notna().any()
    overall = report.loc[report["scope"] == "ALL"]
    assert set(overall["factor"]) == set(config["factors"])
    assert overall["RankIC Valid Dates"].gt(0).all()
    assert (results_root / "factors" / "summary.md").is_file()
    assert (results_root / "factors" / "rank_ic.png").is_file()

    run_manifests = sorted((results_root / "runs").glob("*/manifest.json"))
    assert len(run_manifests) == 4
    run_payloads = [json.loads(path.read_text(encoding="utf-8")) for path in run_manifests]
    assert {payload["command"] for payload in run_payloads} == {
        "import-universe",
        "import-security-data",
        "build-dataset",
        "evaluate-factors",
    }
    assert all(payload["status"] == "success" for payload in run_payloads)
    build_run = next(
        payload for payload in run_payloads if payload["command"] == "build-dataset"
    )
    evaluate_run = next(
        payload for payload in run_payloads if payload["command"] == "evaluate-factors"
    )
    assert evaluate_run["valid_rank_ic_observations"] > 0
    assert evaluate_run["dataset_producer_run_id"] == build_run["run_id"]
    for payload in (build_run, evaluate_run):
        assert payload["bar_data_version"] == dataset_manifest["bar_data_version"]
        assert payload["bar_data_provenance"] == dataset_manifest["bar_data_provenance"]
        assert payload["dataset_sha256"] == dataset_manifest["dataset_sha256"]
        assert payload["universe_version"] == universe_manifest["sha256"]
        assert payload["security_data_version"] == security_manifest[
            "security_data_version"
        ]

    # A newly imported universe must not reuse a status generation bound to
    # different membership, nor overwrite the already verified dataset.
    old_dataset = dataset_path.read_bytes()
    changed_membership = membership.copy()
    changed_membership.loc[
        changed_membership["symbol"].eq(symbols[0]), "symbol"
    ] = "600001.SH"
    changed_path = tmp_path / "different_membership.csv"
    changed_membership.to_csv(changed_path, index=False)
    assert research_cli.main(
        [
            "import-universe",
            "--config",
            str(config_path),
            "--input",
            str(changed_path),
        ]
    ) == 0
    assert research_cli.main(["build-dataset", "--config", str(config_path)]) == 1
    assert dataset_path.read_bytes() == old_dataset
    final_runs = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (results_root / "runs").glob("*/manifest.json")
    ]
    failed_runs = [payload for payload in final_runs if payload["status"] == "failed"]
    assert len(failed_runs) == 1
    assert "SECURITY STATUS DATA REQUIRED" in failed_runs[0]["error"]
    assert "universe_sha256" in failed_runs[0]["error"]
