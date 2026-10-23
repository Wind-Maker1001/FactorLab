"""Static local bundles preserve explicit contracts and verified generations."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from factorlab.data.providers import local
from factorlab.data.providers.local import (
    LOCAL_MARKET_CONTRACTS,
    LocalParquetProvider,
    publish_local_market_data,
)
from factorlab.data.schema import CANONICAL_BAR_COLUMNS, NUMERIC_BAR_COLUMNS
from factorlab.errors import PointInTimeUniverseRequiredError, ProviderError


def _bars(symbol, dates, multiplier=1.0):
    rows = []
    for position, date in enumerate(dates):
        close = 10.0 + position
        row = {"date": date, "symbol": symbol, "volume": 1000.0,
               "amount": 10000.0, "turnover": 0.01, "adj_factor": multiplier}
        for field, value in (("open", close), ("high", close + 1),
                             ("low", close - 1), ("close", close)):
            row[field] = value
            row["raw_" + field] = value
            row["adj_" + field] = value * multiplier
        rows.append(row)
    return pd.DataFrame(rows, columns=CANONICAL_BAR_COLUMNS)


@pytest.fixture
def inputs(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=5)
    stocks = pd.concat([_bars("600000.SH", dates, 2.0),
                        _bars("000001.SZ", dates, 3.0)], ignore_index=True)
    benchmark = _bars("000905.SH", dates)
    stock_path, benchmark_path = tmp_path / "stocks.csv", tmp_path / "benchmark.csv"
    stocks.to_csv(stock_path, index=False)
    benchmark.to_csv(benchmark_path, index=False)
    return SimpleNamespace(stocks=stocks, benchmark=benchmark, dates=dates,
                           stock_path=stock_path, benchmark_path=benchmark_path,
                           root=tmp_path / "bundle")


def _publish(inputs, **kwargs):
    return publish_local_market_data(inputs.stock_path, inputs.benchmark_path,
                                     inputs.root, "test-only-synthetic", "fixture-v1",
                                     **kwargs)


def _replace_manifest(inputs, manifest, recompute=False):
    if recompute:
        manifest["bundle_version"] = local._bundle_version(manifest)
    (inputs.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_publish_canonical_hfq_contract_and_calendar(inputs):
    manifest = _publish(inputs)
    assert manifest["contracts"] == LOCAL_MARKET_CONTRACTS
    assert manifest["source_declaration"] == "user-provided-not-independently-verified"
    assert manifest["stock_symbols"] == ["000001.SZ", "600000.SH"]
    assert manifest["stocks_rows"] == 10
    for role, path in (("stocks", inputs.stock_path), ("benchmark", inputs.benchmark_path)):
        assert manifest["input_sha256"][role] == hashlib.sha256(path.read_bytes()).hexdigest()
        artifact = inputs.root / manifest[role + "_file"]
        assert artifact.is_file()
        assert manifest[role + "_file_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    provider = LocalParquetProvider(inputs.root)
    bars = provider.get_daily_bars("600000.SH", inputs.dates[0], inputs.dates[-1])
    assert bars["adj_close"].eq(bars["raw_close"] * 2).all()
    assert bars["close"].eq(bars["raw_close"]).all()
    assert bars["date"].dtype == "datetime64[ns]"
    assert provider.get_trade_calendar(inputs.dates[0], inputs.dates[-1]).equals(inputs.dates)
    assert provider.cache_source_version("600000.SH", False).endswith("contract=stock-raw+hfq-v1")
    assert provider.cache_source_version("000905.SH", True).endswith("contract=index-raw-v1")


def test_parquet_input_supported_and_unsorted_input_sorted(inputs):
    inputs.stock_path = inputs.stock_path.with_suffix(".parquet")
    inputs.benchmark_path = inputs.benchmark_path.with_suffix(".pq")
    inputs.stocks.iloc[::-1].to_parquet(inputs.stock_path, index=False)
    inputs.benchmark.iloc[::-1].to_parquet(inputs.benchmark_path, index=False)
    _publish(inputs)
    selected = LocalParquetProvider(inputs.root).get_daily_bars("600000.SH", "2024-01-02", "2024-01-10")
    assert selected["date"].is_monotonic_increasing


def test_provider_operations_do_not_use_network(inputs, monkeypatch):
    import requests
    import baostock

    def forbidden(*args, **kwargs):
        pytest.fail("local provider attempted a network operation")
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(baostock, "login", forbidden)
    _publish(inputs)
    provider = LocalParquetProvider(inputs.root)
    provider.get_daily_bars("000001.SZ", "2024-01-02", "2024-01-08")
    provider.get_index_bars("000905.SH", "2024-01-02", "2024-01-08")
    provider.get_trade_calendar("2024-01-02", "2024-01-08")
    assert provider.source_version


def test_market_bundle_is_not_a_pit_universe(inputs):
    provider = LocalParquetProvider(inputs.root)
    with pytest.raises(PointInTimeUniverseRequiredError, match="POINT-IN-TIME UNIVERSE REQUIRED"):
        provider.get_index_constituents("000905.SH", "2024-01-02")
    assert not inputs.root.exists()


def test_all_reads_and_source_identity_pin_one_generation(inputs, monkeypatch):
    first = _publish(inputs)
    provider = LocalParquetProvider(inputs.root)
    reads = []
    original = Path.read_bytes
    def tracked(path):
        if path.parent == inputs.root:
            reads.append(path.name)
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", tracked)
    old_identity = provider.source_version
    old_bars = provider.get_daily_bars("600000.SH", "2024-01-02", "2024-01-08")
    for symbol in ("600000.SH", "000001.SZ") * 10:
        provider.get_daily_bars(symbol, "2024-01-02", "2024-01-08")
        provider.cache_source_version(symbol, False)
    provider.get_index_bars("000905.SH", "2024-01-02", "2024-01-08")
    assert reads == ["manifest.json", first["stocks_file"], first["benchmark_file"]]
    modified = inputs.stocks.copy()
    for field in ("open", "high", "low", "close"):
        modified[field] *= 1.1
        modified["raw_" + field] *= 1.1
        modified["adj_" + field] *= 1.1
    modified.to_csv(inputs.stock_path, index=False)
    second = _publish(inputs)
    assert second["bundle_version"] != first["bundle_version"]
    assert provider.source_version == old_identity
    assert_frame_equal(provider.get_daily_bars("600000.SH", "2024-01-02", "2024-01-08"), old_bars)
    assert LocalParquetProvider(inputs.root).source_version != old_identity
    assert (inputs.root / first["stocks_file"]).is_file()
    assert (inputs.root / first["benchmark_file"]).is_file()


def test_returns_copies_not_mutable_internal_state(inputs):
    manifest = _publish(inputs)
    manifest["contracts"]["price"] = "USD"
    provider = LocalParquetProvider(inputs.root)
    returned = provider.get_bundle_manifest()
    returned["contracts"]["price"] = "USD"
    returned["stock_symbols"].clear()
    frame = provider.get_daily_bars("600000.SH", "2024-01-02", "2024-01-08")
    frame.loc[:, "close"] = -1
    assert provider.bundle_manifest["contracts"]["price"] == "CNY"
    assert provider.bundle_manifest["stock_symbols"]
    assert provider.get_daily_bars("600000.SH", "2024-01-02", "2024-01-08")["close"].gt(0).all()


def test_semantic_identity_excludes_locator_time_and_generation(inputs):
    first = _publish(inputs)
    second = _publish(inputs)
    assert first["generation_id"] != second["generation_id"]
    assert first["bundle_version"] == second["bundle_version"]
    for role in ("stocks", "benchmark"):
        # Change the input paths without changing their exact bytes.
        origin = inputs.stock_path if role == "stocks" else inputs.benchmark_path
        destination = origin.with_name("moved-" + origin.name)
        destination.write_bytes(origin.read_bytes())
        if role == "stocks":
            inputs.stock_path = destination
        else:
            inputs.benchmark_path = destination
    third = _publish(inputs)
    assert third["bundle_version"] == first["bundle_version"]
    assert third["input_files"] != first["input_files"]


def test_source_declaration_changes_semantic_identity(inputs):
    first = _publish(inputs)
    second = publish_local_market_data(inputs.stock_path, inputs.benchmark_path,
                                      inputs.root, "different-source", "fixture-v1")
    third = publish_local_market_data(inputs.stock_path, inputs.benchmark_path,
                                     inputs.root, "different-source", "fixture-v2")
    assert len({first["bundle_version"], second["bundle_version"], third["bundle_version"]}) == 3


@pytest.mark.parametrize("field", ["source", "source_version"])
@pytest.mark.parametrize("value", ["", " unknown", "unknown", "NONE", "null", "n/a", "x\ny", None, 5])
def test_source_requires_explicit_declaration(inputs, field, value):
    args = {"source": "test-only-synthetic", "source_version": "fixture-v1"}
    args[field] = value
    with pytest.raises(ProviderError, match="explicit"):
        publish_local_market_data(inputs.stock_path, inputs.benchmark_path, inputs.root, **args)
    assert not inputs.root.exists()


@pytest.mark.parametrize("column", NUMERIC_BAR_COLUMNS)
@pytest.mark.parametrize("value", [np.inf, -np.inf])
def test_numeric_infinity_is_rejected(inputs, column, value):
    malformed = inputs.stocks.copy()
    malformed.loc[0, column] = value
    malformed.to_parquet(inputs.stock_path.with_suffix(".parquet"), index=False)
    inputs.stock_path = inputs.stock_path.with_suffix(".parquet")
    with pytest.raises(ProviderError, match="finite"):
        _publish(inputs)


@pytest.mark.parametrize("mutation", ["missing_raw", "missing_adj", "extra", "duplicate", "bad_ohlc", "bad_factor", "negative_volume", "numeric_symbol", "vendor_symbol", "numeric_date", "time", "timezone", "unknown_date", "bool_volume"])
def test_reject_implicit_or_invalid_market_data(inputs, mutation):
    frame = inputs.stocks.copy()
    if mutation == "missing_raw":
        frame = frame.drop(columns="raw_open")
    elif mutation == "missing_adj":
        frame = frame.drop(columns="adj_open")
    elif mutation == "extra":
        frame["adjustment_guess"] = 2
    elif mutation == "duplicate":
        frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    elif mutation == "bad_ohlc":
        frame.loc[0, "low"] = 100
    elif mutation == "bad_factor":
        frame.loc[0, "adj_factor"] = 1
    elif mutation == "negative_volume":
        frame.loc[0, "volume"] = -1
    elif mutation == "numeric_symbol":
        frame["symbol"] = 600000
    elif mutation == "vendor_symbol":
        frame["symbol"] = "sh600000"
    elif mutation == "numeric_date":
        frame["date"] = 20240102
    elif mutation == "time":
        frame["date"] += pd.Timedelta(hours=1)
    elif mutation == "timezone":
        frame["date"] = frame["date"].dt.tz_localize("Asia/Shanghai")
    elif mutation == "unknown_date":
        frame.loc[0, "date"] = pd.NaT
    elif mutation == "bool_volume":
        frame["volume"] = True
    inputs.stock_path = inputs.stock_path.with_suffix(".parquet")
    frame.to_parquet(inputs.stock_path, index=False)
    with pytest.raises(ProviderError):
        _publish(inputs)


@pytest.mark.parametrize("block", ["canonical", "raw", "adj"])
def test_one_price_block_cannot_be_missing_alone(inputs, block):
    frame = inputs.stocks.copy()
    prefix = "" if block == "canonical" else block + "_"
    for field in ("open", "high", "low", "close"):
        frame.loc[0, prefix + field] = np.nan
    frame.to_csv(inputs.stock_path, index=False)
    with pytest.raises(ProviderError, match="missing|explicit"):
        _publish(inputs)


def test_full_suspension_preserves_missing_prices_without_fill(inputs):
    frame = inputs.stocks.copy()
    for field in ("open", "high", "low", "close"):
        for prefix in ("", "raw_", "adj_"):
            frame.loc[1, prefix + field] = np.nan
    frame.loc[1, ["adj_factor", "volume", "amount", "turnover"]] = [np.nan, 0, 0, np.nan]
    frame.to_csv(inputs.stock_path, index=False)
    _publish(inputs)
    selected = LocalParquetProvider(inputs.root).get_daily_bars("600000.SH", "2024-01-02", "2024-01-08")
    assert selected.loc[1, ["open", "raw_open", "adj_open", "adj_factor"]].isna().all()
    assert selected.loc[1, "volume"] == 0


@pytest.mark.parametrize("mutation", ["wrong_symbol", "hfq", "missing", "calendar"])
def test_benchmark_must_be_complete_raw_declared_index(inputs, mutation):
    frame = inputs.benchmark.copy()
    if mutation == "wrong_symbol":
        frame["symbol"] = "000300.SH"
    elif mutation == "hfq":
        for field in ("open", "high", "low", "close"):
            frame["adj_" + field] *= 2
        frame["adj_factor"] = 2
    elif mutation == "missing":
        for field in ("open", "high", "low", "close"):
            frame.loc[0, field] = np.nan
    elif mutation == "calendar":
        frame = frame.iloc[1:]
    frame.to_csv(inputs.benchmark_path, index=False)
    with pytest.raises(ProviderError):
        _publish(inputs)


@pytest.mark.parametrize("mutation", ["unknown_field", "missing_field", "bool_schema", "schema", "contracts", "path_escape", "generation", "bad_hash", "bad_identity", "bad_timestamp", "naive_timestamp", "bad_rows", "bad_bounds", "bad_symbols", "duplicate_symbols", "semantic_hash", "wrong_benchmark"])
def test_manifest_rejects_invalid_metadata(inputs, mutation):
    manifest = _publish(inputs)
    if mutation == "unknown_field":
        manifest["extra"] = True
    elif mutation == "missing_field":
        del manifest["contracts"]
    elif mutation == "bool_schema":
        manifest["schema_version"] = True
    elif mutation == "schema":
        manifest["schema_version"] = 100
    elif mutation == "contracts":
        manifest["contracts"]["volume"] = "lots"
    elif mutation == "path_escape":
        manifest["stocks_file"] = "../stocks.parquet"
    elif mutation == "generation":
        manifest["generation_id"] = "../x"
    elif mutation == "bad_hash":
        manifest["stocks_file_sha256"] = "not-a-hash"
    elif mutation == "bad_identity":
        manifest["source"] = "unknown"
    elif mutation == "bad_timestamp":
        manifest["generated_at"] = "not-a-time"
    elif mutation == "naive_timestamp":
        manifest["generated_at"] = "2024-01-01T10:00:00"
    elif mutation == "bad_rows":
        manifest["stocks_rows"] = True
    elif mutation == "bad_bounds":
        manifest["stocks_start"] = "2025-01-01"
    elif mutation == "bad_symbols":
        manifest["stock_symbols"] = ["sh600000"]
    elif mutation == "duplicate_symbols":
        manifest["stock_symbols"] *= 2
    elif mutation == "semantic_hash":
        manifest["bundle_version"] = "0" * 64
    elif mutation == "wrong_benchmark":
        manifest["benchmark_symbol"] = "000300.SH"
    _replace_manifest(inputs, manifest)
    with pytest.raises(ProviderError):
        _ = LocalParquetProvider(inputs.root).source_version


@pytest.mark.parametrize("field,value", [("stocks_rows", 11), ("benchmark_rows", 6), ("stocks_start", "2024-01-01"), ("stocks_end", "2024-01-09"), ("benchmark_start", "2024-01-01"), ("stock_symbols", ["600000.SH"])])
def test_semantically_hashed_stats_still_must_match_actual_frames(inputs, field, value):
    manifest = _publish(inputs)
    manifest[field] = value
    _replace_manifest(inputs, manifest, recompute=True)
    with pytest.raises(ProviderError, match="disagree"):
        _ = LocalParquetProvider(inputs.root).source_version


@pytest.mark.parametrize("role", ["stocks", "benchmark"])
def test_file_hash_mismatch_blocks_provider_identity(inputs, role):
    manifest = _publish(inputs)
    with (inputs.root / manifest[role + "_file"]).open("ab") as handle:
        handle.write(b"tampering")
    with pytest.raises(ProviderError, match="SHA-256 mismatch"):
        _ = LocalParquetProvider(inputs.root).source_version


def test_no_reference_to_missing_or_invalid_parquet(inputs):
    manifest = _publish(inputs)
    path = inputs.root / manifest["stocks_file"]
    path.unlink()
    with pytest.raises(ProviderError, match="missing or unreadable"):
        _ = LocalParquetProvider(inputs.root).source_version
    path.write_bytes(b"invalid parquet")
    manifest["stocks_file_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    _replace_manifest(inputs, manifest, recompute=True)
    with pytest.raises(ProviderError, match="cannot be parsed"):
        _ = LocalParquetProvider(inputs.root).source_version


def test_parse_and_hash_use_same_input_bytes(inputs, monkeypatch):
    original_content = inputs.stock_path.read_bytes()
    original = Path.read_bytes
    count = 0
    def raced(path):
        nonlocal count
        content = original(path)
        if path == inputs.stock_path:
            count += 1
            path.write_bytes(b"bad replacement after verified read")
        return content
    monkeypatch.setattr(Path, "read_bytes", raced)
    manifest = _publish(inputs)
    assert count == 1
    assert manifest["input_sha256"]["stocks"] == hashlib.sha256(original_content).hexdigest()
    assert len(LocalParquetProvider(inputs.root).get_daily_bars("600000.SH", "2024-01-02", "2024-01-08")) == 5


def test_provider_parses_same_artifact_bytes_that_were_hashed(inputs, monkeypatch):
    manifest = _publish(inputs)
    target = inputs.root / manifest["stocks_file"]
    original = Path.read_bytes
    def raced(path):
        content = original(path)
        if path == target:
            path.write_bytes(b"changed after read")
        return content
    monkeypatch.setattr(Path, "read_bytes", raced)
    provider = LocalParquetProvider(inputs.root)
    assert len(provider.get_daily_bars("600000.SH", "2024-01-02", "2024-01-08")) == 5
    assert provider.bundle_version == manifest["bundle_version"]


@pytest.mark.parametrize("stage", ["stocks", "benchmark", "manifest"])
def test_failed_publish_preserves_previous_committed_generation(inputs, monkeypatch, stage):
    manifest = _publish(inputs)
    pointer = (inputs.root / "manifest.json").read_bytes()
    original = local._write_immutable_parquet
    def failed_write(frame, path):
        if path.name.startswith(stage + "."):
            raise OSError("injected artifact failure")
        return original(frame, path)
    if stage == "manifest":
        def failed_replace(*args):
            raise OSError("injected pointer failure")
        monkeypatch.setattr(local.os, "replace", failed_replace)
    else:
        monkeypatch.setattr(local, "_write_immutable_parquet", failed_write)
    with pytest.raises(ProviderError, match="cannot publish"):
        _publish(inputs)
    assert (inputs.root / "manifest.json").read_bytes() == pointer
    assert not list(inputs.root.glob(".manifest.json.tmp-*"))
    assert LocalParquetProvider(inputs.root).bundle_version == manifest["bundle_version"]


def test_uuid_collision_does_not_overwrite_existing_artifacts(inputs, monkeypatch):
    manifest = _publish(inputs)
    old = (inputs.root / manifest["stocks_file"]).read_bytes()
    monkeypatch.setattr(local.uuid, "uuid4", lambda: SimpleNamespace(hex=manifest["generation_id"]))
    with pytest.raises(ProviderError, match="cannot publish"):
        _publish(inputs)
    assert (inputs.root / manifest["stocks_file"]).read_bytes() == old
    assert LocalParquetProvider(inputs.root).bundle_version == manifest["bundle_version"]


def test_manifest_temp_collision_does_not_remove_other_writer(tmp_path, monkeypatch):
    generation = "a" * 32
    destination = tmp_path / "manifest.json"
    temporary = tmp_path / (".manifest.json.tmp-" + generation)
    temporary.write_bytes(b"other writer owns this")
    monkeypatch.setattr(local.uuid, "uuid4", lambda: SimpleNamespace(hex=generation))
    with pytest.raises(FileExistsError):
        local._atomic_manifest({"test": True}, destination)
    assert temporary.read_bytes() == b"other writer owns this"
    assert not destination.exists()


def test_manifest_cleanup_failure_keeps_original_exception(tmp_path, monkeypatch):
    def failed_replace(*args):
        raise OSError("original publication failure")
    original = Path.unlink
    def failed_unlink(path, *args, **kwargs):
        if path.name.startswith(".manifest.json.tmp-"):
            raise PermissionError("cleanup blocked")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(local.os, "replace", failed_replace)
    monkeypatch.setattr(Path, "unlink", failed_unlink)
    with pytest.raises(OSError, match="original publication failure"):
        local._atomic_manifest({"test": True}, tmp_path / "manifest.json")


@pytest.mark.parametrize("value", [0, np.int64(0), np.float64(0), True, np.bool_(True), "20240102", "2024-01-02T00:00:00", "2024-01-02T01:00:00Z", pd.NaT, pd.Timestamp("2024-01-02", tz="UTC")])
def test_request_dates_reject_numeric_or_noncanonical_values(inputs, value):
    _publish(inputs)
    with pytest.raises(ProviderError):
        LocalParquetProvider(inputs.root).get_daily_bars("600000.SH", value, "2024-01-08")


def test_missing_stock_and_inverted_window_are_errors_but_empty_valid_window_is_valid(inputs):
    _publish(inputs)
    provider = LocalParquetProvider(inputs.root)
    with pytest.raises(ProviderError, match="absent"):
        provider.get_daily_bars("600001.SH", "2024-01-02", "2024-01-08")
    with pytest.raises(ProviderError, match="on or before"):
        provider.get_daily_bars("600000.SH", "2024-01-08", "2024-01-02")
    frame = provider.get_daily_bars("600000.SH", "2025-01-01", "2025-01-02")
    assert frame.empty
    assert tuple(frame.columns) == CANONICAL_BAR_COLUMNS
    assert frame["date"].dtype == "datetime64[ns]"
    with pytest.raises(ProviderError, match="declared benchmark"):
        provider.get_index_bars("000300.SH", "2024-01-02", "2024-01-08")


def test_symlink_artifact_is_rejected_even_when_hash_matches(inputs, tmp_path):
    manifest = _publish(inputs)
    artifact = inputs.root / manifest["stocks_file"]
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(artifact.read_bytes())
    artifact.unlink()
    try:
        artifact.symlink_to(outside)
    except OSError:
        pytest.skip("host does not allow symlink creation")
    with pytest.raises(ProviderError, match="symlink"):
        _ = LocalParquetProvider(inputs.root).source_version


def test_root_symlink_is_rejected(inputs, tmp_path):
    _publish(inputs)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(inputs.root, target_is_directory=True)
    except OSError:
        pytest.skip("host does not allow symlink creation")
    with pytest.raises(ProviderError, match="symlink"):
        _ = LocalParquetProvider(alias).source_version


def test_parallel_queries_pin_and_read_each_artifact_once(inputs, monkeypatch):
    manifest = _publish(inputs)
    original = Path.read_bytes
    reads = []
    def counted(path):
        if path.parent == inputs.root:
            reads.append(path.name)
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", counted)
    provider = LocalParquetProvider(inputs.root)
    def query(symbol):
        assert provider.source_version
        return len(provider.get_daily_bars(symbol, "2024-01-02", "2024-01-08"))
    with ThreadPoolExecutor(max_workers=8) as executor:
        lengths = list(executor.map(query, ("600000.SH", "000001.SZ") * 20))
    assert lengths == [5] * 40
    assert reads == ["manifest.json", manifest["stocks_file"], manifest["benchmark_file"]]


@pytest.mark.parametrize("role", ["stocks", "benchmark"])
def test_unreadable_input_does_not_publish_or_alter_existing_bundle(inputs, role):
    _publish(inputs)
    previous = (inputs.root / "manifest.json").read_bytes()
    path = inputs.stock_path if role == "stocks" else inputs.benchmark_path
    path.unlink()
    with pytest.raises(ProviderError, match="cannot read input"):
        _publish(inputs)
    assert (inputs.root / "manifest.json").read_bytes() == previous


@pytest.mark.parametrize("content,suffix", [(b"not parquet", ".parquet"), (b"x,y\n1,2", ".csv"), (b"whatever", ".json")])
def test_bad_input_files_are_rejected_before_publication(inputs, content, suffix):
    inputs.stock_path = inputs.stock_path.with_suffix(suffix)
    inputs.stock_path.write_bytes(content)
    with pytest.raises(ProviderError):
        _publish(inputs)
    assert not inputs.root.exists()


@pytest.mark.parametrize("content", [b"{not-json", b"\xff\xfe", b"[]", b"null"])
def test_unreadable_manifest_blocks_first_pin(inputs, content):
    _publish(inputs)
    (inputs.root / "manifest.json").write_bytes(content)
    with pytest.raises(ProviderError):
        _ = LocalParquetProvider(inputs.root).source_version


def test_existing_file_cannot_be_a_bundle_root(inputs):
    inputs.root.write_text("preserve existing non-directory", encoding="utf-8")
    with pytest.raises(ProviderError, match="root is inaccessible"):
        _publish(inputs)
    assert inputs.root.read_text(encoding="utf-8") == "preserve existing non-directory"


def test_root_creation_failure_uses_provider_error(inputs, monkeypatch):
    original = Path.mkdir
    def denied(path, *args, **kwargs):
        if path == inputs.root:
            raise PermissionError("injected inaccessible root")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(ProviderError, match="root is inaccessible"):
        _publish(inputs)
