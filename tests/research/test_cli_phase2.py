"""Offline end-to-end tests for the Phase-2 CLI orchestration functions."""

from datetime import date
from functools import lru_cache
import json
from pathlib import Path
import pickle

import numpy as np
import pandas as pd
import pytest

# These tests verify orchestration, not the truth of synthetic market evidence.
pytestmark = pytest.mark.usefixtures("synthetic_cli_admission")

from factorlab import cli
from factorlab.config import (
    DataConfig,
    EvaluationConfig,
    FactorScoreConfig,
    LightGBMConfig,
    LightGBMParameters,
    ModelsConfig,
    PathsConfig,
    PortfolioConfig,
    ProjectConfig,
    ResearchConfig,
    RidgeConfig,
    UniverseConfig,
    ValidationConfig,
    XGBoostConfig,
    XGBoostParameters,
)
from factorlab.data.schema import canonicalize_bars
from factorlab.errors import FactorLabError


def _provenance():
    return {
        "git_commit": "a" * 40,
        "git_dirty": False,
        "git_diff_sha256": None,
        "python_version": "3.9.25",
        "package_versions": {"numpy": "fixture"},
    }


def _legacy_universe_manifest(manifest):
    """Build an actual pre-generation fixture, not a hybrid schema."""
    result = dict(manifest)
    for field in (
        "generation_id", "daily_file", "intervals_file", "daily_alias",
        "intervals_alias", "daily_file_sha256", "intervals_file_sha256",
    ):
        result.pop(field, None)
    return result


class _Run:
    def __init__(self, run_id):
        self.run_id = run_id
        self.payload = _provenance()
        self.outputs = {}

    def add_output(self, name, value):
        self.outputs[name] = str(value)


def _build_inputs():
    dates = pd.bdate_range("2020-01-02", "2021-06-30")
    warmup_dates = pd.bdate_range(end=dates[0] - pd.offsets.BDay(), periods=120)
    symbols = ["%06d.SZ" % number for number in range(1, 11)]
    rows = []
    bar_rows = []
    for date_number, trading_date in enumerate(dates):
        wave = np.sin(date_number / 13.0)
        for symbol_number, symbol in enumerate(symbols):
            factor_one = (symbol_number - 4.5) / 4.5 + wave * 0.04
            factor_two = np.cos(date_number / 19.0 + symbol_number / 3.0)
            target = 0.02 * factor_one - 0.008 * factor_two
            entry = dates[date_number + 1] if date_number + 1 < len(dates) else pd.NaT
            exit_ = dates[date_number + 6] if date_number + 6 < len(dates) else pd.NaT
            rows.append(
                {
                    "date": trading_date,
                    "symbol": symbol,
                    "REV1": factor_one,
                    "MOM20": factor_two,
                    "entry_date": entry,
                    "exit_date": exit_,
                    "EXCESS_RET5": target if pd.notna(exit_) else np.nan,
                    "MARKET_REGIME": "BULL" if wave >= 0 else "BEAR",
                    "INDEX_RET5": wave * 0.01,
                    "INDEX_RET20": wave * 0.02,
                    "INDEX_RET60": wave * 0.03,
                    "INDEX_VOL20": 0.01 + abs(wave) * 0.005,
                }
            )
            price = 10.0 + symbol_number + date_number * (0.003 + symbol_number * 0.0001)
            bar_rows.append(
                {
                    "date": trading_date,
                    "symbol": symbol,
                    "adj_open": price,
                    "raw_open": price,
                    "raw_high": price + 0.02,
                    "raw_low": price - 0.02,
                    "raw_close": price,
                    "volume": 100_000.0,
                    "amount": price * 100_000.0,
                    "turnover": 0.02,
                    "trade_status": 1,
                    "is_st": False,
                }
            )
    # Price caches include the actual warm-up sessions required by the config.
    # Research observations and their original trading prices stay unchanged.
    first_day_rows = bar_rows[:len(symbols)]
    for trading_date in warmup_dates:
        bar_rows.extend(dict(row, date=trading_date) for row in first_day_rows)
    benchmark_dates = warmup_dates.append(dates)
    benchmark_price = 5000.0 + np.arange(-len(warmup_dates), len(dates), dtype=float) * 0.8
    benchmark = canonicalize_bars(
        pd.DataFrame(
            {
                "date": benchmark_dates,
                "symbol": "000905.SH",
                "open": benchmark_price,
                "high": benchmark_price + 2.0,
                "low": benchmark_price - 2.0,
                "close": benchmark_price + 0.5,
                "volume": 1_000_000.0,
                "amount": 5_000_000_000.0,
                "turnover": np.nan,
            }
        )
    )
    return dates, pd.DataFrame(rows), canonicalize_bars(pd.DataFrame(bar_rows)), benchmark


@lru_cache(maxsize=1)
def _input_template(fixture_version, numpy_version, pandas_version):
    """Process-local immutable bytes bound to the fixture recipe and library pins."""
    version = (fixture_version, numpy_version, pandas_version)
    return pickle.dumps((version, _build_inputs()), protocol=5)


def _inputs():
    version = (1, np.__version__, pd.__version__)
    stored_version, values = pickle.loads(_input_template(*version))
    assert stored_version == version
    return values


def _config(tmp_path):
    config = ResearchConfig(
        project=ProjectConfig(name="factorlab-phase2-cli-test"),
        data=DataConfig(
            start=date(2020, 1, 2),
            end=date(2021, 6, 30),
            warmup_trading_days=120,
        ),
        universe=UniverseConfig(
            provider="local",
            expected_member_count=10,
            min_listed_days=120,
            data_quality_window_days=60,
        ),
        factors=("REV1", "MOM20"),
        paths=PathsConfig(
            data_root=tmp_path / "data" / "research",
            results_root=tmp_path / "results",
        ),
        evaluation=EvaluationConfig(quantiles=5, min_cross_section=5),
        models=ModelsConfig(
            factor_score=FactorScoreConfig(
                weights={"REV1": 1.0, "MOM20": -0.5},
                normalize_by_absolute_weight=True,
            ),
            ridge=RidgeConfig(alphas=(0.1, 1.0)),
            lightgbm=LightGBMConfig(
                candidates=(
                    LightGBMParameters(
                        num_leaves=7,
                        max_depth=3,
                        n_estimators=8,
                        min_child_samples=2,
                    ),
                    LightGBMParameters(
                        num_leaves=7,
                        max_depth=3,
                        n_estimators=12,
                        min_child_samples=2,
                    ),
                )
            ),
            xgboost=XGBoostConfig(
                candidates=(
                    XGBoostParameters(max_depth=2, n_estimators=8),
                    XGBoostParameters(max_depth=3, n_estimators=12),
                )
            ),
        ),
        validation=ValidationConfig(
            initial_train_months=4,
            validation_months=2,
            test_months=2,
            step_months=2,
            purge_days=5,
            embargo_days=5,
        ),
        portfolio=PortfolioConfig(top_k=3, rebalance_days=5),
    )
    object.__setattr__(config, "repo_root", Path(tmp_path))
    object.__setattr__(config, "config_path", Path(tmp_path) / "fixture.yaml")
    config.validate()
    return config


def _write_verified_inputs(
    config,
    dataset,
    *,
    dataset_sha256=None,
    write_dataset_manifest=True,
    write_universe_manifest=True,
    bars=None,
    benchmark=None,
    missing_stock_symbols=(),
):
    config.paths.processed_dir.mkdir(parents=True)
    dataset_path = config.paths.processed_dir / "dataset.parquet"
    dataset.to_parquet(dataset_path, index=False)
    effective_dataset_sha256 = (
        dataset_sha256
        if dataset_sha256 is not None
        else cli._file_sha256(dataset_path)
    )
    membership = dataset[["date", "symbol"]].copy()
    membership["is_member"] = True
    membership = cli.validate_daily_membership(
        membership,
        pd.DatetimeIndex(dataset["date"].drop_duplicates()),
        config.universe.expected_member_count,
    )
    config.paths.universe_dir.mkdir(parents=True)
    membership.to_parquet(
        config.paths.universe_dir / "csi500_daily.parquet", index=False
    )
    universe_version = cli.dataframe_sha256(membership)
    intervals = cli.compress_membership_intervals(membership)
    intervals.to_parquet(
        config.paths.universe_dir / "csi500_intervals.parquet", index=False
    )
    if write_universe_manifest:
        # Synthetic membership exercises the real local importer. Do not
        # manufacture a BaoStock identity whose effective dates are unverified.
        source_path = config.repo_root / "synthetic-pit.csv"
        membership.to_csv(source_path, index=False, date_format="%Y-%m-%d")
        cli.import_point_in_time_universe(
            source_path, config.paths.universe_dir,
            pd.DatetimeIndex(dataset["date"].drop_duplicates()),
            config.universe.expected_member_count, config.universe.index_symbol,
        )
    symbols = membership["symbol"].drop_duplicates().sort_values().tolist()
    if bars is None or benchmark is None:
        _, _, fixture_bars, fixture_benchmark = _inputs()
        if bars is None:
            bars = fixture_bars
        if benchmark is None:
            benchmark = fixture_benchmark
    calendar = pd.DatetimeIndex(benchmark["date"].drop_duplicates().sort_values())
    status = pd.MultiIndex.from_product(
        [calendar, symbols], names=["date", "symbol"]
    ).to_frame(index=False)
    status["trade_status"] = 1
    status["is_st"] = False
    listings = pd.DataFrame(
        {"symbol": symbols, "listing_date": calendar.min()}
    )
    security_manifest = cli.publish_security_data(
        status,
        listings,
        output_dir=config.paths.universe_dir,
        calendar=calendar,
        membership=membership,
        universe_sha256=universe_version,
        source="BaoStockPointInTimeProvider",
        source_version="baostock-security-history-v1",
    )
    security_manifest_sha256 = cli._security_manifest_sha256(security_manifest)
    security_fields = {
        "security_data_version": security_manifest["security_data_version"],
        "security_data_source": security_manifest["source"],
        "security_data_source_version": security_manifest["source_version"],
        "security_data_manifest_sha256": security_manifest_sha256,
        "security_status_sha256": security_manifest["status_sha256"],
        "listing_dates_sha256": security_manifest["listings_sha256"],
        "security_data_input_sha256": security_manifest["input_sha256"],
    }
    provider = cli._provider(config)
    benchmark_cache = cli._benchmark_cache(config)
    benchmark_cache.update(
        config.data.benchmark,
        benchmark,
        benchmark["date"].min(),
        benchmark["date"].max(),
        source=provider.__class__.__name__,
        source_version=provider.cache_source_version(config.data.benchmark, True),
    )
    stock_cache = cli._stock_cache(config)
    for symbol in symbols:
        if symbol in missing_stock_symbols:
            continue
        stock_cache.update(
            symbol,
            bars.loc[bars["symbol"].eq(symbol)],
            bars["date"].min(),
            bars["date"].max(),
            source=provider.__class__.__name__,
            source_version=provider.cache_source_version(symbol, False),
        )
    _, _, bar_data_provenance, bar_data_version = cli._load_bar_inputs(
        config, membership
    )
    bar_fields = {
        "bar_data_provenance": bar_data_provenance,
        "bar_data_version": bar_data_version,
    }
    if write_dataset_manifest:
        cli._atomic_json(
            {
                "schema_version": 4,
                "dataset_sha256": effective_dataset_sha256,
                "universe_version": universe_version,
                **security_fields,
                **bar_fields,
                "factor_list": list(config.factors),
                "target": "EXCESS_RET5",
                "rows": len(dataset),
                "dates": dataset["date"].nunique(),
                "data_start": config.data.start.isoformat(),
                "data_end": config.data.end.isoformat(),
                "dataset_contract": cli._dataset_contract(config),
                "producer_run_id": "20200101T000000-build-dataset",
                "producer_provenance": _provenance(),
            },
            config.paths.processed_dir / "dataset_manifest.json",
        )
        cli._atomic_json(
            {
                "run_id": "20200101T000000-build-dataset",
                "command": "build-dataset",
                "status": "success",
                "config": config.to_dict(),
                "dataset_sha256": effective_dataset_sha256,
                "universe_version": universe_version,
                **security_fields,
                **bar_fields,
                **_provenance(),
            },
            config.paths.runs_dir
            / "20200101T000000-build-dataset"
            / "manifest.json",
        )
    return dataset_path, membership, universe_version


def test_evaluate_records_verified_dataset_provenance(tmp_path, monkeypatch):
    _, dataset, _, benchmark = _inputs()
    config = _config(tmp_path)
    dataset_path, _, universe_version = _write_verified_inputs(config, dataset)
    monkeypatch.setattr(cli, "_load_benchmark_with_warmup", lambda current: benchmark)

    run = _Run("20200101T000000-evaluate")
    cli.command_evaluate(config, run)

    assert Path(run.outputs["factor_report"]).is_file()
    assert run.payload["dataset_sha256"] == cli._file_sha256(dataset_path)
    assert run.payload["universe_version"] == universe_version
    assert run.payload["valid_rank_ic_observations"] > 0
    dataset_manifest = json.loads(
        (config.paths.processed_dir / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    assert run.payload["bar_data_version"] == dataset_manifest["bar_data_version"]
    assert run.payload["bar_data_provenance"] == dataset_manifest["bar_data_provenance"]


def test_execution_status_join_allows_nonmember_price_rows_and_pins_generation(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    calendar = pd.bdate_range("2024-01-02", periods=4)
    membership = pd.DataFrame(
        {
            "date": calendar[2:],
            "symbol": ["000001.SZ", "000001.SZ"],
            "is_member": [True, True],
        }
    )
    universe_version = cli.dataframe_sha256(
        cli.validate_daily_membership(membership, expected_member_count=None)
    )
    status = membership[["date", "symbol"]].copy()
    status["trade_status"] = 1
    status["is_st"] = False
    listings = pd.DataFrame(
        {"symbol": ["000001.SZ"], "listing_date": [calendar[0]]}
    )
    manifest = cli.publish_security_data(
        status,
        listings,
        output_dir=config.paths.universe_dir,
        calendar=calendar,
        membership=membership,
        universe_sha256=universe_version,
        source="BaoStockPointInTimeProvider",
        source_version="baostock-security-history-v1",
    )
    bars = pd.DataFrame(
        {
            "date": calendar,
            "symbol": "000001.SZ",
            "raw_open": 10.0,
            "raw_close": 10.0,
            "volume": 100_000.0,
        }
    )
    monkeypatch.setattr(
        cli, "_load_verified_membership", lambda current, dates: membership
    )

    joined = cli._attach_execution_status(
        config,
        bars,
        calendar,
        calendar[2:],
        expected_security_data_version=manifest["security_data_version"],
        expected_security_manifest_sha256=cli._security_manifest_sha256(
            manifest
        ),
    )

    assert joined.loc[joined["date"].isin(calendar[:2]), "trade_status"].isna().all()
    assert joined.loc[joined["date"].isin(calendar[2:]), "trade_status"].eq(1).all()
    with pytest.raises(FactorLabError, match="changed after dataset verification"):
        cli._attach_execution_status(
            config,
            bars,
            calendar,
            calendar[2:],
            expected_security_data_version=manifest["security_data_version"],
            expected_security_manifest_sha256="f" * 64,
        )
def test_build_dataset_binds_producer_code_provenance(tmp_path, monkeypatch):
    _, dataset, bars, benchmark = _inputs()
    config = _config(tmp_path)
    _, membership, universe_version = _write_verified_inputs(config, dataset)
    security_manifest = json.loads(
        (config.paths.universe_dir / "security_data_manifest.json").read_text(
            encoding="utf-8"
        )
    )

    class FakeBuilder:
        def __init__(self, options):
            self.options = options

        def build(self, *args):
            return dataset.copy()

        def write_artifacts(self, result, destination, features_dir):
            destination.parent.mkdir(parents=True, exist_ok=True)
            result.to_parquet(destination, index=False)

    monkeypatch.setattr(cli, "_load_benchmark_with_warmup", lambda current: benchmark)
    monkeypatch.setattr(
        cli, "_load_verified_membership", lambda current, calendar: membership
    )
    monkeypatch.setattr(cli, "_load_stock_bars", lambda current, symbols: bars)
    monkeypatch.setattr(cli, "DatasetBuilder", FakeBuilder)

    run = _Run("20200101T000000-build-dataset")
    cli.command_build_dataset(config, run)

    manifest = json.loads(
        (config.paths.processed_dir / "dataset_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["schema_version"] == 4
    assert manifest["producer_run_id"] == run.run_id
    assert manifest["producer_provenance"] == _provenance()
    assert manifest["universe_version"] == universe_version
    assert manifest["security_data_version"] == security_manifest[
        "security_data_version"
    ]
    assert manifest["security_status_sha256"] == security_manifest[
        "status_sha256"
    ]
    assert manifest["listing_dates_sha256"] == security_manifest[
        "listings_sha256"
    ]
    assert manifest["dataset_sha256"] == run.payload["dataset_sha256"]
    assert manifest["bar_data_version"] == run.payload["bar_data_version"]
    assert manifest["bar_data_provenance"] == run.payload["bar_data_provenance"]


def test_build_dataset_validates_security_bundle_before_loading_stock_bars(
    tmp_path, monkeypatch
):
    _, dataset, _, benchmark = _inputs()
    config = _config(tmp_path)
    _write_verified_inputs(config, dataset)
    (config.paths.universe_dir / "security_data_manifest.json").unlink()
    monkeypatch.setattr(
        cli, "_load_benchmark_with_warmup", lambda current: benchmark
    )
    monkeypatch.setattr(
        cli,
        "_load_bar_inputs",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("bar inputs must not load before security validation")
        ),
    )

    with pytest.raises(FactorLabError, match="SECURITY STATUS DATA REQUIRED"):
        cli.command_build_dataset(config, _Run("security-fail-fast"))


def test_evaluate_rejects_a_tampered_content_hash(tmp_path, monkeypatch):
    _, dataset, _, benchmark = _inputs()
    config = _config(tmp_path)
    _write_verified_inputs(config, dataset, dataset_sha256="0" * 64)
    monkeypatch.setattr(cli, "_load_benchmark_with_warmup", lambda current: benchmark)

    with pytest.raises(FactorLabError, match="Dataset manifest mismatch"):
        cli.command_evaluate(config, _Run("20200101T000000-evaluate"))


def test_dataset_reuse_rejects_a_new_security_data_generation(tmp_path):
    dates, dataset, _, _ = _inputs()
    config = _config(tmp_path)
    _, membership, universe_version = _write_verified_inputs(config, dataset)
    status = pd.read_parquet(
        config.paths.universe_dir / "security_status.parquet"
    )
    listings = pd.read_parquet(
        config.paths.universe_dir / "listing_dates.parquet"
    )
    status.loc[0, "is_st"] = True
    cli.publish_security_data(
        status,
        listings,
        output_dir=config.paths.universe_dir,
        calendar=pd.DatetimeIndex(cli._load_benchmark_with_warmup(config)["date"]),
        membership=membership,
        universe_sha256=universe_version,
        source="BaoStockPointInTimeProvider",
        source_version="baostock-security-history-v1",
    )

    with pytest.raises(FactorLabError, match="security_data_version"):
        cli._load_verified_dataset(config, dates)


@pytest.mark.parametrize("asset", ["stock", "benchmark"])
def test_dataset_reuse_rejects_corrected_raw_bar_content(tmp_path, asset):
    dates, dataset, bars, benchmark = _inputs()
    config = _config(tmp_path)
    _write_verified_inputs(config, dataset, bars=bars, benchmark=benchmark)
    provider = cli._provider(config)
    symbol = config.data.benchmark if asset == "benchmark" else bars["symbol"].iloc[0]
    cache = cli._benchmark_cache(config) if asset == "benchmark" else cli._stock_cache(config)
    changed = cache.load(symbol).iloc[[10]].copy()
    for column in (
        "open", "high", "low", "close", "raw_open", "raw_high", "raw_low",
        "raw_close", "adj_open", "adj_high", "adj_low", "adj_close",
    ):
        changed[column] *= 1.01
    cache.update(
        symbol, changed, changed["date"].iloc[0], changed["date"].iloc[0],
        source=provider.__class__.__name__,
        source_version=provider.cache_source_version(symbol, asset == "benchmark"),
    )

    with pytest.raises(FactorLabError, match="bar_data_version"):
        cli._load_verified_dataset(config, dates)


def test_dataset_reuse_rejects_a_previously_missing_stock_bar_being_filled(tmp_path):
    dates, dataset, bars, benchmark = _inputs()
    config = _config(tmp_path)
    missing_row = bars.iloc[[10]].copy()
    _write_verified_inputs(
        config, dataset, bars=bars.drop(index=missing_row.index), benchmark=benchmark
    )
    # The prior snapshot can contain missing observations without filling them.
    loaded, _, _ = cli._load_verified_dataset(config, dates)
    assert len(loaded) == len(dataset)
    provider = cli._provider(config)
    symbol = missing_row["symbol"].iloc[0]
    cli._stock_cache(config).update(
        symbol, missing_row, missing_row["date"].iloc[0], missing_row["date"].iloc[0],
        source=provider.__class__.__name__,
        source_version=provider.cache_source_version(symbol, False),
    )

    with pytest.raises(FactorLabError, match="bar_data_version"):
        cli._load_verified_dataset(config, dates)


def test_dataset_reuse_rejects_a_previously_missing_stock_cache_being_created(tmp_path):
    dates, dataset, bars, benchmark = _inputs()
    config = _config(tmp_path)
    symbol = bars["symbol"].iloc[0]
    _write_verified_inputs(
        config, dataset, bars=bars, benchmark=benchmark, missing_stock_symbols=(symbol,)
    )
    manifest_path = config.paths.processed_dir / "dataset_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["bar_data_provenance"]["stocks"][symbol]["state"] == "missing"
    loaded, _, _ = cli._load_verified_dataset(config, dates)
    assert len(loaded) == len(dataset)
    provider = cli._provider(config)
    newly_available = bars.loc[bars["symbol"].eq(symbol)]
    cli._stock_cache(config).update(
        symbol, newly_available, newly_available["date"].min(), newly_available["date"].max(),
        source=provider.__class__.__name__,
        source_version=provider.cache_source_version(symbol, False),
    )

    with pytest.raises(FactorLabError, match="bar_data_version"):
        cli._load_verified_dataset(config, dates)


def test_dataset_reuse_rejects_current_cache_source_contract_mismatch(tmp_path):
    dates, dataset, bars, _ = _inputs()
    config = _config(tmp_path)
    _write_verified_inputs(config, dataset)
    cache = cli._stock_cache(config)
    metadata_path = cache.metadata_path(bars["symbol"].iloc[0])
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["source_version"] = "different-provider-contract"
    cli._atomic_json(metadata, metadata_path)

    with pytest.raises(FactorLabError, match="cache source identity mismatch"):
        cli._load_verified_dataset(config, dates)


@pytest.mark.parametrize("field", ["bar_data_version", "bar_data_provenance"])
def test_dataset_reuse_requires_matching_build_run_bar_receipt(tmp_path, field):
    dates, dataset, _, _ = _inputs()
    config = _config(tmp_path)
    _write_verified_inputs(config, dataset)
    producer_path = config.paths.runs_dir / "20200101T000000-build-dataset" / "manifest.json"
    producer = json.loads(producer_path.read_text(encoding="utf-8"))
    producer[field] = "f" * 64 if field == "bar_data_version" else {}
    cli._atomic_json(producer, producer_path)

    with pytest.raises(FactorLabError, match="producer run manifest mismatch.*" + field):
        cli._load_verified_dataset(config, dates)


def test_dataset_reuse_conservatively_rejects_cache_extension_outside_research_span(tmp_path):
    dates, dataset, bars, _ = _inputs()
    config = _config(tmp_path)
    _write_verified_inputs(config, dataset)
    symbol = bars["symbol"].iloc[0]
    cache = cli._stock_cache(config)
    before_selected = cache.load(symbol).copy()
    appended = before_selected.iloc[[-1]].copy()
    appended["date"] += pd.offsets.BDay()
    provider = cli._provider(config)
    cache.update(
        symbol, appended, appended["date"].iloc[0], appended["date"].iloc[0],
        source=provider.__class__.__name__,
        source_version=provider.cache_source_version(symbol, False),
    )
    after_selected = cache.load(symbol)
    after_selected = after_selected.loc[after_selected["date"] <= pd.Timestamp(config.data.end)]
    pd.testing.assert_frame_equal(before_selected, after_selected)

    with pytest.raises(FactorLabError, match="bar_data_version"):
        cli._load_verified_dataset(config, dates)


def test_dataset_reuse_allows_identical_bars_republished_in_a_new_generation(tmp_path):
    dates, dataset, bars, _ = _inputs()
    config = _config(tmp_path)
    _write_verified_inputs(config, dataset)
    symbol = bars["symbol"].iloc[0]
    cache = cli._stock_cache(config)
    previous = cache.read_metadata(symbol)
    provider = cli._provider(config)
    cache.update(
        symbol, cache.load(symbol), cache.load(symbol)["date"].min(), config.data.end,
        source=provider.__class__.__name__,
        source_version=provider.cache_source_version(symbol, False),
    )
    current = cache.read_metadata(symbol)
    assert current["generation_id"] != previous["generation_id"]
    assert current["content_sha256"] == previous["content_sha256"]

    loaded, _, _ = cli._load_verified_dataset(config, dates)
    assert len(loaded) == len(dataset)


def test_evaluate_rejects_a_missing_dataset_manifest(tmp_path, monkeypatch):
    _, dataset, _, benchmark = _inputs()
    config = _config(tmp_path)
    _write_verified_inputs(config, dataset, write_dataset_manifest=False)
    monkeypatch.setattr(cli, "_load_benchmark_with_warmup", lambda current: benchmark)

    with pytest.raises(FactorLabError, match="Dataset manifest is missing"):
        cli.command_evaluate(config, _Run("20200101T000000-evaluate"))


def test_evaluate_rejects_dataset_from_different_code_provenance(
    tmp_path, monkeypatch
):
    _, dataset, _, benchmark = _inputs()
    config = _config(tmp_path)
    _write_verified_inputs(config, dataset)
    manifest_path = config.paths.processed_dir / "dataset_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["producer_provenance"]["git_commit"] = "b" * 40
    cli._atomic_json(manifest, manifest_path)
    producer_path = (
        config.paths.runs_dir
        / manifest["producer_run_id"]
        / "manifest.json"
    )
    producer = json.loads(producer_path.read_text(encoding="utf-8"))
    producer["git_commit"] = "b" * 40
    cli._atomic_json(producer, producer_path)
    monkeypatch.setattr(cli, "_load_benchmark_with_warmup", lambda current: benchmark)

    with pytest.raises(FactorLabError, match="Dataset producer provenance differs"):
        cli.command_evaluate(config, _Run("20200101T000000-evaluate"))


def test_evaluate_rejects_missing_dataset_producer_run(tmp_path, monkeypatch):
    _, dataset, _, benchmark = _inputs()
    config = _config(tmp_path)
    _write_verified_inputs(config, dataset)
    producer_path = (
        config.paths.runs_dir
        / "20200101T000000-build-dataset"
        / "manifest.json"
    )
    producer_path.unlink()
    monkeypatch.setattr(cli, "_load_benchmark_with_warmup", lambda current: benchmark)

    with pytest.raises(FactorLabError, match="producer run manifest is missing"):
        cli.command_evaluate(config, _Run("20200101T000000-evaluate"))


def test_dataset_reuse_allows_non_dataset_model_config_changes(tmp_path):
    dates, dataset, _, _ = _inputs()
    config = _config(tmp_path)
    _write_verified_inputs(config, dataset)
    producer_path = (
        config.paths.runs_dir
        / "20200101T000000-build-dataset"
        / "manifest.json"
    )
    producer = json.loads(producer_path.read_text(encoding="utf-8"))
    producer["config"]["models"]["ridge"]["alphas"] = [999.0]
    cli._atomic_json(producer, producer_path)

    loaded, _, _ = cli._load_verified_dataset(
        config, dates, _Run("20200102T000000-consumer")
    )

    assert len(loaded) == len(dataset)


def test_evaluate_rejects_dataset_without_producer_provenance(
    tmp_path, monkeypatch
):
    _, dataset, _, benchmark = _inputs()
    config = _config(tmp_path)
    _write_verified_inputs(config, dataset)
    manifest_path = config.paths.processed_dir / "dataset_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("producer_provenance")
    cli._atomic_json(manifest, manifest_path)
    monkeypatch.setattr(cli, "_load_benchmark_with_warmup", lambda current: benchmark)

    with pytest.raises(FactorLabError, match="producer provenance"):
        cli.command_evaluate(config, _Run("20200101T000000-evaluate"))


def test_evaluate_rejects_a_missing_pit_manifest(tmp_path, monkeypatch):
    _, dataset, _, benchmark = _inputs()
    config = _config(tmp_path)
    _write_verified_inputs(config, dataset, write_universe_manifest=False)
    monkeypatch.setattr(cli, "_load_benchmark_with_warmup", lambda current: benchmark)

    with pytest.raises(FactorLabError, match="POINT-IN-TIME UNIVERSE REQUIRED"):
        cli.command_evaluate(config, _Run("20200101T000000-evaluate"))


@pytest.mark.parametrize("schema_version", [1, 3])
@pytest.mark.parametrize("claimed_verified", [False, True])
def test_baostock_membership_cannot_be_approved_by_old_manifest_or_boolean(
    tmp_path, schema_version, claimed_verified
):
    dates, dataset, _, _ = _inputs()
    config = _config(tmp_path)
    _write_verified_inputs(config, dataset)
    manifest_path = config.paths.universe_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        schema_version=schema_version,
        source="BaoStockPointInTimeProvider",
        source_version="baostock-query_zz500_stocks-daily-v2",
        effective_membership_verified=claimed_verified,
        point_in_time_universe_verified=claimed_verified,
    )
    cli._atomic_json(manifest, manifest_path)
    object.__setattr__(config.universe, "provider", "baostock")
    with pytest.raises(FactorLabError, match="EFFECTIVE MEMBERSHIP SEMANTICS UNVERIFIED"):
        cli._load_verified_membership(config, dates)

    # Relabelling the config cannot turn vendor snapshots into a local import.
    object.__setattr__(config.universe, "provider", "local")
    with pytest.raises(FactorLabError, match="unsupported local universe manifest"):
        cli._load_verified_membership(config, dates)


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("source", "BaoStockPointInTimeProvider"),
        ("source_version", "baostock-query_zz500_stocks-daily-v1"),
        ("index_symbol", "000300.SH"),
        ("start", "2019-12-31"),
        ("trading_dates", 1),
        ("expected_member_count", 500),
    ],
)
def test_verified_membership_rejects_manifest_contract_mismatch(
    tmp_path, field, bad_value
):
    dates, dataset, _, _ = _inputs()
    config = _config(tmp_path)
    _write_verified_inputs(config, dataset)
    manifest_path = config.paths.universe_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = bad_value
    cli._atomic_json(manifest, manifest_path)

    with pytest.raises(FactorLabError, match="POINT-IN-TIME UNIVERSE REQUIRED"):
        cli._load_verified_membership(config, dates)


@pytest.mark.parametrize(
    "field,bad_value",
    [("source_file", ""), ("source_file_sha256", "not-a-sha256")],
)
def test_verified_local_membership_requires_import_source_provenance(
    tmp_path, field, bad_value
):
    dates, dataset, _, _ = _inputs()
    config = _config(tmp_path)
    object.__setattr__(config.universe, "provider", "local")
    _write_verified_inputs(config, dataset)
    manifest_path = config.paths.universe_dir / "manifest.json"
    manifest = _legacy_universe_manifest(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    manifest.update(
        {
            "schema_version": 1,
            "source": "local-import",
            "source_version": "user-pit-daily-import-v1",
            "source_file": str(tmp_path / "pit.csv"),
            "source_file_sha256": "c" * 64,
        }
    )
    manifest[field] = bad_value
    cli._atomic_json(manifest, manifest_path)

    with pytest.raises(FactorLabError, match="POINT-IN-TIME UNIVERSE REQUIRED"):
        cli._load_verified_membership(config, dates)


@pytest.mark.parametrize(
    "schema_version,source_version,extra",
    [
        (1, "user-pit-daily-import-v1", {}),
        (
            2,
            "user-pit-import-v2",
            {
                "input_layout": "daily",
                "membership_semantics": "exact-trading-date-snapshot",
            },
        ),
        (
            2,
            "user-pit-import-v2",
            {
                "input_layout": "inclusive-intervals",
                "membership_semantics": "entry_date <= trading_date <= exit_date",
            },
        ),
    ],
)
def test_verified_local_membership_accepts_allowlisted_manifest_versions(
    tmp_path, schema_version, source_version, extra
):
    dates, dataset, _, _ = _inputs()
    config = _config(tmp_path)
    object.__setattr__(config.universe, "provider", "local")
    _, expected, _ = _write_verified_inputs(config, dataset)
    manifest_path = config.paths.universe_dir / "manifest.json"
    manifest = _legacy_universe_manifest(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    manifest.update(
        {
            "schema_version": schema_version,
            "source": "local-import",
            "source_version": source_version,
            "source_file": str(tmp_path / "pit.csv"),
            "source_file_sha256": "c" * 64,
            **extra,
        }
    )
    if schema_version == 2:
        manifest["daily_sha256"] = manifest["sha256"]
        manifest["intervals_sha256"] = cli.dataframe_sha256(
            cli.compress_membership_intervals(expected)
        )
    cli._atomic_json(manifest, manifest_path)

    actual = cli._load_verified_membership(config, dates)
    pd.testing.assert_frame_equal(actual, expected)


@pytest.mark.parametrize(
    "updates,match",
    [
        ({"schema_version": 4}, "unsupported local universe manifest version"),
        ({"input_layout": "opt-out-intervals"}, "input_layout is invalid"),
        ({"membership_semantics": "exclusive"}, "membership_semantics is invalid"),
        ({"daily_sha256": "d" * 64}, "daily_sha256 is invalid"),
        ({"intervals_sha256": None}, "intervals_sha256 is invalid"),
    ],
)
def test_verified_local_membership_rejects_unknown_v2_contract(
    tmp_path, updates, match
):
    dates, dataset, _, _ = _inputs()
    config = _config(tmp_path)
    object.__setattr__(config.universe, "provider", "local")
    _write_verified_inputs(config, dataset)
    manifest_path = config.paths.universe_dir / "manifest.json"
    manifest = _legacy_universe_manifest(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    manifest.update(
        {
            "schema_version": 2,
            "source": "local-import",
            "source_version": "user-pit-import-v2",
            "source_file": str(tmp_path / "pit.csv"),
            "source_file_sha256": "c" * 64,
            "input_layout": "inclusive-intervals",
            "membership_semantics": "entry_date <= trading_date <= exit_date",
            "daily_sha256": manifest["sha256"],
            "intervals_sha256": cli.dataframe_sha256(
                cli.compress_membership_intervals(
                    cli.validate_daily_membership(
                        pd.read_parquet(
                            config.paths.universe_dir / "csi500_daily.parquet"
                        ),
                        dates,
                        config.universe.expected_member_count,
                    )
                )
            ),
            **updates,
        }
    )
    cli._atomic_json(manifest, manifest_path)

    with pytest.raises(FactorLabError, match=match):
        cli._load_verified_membership(config, dates)


def test_verified_local_membership_rejects_tampered_intervals(tmp_path):
    dates, dataset, _, _ = _inputs()
    config = _config(tmp_path)
    object.__setattr__(config.universe, "provider", "local")
    _, membership, _ = _write_verified_inputs(config, dataset)
    intervals = cli.compress_membership_intervals(membership)
    manifest_path = config.paths.universe_dir / "manifest.json"
    manifest = _legacy_universe_manifest(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    manifest.update(
        {
            "schema_version": 2,
            "source": "local-import",
            "source_version": "user-pit-import-v2",
            "source_file": str(tmp_path / "pit.csv"),
            "source_file_sha256": "c" * 64,
            "input_layout": "inclusive-intervals",
            "membership_semantics": "entry_date <= trading_date <= exit_date",
            "daily_sha256": manifest["sha256"],
            "intervals_sha256": cli.dataframe_sha256(intervals),
        }
    )
    cli._atomic_json(manifest, manifest_path)
    intervals.loc[0, "exit_date"] = intervals.loc[0, "entry_date"]
    intervals.to_parquet(
        config.paths.universe_dir / "csi500_intervals.parquet", index=False
    )

    with pytest.raises(FactorLabError, match="intervals content hash mismatch"):
        cli._load_verified_membership(config, dates)


def test_verified_legacy_local_membership_also_checks_intervals(tmp_path):
    dates, dataset, _, _ = _inputs()
    config = _config(tmp_path)
    object.__setattr__(config.universe, "provider", "local")
    _write_verified_inputs(config, dataset)
    manifest_path = config.paths.universe_dir / "manifest.json"
    manifest = _legacy_universe_manifest(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    manifest.update(
        {
            "schema_version": 1,
            "source": "local-import",
            "source_version": "user-pit-daily-import-v1",
            "source_file": str(tmp_path / "pit.csv"),
            "source_file_sha256": "c" * 64,
        }
    )
    cli._atomic_json(manifest, manifest_path)
    intervals_path = config.paths.universe_dir / "csi500_intervals.parquet"
    tampered = pd.read_parquet(intervals_path)
    tampered.loc[0, "exit_date"] = tampered.loc[0, "entry_date"]
    tampered.to_parquet(intervals_path, index=False)

    with pytest.raises(FactorLabError, match="intervals content hash mismatch"):
        cli._load_verified_membership(config, dates)


def test_walk_forward_pins_bar_generation_before_model_run(tmp_path, monkeypatch):
    _, dataset, bars, benchmark = _inputs()
    config = _config(tmp_path)
    _, membership, _ = _write_verified_inputs(
        config, dataset, bars=bars, benchmark=benchmark
    )
    pinned_benchmark, pinned_bars, _, pinned_version = cli._load_bar_inputs(
        config, membership
    )
    real_run = cli.WalkForwardRunner.run
    observed = {}

    def update_prices_after_model_run(runner, *args, **kwargs):
        result = real_run(runner, *args, **kwargs)
        observed["predictions"] = result.predictions
        symbol = pinned_bars["symbol"].iloc[0]
        cache = cli._stock_cache(config)
        corrected = cache.load(symbol).copy()
        for column in (
            "open", "high", "low", "close", "raw_open", "raw_high", "raw_low",
            "raw_close", "adj_open", "adj_high", "adj_low", "adj_close",
        ):
            corrected[column] *= 1.5
        provider = cli._provider(config)
        cache.update(
            symbol, corrected, corrected["date"].min(), corrected["date"].max(),
            source=provider.__class__.__name__,
            source_version=provider.cache_source_version(symbol, False),
        )
        return result

    monkeypatch.setattr(cli.WalkForwardRunner, "run", update_prices_after_model_run)
    run = _Run("20200101T000000-pinned-factor-score")
    cli.command_walk_forward(config, run, "factor_score")

    original_prices, _ = cli._backtest_window(
        observed["predictions"], pinned_bars, pinned_benchmark
    )
    current_benchmark, current_bars, _, current_version = cli._load_bar_inputs(
        config, membership
    )
    corrected_prices, _ = cli._backtest_window(
        observed["predictions"], current_bars, current_benchmark
    )
    assert current_version != pinned_version
    assert cli.dataframe_sha256(corrected_prices) != cli.dataframe_sha256(original_prices)
    assert run.payload["bar_data_version"] == pinned_version
    assert run.payload["backtest_stock_bars_sha256"] == cli.dataframe_sha256(original_prices)


def test_walk_forward_and_compare_commands_are_offline_and_complete(tmp_path, monkeypatch):
    dates, dataset, bars, benchmark = _inputs()
    config = _config(tmp_path)
    _, membership, _ = _write_verified_inputs(
        config,
        dataset,
    )
    monkeypatch.setattr(cli, "_load_benchmark_with_warmup", lambda current: benchmark)
    monkeypatch.setattr(cli, "_load_stock_bars", lambda current, symbols: bars)
    monkeypatch.setattr(
        cli,
        "_load_verified_membership",
        lambda current, calendar: membership,
    )
    observed_minimum_cross_sections = []
    real_run_fast_backtest = cli.run_fast_backtest

    def recording_run_fast_backtest(*args, **kwargs):
        observed_minimum_cross_sections.append(
            kwargs["config"].minimum_cross_section
        )
        return real_run_fast_backtest(*args, **kwargs)

    monkeypatch.setattr(cli, "run_fast_backtest", recording_run_fast_backtest)

    factor_run = _Run("20200101T000000-factor-score")
    ridge_run = _Run("20200102T000000-ridge")
    lightgbm_run = _Run("20200103T000000-lightgbm")
    xgboost_run = _Run("20200104T000000-xgboost")
    runs = {
        "factor_score": factor_run,
        "ridge": ridge_run,
        "lightgbm": lightgbm_run,
        "xgboost": xgboost_run,
    }
    for model_name, model_run in runs.items():
        cli.command_walk_forward(config, model_run, model_name)
    assert observed_minimum_cross_sections == [
        config.evaluation.min_cross_section
    ] * 4

    for model_name, run in runs.items():
        output = config.paths.model_results_dir / model_name / run.run_id
        assert (output / "summary.md").is_file()
        assert (output / "predictions.parquet").is_file()
        assert (output / "nav.csv").is_file()
        assert (output / "execution_nav.csv").is_file()
        assert (output / "execution_rejections.csv").is_file()
        prediction_summary = pd.read_csv(output / "prediction_summary.csv")
        assert {"BULL", "BEAR"}.issubset(
            set(
                prediction_summary.loc[
                    prediction_summary["scope"] == "REGIME", "period"
                ]
            )
        )
        assert (output / "regime_performance.png").is_file()
        if model_name in {"lightgbm", "xgboost"}:
            assert (output / "feature_importance.csv").is_file()
            assert (output / "feature_importance.png").is_file()
        predictions = pd.read_parquet(output / "predictions.parquet")
        trades = pd.read_csv(output / "trades.csv", parse_dates=["signal_date", "execution_date"])
        assert not predictions.empty
        assert predictions["score"].notna().all()
        assert not trades.empty
        assert (trades["execution_date"] > trades["signal_date"]).all()
        assert run.payload["walk_forward_folds"] > 0
        assert run.payload["prediction_metrics"]["rank_ic_valid_dates"] > 0
        assert run.payload["portfolio_metrics"]["number_of_trades"] > 0
        assert run.payload["execution_portfolio_metrics"]["number_of_trades"] > 0
        assert run.payload["predictions_sha256"] == cli._file_sha256(
            output / "predictions.parquet"
        )
        cli._atomic_json(
            {
                "status": "success",
                "run_id": run.run_id,
                "command": "walk-forward",
                "model": model_name,
                "config": config.to_dict(),
                "dataset_sha256": run.payload["dataset_sha256"],
                "universe_version": run.payload["universe_version"],
                "bar_data_provenance": run.payload["bar_data_provenance"],
                "bar_data_version": run.payload["bar_data_version"],
                "backtest_stock_bars_sha256": run.payload[
                    "backtest_stock_bars_sha256"
                ],
                "backtest_benchmark_sha256": run.payload[
                    "backtest_benchmark_sha256"
                ],
                "execution_bars_sha256": run.payload["execution_bars_sha256"],
                "security_data_version": run.payload["security_data_version"],
                "security_data_source": run.payload["security_data_source"],
                "security_data_source_version": run.payload[
                    "security_data_source_version"
                ],
                "security_data_manifest_sha256": run.payload[
                    "security_data_manifest_sha256"
                ],
                "security_status_sha256": run.payload[
                    "security_status_sha256"
                ],
                "listing_dates_sha256": run.payload[
                    "listing_dates_sha256"
                ],
                "security_data_input_sha256": run.payload[
                    "security_data_input_sha256"
                ],
                "security_data_input_files": run.payload[
                    "security_data_input_files"
                ],
                "predictions_sha256": run.payload["predictions_sha256"],
                "model_artifact_sha256": run.payload[
                    "model_artifact_sha256"
                ],
                "stage_diagnostics_version": run.payload["stage_diagnostics_version"],
                "stage_diagnostic_provenance": run.payload["stage_diagnostic_provenance"],
                "inference_input_version": run.payload["inference_input_version"],
                "inference_calendar_sha256": run.payload["inference_calendar_sha256"],
                **_provenance(),
            },
            config.paths.runs_dir / run.run_id / "manifest.json",
        )

    comparison_run = _Run("20200105T000000-compare")
    cli.command_compare_models(config, comparison_run)
    comparison_dir = config.paths.report_dir / comparison_run.run_id
    comparison = pd.read_csv(comparison_dir / "model_comparison.csv")
    assert comparison["model_name"].tolist() == [
        "factor_score",
        "lightgbm",
        "ridge",
        "xgboost",
    ]
    assert (comparison_dir / "walk_forward_results.csv").is_file()
    assert (comparison_dir / "model_ic_comparison.png").is_file()
    for name in (
        "nav.csv",
        "trades.csv",
        "positions.csv",
        "execution_nav.csv",
        "execution_trades.csv",
        "execution_positions.csv",
        "execution_rejections.csv",
        "prediction_summary.csv",
        "feature_importance.csv",
        "nav_curve.png",
        "drawdown.png",
        "feature_importance.png",
        "regime_performance.png",
        "turnover.png",
        "transaction_cost.png",
    ):
        assert (comparison_dir / name).is_file()
    assert "execution_cumulative_return" in comparison.columns
    assert comparison["stage_diagnostics_available"].all()
    for table_name in ("stage_prediction_daily", "stage_prediction_summary", "stage_prediction_audit"):
        stage_table = pd.read_parquet(comparison_dir / (table_name + ".parquet"))
        assert set(stage_table["model_name"]) == set(runs)
        assert set(stage_table["stage"]) == {"TRAIN_IN_SAMPLE", "VALIDATION_SELECTION", "HISTORICAL_TEST"}
        if table_name == "stage_prediction_daily":
            assert pd.api.types.is_datetime64_ns_dtype(stage_table["date"])
        elif table_name == "stage_prediction_audit":
            assert pd.api.types.is_datetime64_ns_dtype(stage_table["signal_start"])
    assert not (comparison_dir / "stage_predictions.parquet").exists()
    assert comparison_run.payload["stage_diagnostics_available"] is True
    assert comparison_run.payload["inference_available"] is True
    assert comparison_run.payload["inference_protocol"]["primary_family_size"] == 3
    inference = pd.read_parquet(comparison_dir / "model_inference.parquet")
    assert inference["primary_hypothesis"].sum() == 3
    assert set(inference.loc[inference.primary_hypothesis, "model_name"]) == {"ridge", "lightgbm", "xgboost"}
    assert (comparison_dir / "model_inference.png").is_file()
    assert (comparison_dir / "inference_protocol.json").is_file()
    assert comparison_run.payload["stage_diagnostics_version"] == 1
    assert "TRAIN_IN_SAMPLE" in (comparison_dir / "summary.md").read_text(encoding="utf-8")
    assert comparison_run.payload["compared_models"] == [
        "factor_score",
        "ridge",
        "lightgbm",
        "xgboost",
    ]
    assert comparison_run.payload["source_predictions_sha256"] == {
        "factor_score": factor_run.payload["predictions_sha256"],
        "ridge": ridge_run.payload["predictions_sha256"],
        "lightgbm": lightgbm_run.payload["predictions_sha256"],
        "xgboost": xgboost_run.payload["predictions_sha256"],
    }
    assert comparison_run.payload["source_model_artifact_sha256"]["ridge"] == (
        ridge_run.payload["model_artifact_sha256"]
    )
    assert comparison_run.payload["security_data_manifest_sha256"] == (
        ridge_run.payload["security_data_manifest_sha256"]
    )
    assert comparison_run.payload["bar_data_version"] == ridge_run.payload["bar_data_version"]
    assert comparison_run.payload["source_bar_data_provenance"]["ridge"] == (
        ridge_run.payload["bar_data_provenance"]
    )
    assert comparison_run.payload["comparison_artifact_sha256"] == (
        cli._artifact_sha256(comparison_dir)
    )
    assert not (config.paths.report_dir / "model_comparison.csv").exists()

    ridge_manifest_path = config.paths.runs_dir / ridge_run.run_id / "manifest.json"
    ridge_manifest = json.loads(ridge_manifest_path.read_text(encoding="utf-8"))
    ridge_output = config.paths.model_results_dir / "ridge" / ridge_run.run_id
    calendar_files = {filename: (ridge_output / filename).read_bytes() for filename in (
        "evaluation_calendar.csv", "evaluation_calendar.parquet",
    )}
    try:
        # Remove a Train-only calendar date so all Test/cohort keys remain
        # untouched. A perfectly well-formed but different calendar is still
        # forbidden; independent byte and semantic hashes alone are not enough.
        altered_calendar = pd.read_parquet(ridge_output / "evaluation_calendar.parquet").iloc[1:].reset_index(drop=True)
        altered_calendar.to_csv(ridge_output / "evaluation_calendar.csv", index=False)
        altered_calendar.to_parquet(ridge_output / "evaluation_calendar.parquet", index=False)
        altered_manifest = dict(ridge_manifest,
            inference_calendar_sha256=cli.diagnostic_frame_sha256(altered_calendar),
            model_artifact_sha256=cli._artifact_sha256(ridge_output),
        )
        cli._atomic_json(altered_manifest, ridge_manifest_path)
        with pytest.raises(FactorLabError, match="different inference benchmark calendars"):
            cli.command_compare_models(config, _Run("inference-calendar-mismatch"))
        assert not (config.paths.report_dir / "inference-calendar-mismatch").exists()
        for filename in calendar_files:
            (ridge_output / filename).unlink()
        legacy_calendar_manifest = dict(ridge_manifest)
        legacy_calendar_manifest.pop("inference_input_version")
        legacy_calendar_manifest.pop("inference_calendar_sha256")
        legacy_calendar_manifest["model_artifact_sha256"] = cli._artifact_sha256(ridge_output)
        cli._atomic_json(legacy_calendar_manifest, ridge_manifest_path)
        with pytest.raises(FactorLabError, match="verified benchmark calendars; rebuild"):
            cli.command_compare_models(config, _Run("missing-inference-calendar"))
        assert not (config.paths.report_dir / "missing-inference-calendar").exists()
    finally:
        for filename, content in calendar_files.items():
            (ridge_output / filename).write_bytes(content)
        cli._atomic_json(ridge_manifest, ridge_manifest_path)
    # Explicit old reports are still readable, but cannot be compared as if
    # all four models had complete Train/Validation evidence.
    factor_manifest_path = config.paths.runs_dir / factor_run.run_id / "manifest.json"
    complete_factor_manifest = json.loads(factor_manifest_path.read_text(encoding="utf-8"))
    factor_output = config.paths.model_results_dir / "factor_score" / factor_run.run_id
    stage_bytes = {name: (factor_output / name).read_bytes() for name in cli._STAGE_ARTIFACTS}
    legacy_factor_manifest = dict(complete_factor_manifest)
    legacy_factor_manifest.pop("stage_diagnostics_version")
    legacy_factor_manifest.pop("stage_diagnostic_provenance")
    try:
        for name in stage_bytes:
            (factor_output / name).unlink()
        legacy_factor_manifest["model_artifact_sha256"] = cli._artifact_sha256(factor_output)
        cli._atomic_json(legacy_factor_manifest, factor_manifest_path)
        with pytest.raises(FactorLabError, match="Cannot mix legacy reports"):
            cli.command_compare_models(config, _Run("mixed-stage-evidence"))
        assert not (config.paths.report_dir / "mixed-stage-evidence").exists()
    finally:
        for name, content in stage_bytes.items():
            (factor_output / name).write_bytes(content)
        cli._atomic_json(complete_factor_manifest, factor_manifest_path)
    mismatched_raw_bars = json.loads(json.dumps(ridge_manifest))
    symbol = sorted(mismatched_raw_bars["bar_data_provenance"]["stocks"])[0]
    mismatched_raw_bars["bar_data_provenance"]["stocks"][symbol]["content_sha256"] = "f" * 64
    mismatched_raw_bars["bar_data_version"] = cli.bar_data_version(
        mismatched_raw_bars["bar_data_provenance"]
    )
    cli._atomic_json(mismatched_raw_bars, ridge_manifest_path)
    with pytest.raises(FactorLabError, match="different or unknown raw bar-data versions"):
        cli.command_compare_models(config, _Run("raw-bar-input-mismatch"))
    cli._atomic_json(ridge_manifest, ridge_manifest_path)

    mismatched_prices = dict(
        ridge_manifest, backtest_stock_bars_sha256="f" * 64
    )
    cli._atomic_json(mismatched_prices, ridge_manifest_path)
    with pytest.raises(FactorLabError, match="backtest stock-bar inputs"):
        cli.command_compare_models(config, _Run("backtest-input-mismatch"))
    cli._atomic_json(ridge_manifest, ridge_manifest_path)

    mismatched_security = dict(
        ridge_manifest, security_data_manifest_sha256="f" * 64
    )
    cli._atomic_json(mismatched_security, ridge_manifest_path)
    with pytest.raises(FactorLabError, match="security-data identity"):
        cli.command_compare_models(config, _Run("security-input-mismatch"))
    cli._atomic_json(ridge_manifest, ridge_manifest_path)

    factor_manifest_path = config.paths.runs_dir / factor_run.run_id / "manifest.json"
    factor_manifest = json.loads(factor_manifest_path.read_text(encoding="utf-8"))
    for path, payload in (
        (factor_manifest_path, factor_manifest),
        (ridge_manifest_path, ridge_manifest),
    ):
        cli._atomic_json(dict(payload, dataset_sha256="unknown"), path)
    with pytest.raises(FactorLabError, match="unknown dataset versions"):
        cli.command_compare_models(config, _Run("unknown-dataset-version"))
    cli._atomic_json(factor_manifest, factor_manifest_path)
    cli._atomic_json(ridge_manifest, ridge_manifest_path)

    ridge_output = config.paths.model_results_dir / "ridge" / ridge_run.run_id
    for artifact_name in ("prediction_summary.csv", "portfolio_metrics.csv"):
        artifact_path = ridge_output / artifact_name
        original_artifact_bytes = artifact_path.read_bytes()
        artifact_path.write_bytes(original_artifact_bytes + b"\ntampered")
        with pytest.raises(FactorLabError, match="artifact SHA-256 mismatch"):
            cli.command_compare_models(
                config, _Run("%s-hash-mismatch" % artifact_name)
            )
        artifact_path.write_bytes(original_artifact_bytes)

    mismatched_provenance = dict(ridge_manifest, git_commit="b" * 40)
    cli._atomic_json(mismatched_provenance, ridge_manifest_path)
    with pytest.raises(FactorLabError, match="git_commit"):
        cli.command_compare_models(config, _Run("provenance-mismatch"))

    missing_provenance = dict(ridge_manifest)
    missing_provenance.pop("package_versions")
    cli._atomic_json(missing_provenance, ridge_manifest_path)
    with pytest.raises(FactorLabError, match="missing or unknown provenance"):
        cli.command_compare_models(config, _Run("missing-provenance"))
    cli._atomic_json(ridge_manifest, ridge_manifest_path)

    ridge_prediction_path = (
        config.paths.model_results_dir
        / "ridge"
        / ridge_run.run_id
        / "predictions.parquet"
    )
    original_prediction_bytes = ridge_prediction_path.read_bytes()
    ridge_prediction_path.write_bytes(original_prediction_bytes + b"tampered")
    with pytest.raises(FactorLabError, match="SHA-256 mismatch"):
        cli.command_compare_models(config, _Run("prediction-hash-mismatch"))
    ridge_prediction_path.write_bytes(original_prediction_bytes)

    mismatched_target = pd.read_parquet(ridge_prediction_path)
    mismatched_target.loc[mismatched_target.index[0], "EXCESS_RET5"] += 0.01
    mismatched_target.to_parquet(ridge_prediction_path, index=False)
    ridge_manifest["predictions_sha256"] = cli._file_sha256(ridge_prediction_path)
    ridge_manifest["model_artifact_sha256"]["predictions.parquet"] = (
        ridge_manifest["predictions_sha256"]
    )
    cli._atomic_json(ridge_manifest, ridge_manifest_path)
    with pytest.raises(FactorLabError, match="identical out-of-sample"):
        cli.command_compare_models(config, _Run("oos-target-mismatch"))

    ridge_prediction_path.write_bytes(original_prediction_bytes)
    ridge_manifest["predictions_sha256"] = cli._file_sha256(ridge_prediction_path)
    ridge_manifest["model_artifact_sha256"]["predictions.parquet"] = (
        ridge_manifest["predictions_sha256"]
    )
    cli._atomic_json(ridge_manifest, ridge_manifest_path)

    mismatched_keys = pd.read_parquet(ridge_prediction_path)
    mismatched_keys.loc[mismatched_keys.index[0], "fold"] += 1000
    mismatched_keys.to_parquet(ridge_prediction_path, index=False)
    ridge_manifest["predictions_sha256"] = cli._file_sha256(ridge_prediction_path)
    ridge_manifest["model_artifact_sha256"]["predictions.parquet"] = (
        ridge_manifest["predictions_sha256"]
    )
    cli._atomic_json(ridge_manifest, ridge_manifest_path)
    with pytest.raises(FactorLabError, match="identical out-of-sample"):
        cli.command_compare_models(config, _Run("oos-key-mismatch"))
