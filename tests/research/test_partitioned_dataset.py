"""Partition boundaries, trusted pins, failure atomicity and past-only semantics."""

from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from examples.research.dataset_registry_v2_demo import synthetic_inputs
from examples.research.scalable_factor_computation import configured, registry
from factorlab.dataset.identity import frame_hash, seal
from factorlab.dataset.partitioned import ChunkedConfig, PartitionedDataset, ResourceLimitError
from factorlab.errors import ConfigurationError, SchemaError
from factorlab.factors.registry_v2 import FactorDefinition, FactorRegistryV2


@pytest.fixture
def inputs():
    dates, bars, benchmark, members, status, listings = synthetic_inputs(200, 6)
    # Historical membership turnover, late listing, terminal missing prices,
    # suspended and unknown statuses, zero-volume and already missing sessions.
    symbols = sorted(bars.symbol.unique())
    listings.loc[listings.symbol == symbols[1], "listing_date"] = dates[25]
    status.loc[(status.symbol == symbols[2]) & (status.date == dates[130]), "trade_status"] = 0
    status = status.loc[~((status.symbol == symbols[3]) & (status.date == dates[140]))]
    bars = bars.loc[~((bars.symbol == symbols[4]) & (bars.date >= dates[180]))]
    members = members.loc[~(((members.symbol == symbols[0]) & (members.date >= dates[160]))
                           | ((members.symbol == symbols[5]) & (members.date < dates[160])))]
    return dates, bars, benchmark, members, status, listings


def builder():
    obj = configured(5, 14)
    obj.options = replace(obj.options, filter_config=replace(obj.options.filter_config, min_listed_days=1))
    return obj


@pytest.mark.parametrize("symbols,factors,dates,workers", [(1, 1, 1, 1), (2, 5, 13, 2), (6, 16, 20, 4)])
def test_dense_dataset_exact_equivalence(inputs, tmp_path, symbols, factors, dates, workers):
    calendar, *frames = inputs
    obj = builder()
    dense = obj.build(*frames, calendar)
    cfg = ChunkedConfig(symbols_per_batch=symbols, factors_per_batch=factors,
                        dates_per_batch=dates, workers=workers)
    bundle = obj.build_chunked(*frames, calendar, output_dir=tmp_path / "dataset", config=cfg)
    loaded = obj.read_partitioned(bundle.path, calendar=calendar, expected_identity_hash=bundle.identity_hash)
    actual = loaded.to_frame()
    pd.testing.assert_frame_equal(actual, dense, check_exact=True)
    assert actual.attrs == dense.attrs
    assert bundle.manifest["counts"]["successful_factors"] == 14
    assert bundle.manifest["counts"]["selected_factors"] is None


def test_ast_alias_dedup_and_order(inputs, tmp_path):
    dates, bars, *_ = inputs
    snapshot, refs = registry(100)
    # Compare actual AST execution to dense interpreter with bounded batches.
    requested = (refs[-1], refs[0], refs[20], refs[-5])
    registry_copy = FactorRegistryV2()
    registry_copy.register_many(snapshot.definitions)
    root = snapshot.get(requested[0].factor_id, "1")
    copy = replace(root, factor_id="DUPLICATE")
    registry_copy.register(copy)
    alias = FactorDefinition.alias("ALIAS", "1", copy)
    registry_copy.register(alias)
    snapshot = registry_copy.freeze()
    requested += (copy.ref, alias.ref)
    expected = snapshot.compute(bars, requested, calendar=dates)
    bundle = snapshot.compute_chunked(bars.sample(frac=1, random_state=9), requested,
        calendar=dates, output_dir=tmp_path / "raw", config=ChunkedConfig(symbols_per_batch=2, factors_per_batch=2))
    actual = bundle.to_frame()
    pd.testing.assert_frame_equal(actual, expected, check_exact=True)
    assert bundle.manifest["counts"]["unique_executed_definitions"] == 4
    assert bundle.manifest["counts"]["requested_candidates"] == 6


def test_future_perturbation(inputs, tmp_path):
    dates, *frames = inputs
    obj = builder()
    original = obj.build_chunked(*frames, dates, output_dir=tmp_path / "original").to_frame()
    changed = [f.copy() for f in frames]
    bars = changed[0]
    future = bars.date > dates[150]
    price_columns = [n for n in bars if n.endswith(("open", "high", "low", "close"))]
    bars.loc[future, price_columns] *= 1.4
    updated = obj.build_chunked(*changed, dates, output_dir=tmp_path / "changed").to_frame()
    columns = [n for n in original if n not in ("entry_date", "exit_date") and "FWD_RET" not in n and not n.startswith("EXCESS_RET")]
    pd.testing.assert_frame_equal(original.loc[original.date <= dates[150], columns],
                                  updated.loc[updated.date <= dates[150], columns], check_exact=True)
    assert not original.EXCESS_RET5.equals(updated.EXCESS_RET5)


def test_research_future_label_interval_and_shuffled_inputs(inputs, tmp_path):
    dates, *frames = inputs
    frames[2] = frames[2].loc[frames[2].date <= dates[175]]
    obj = builder()
    expected = obj.build(*frames, dates)
    # The existing market-regime kernel requires an ordered benchmark.
    # Keyed security inputs may be unordered; retain that benchmark contract.
    shuffled = [f if i == 1 else f.sample(frac=1, random_state=8)
                for i, f in enumerate(frames)]
    actual = obj.build_chunked(*shuffled, dates, output_dir=tmp_path / "interval").to_frame()
    pd.testing.assert_frame_equal(actual, expected, check_exact=True)
    assert actual.date.max() == dates[175]
    assert actual.loc[actual.date == dates[175], "EXCESS_RET5"].notna().any()


@pytest.mark.parametrize("damage", ["corrupt", "missing", "duplicate_keys", "schema", "overlap", "extra"])
def test_partition_integrity(inputs, tmp_path, damage):
    dates, *frames = inputs
    bundle = builder().build_chunked(*frames, dates, output_dir=tmp_path / "bundle")
    manifest = bundle.manifest
    record = manifest["partitions"][0]
    path = bundle.path / record["file"]
    if damage == "corrupt":
        path.write_bytes(b"broken")
    elif damage == "missing":
        path.unlink()
    elif damage == "extra":
        (bundle.path / "extra.parquet").write_bytes(b"extra")
    else:
        if damage == "overlap":
            manifest["partitions"][1]["date_start"] = record["date_end"]
        else:
            frame = pd.read_parquet(path)
            if damage == "duplicate_keys":
                frame.iloc[1] = frame.iloc[0]
            else:
                frame = frame.drop(columns="REV1")
            frame.to_parquet(path, index=False)
            from factorlab.dataset.partitioned import _record
            manifest["partitions"][0] = _record(path, frame)
        manifest = seal({k: v for k, v in manifest.items() if k != "identity_hash"})
        (bundle.path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SchemaError):
        PartitionedDataset(bundle.path).verify()


@pytest.mark.parametrize("kind", ["memory", "disk", "timeout", "duplicate", "outside", "factor_failure"])
def test_failure_never_publishes(inputs, tmp_path, kind, monkeypatch):
    dates, *frames = inputs
    cfg = ChunkedConfig()
    if kind == "memory":
        cfg = replace(cfg, memory_limit_bytes=1)
    elif kind == "disk":
        cfg = replace(cfg, disk_limit_bytes=1)
    elif kind == "timeout":
        cfg = replace(cfg, timeout_seconds=1e-9)
    elif kind == "duplicate":
        frames[0] = pd.concat([frames[0], frames[0].iloc[:1]])
    elif kind == "outside":
        frames[0] = frames[0].copy()
        frames[0].loc[0, "date"] = dates[-1] + pd.Timedelta(days=10)
    else:
        def broken(*args, **kwargs):
            raise RuntimeError("injected factor failure")
        monkeypatch.setattr("factorlab.dataset.partitioned._factor_batches", broken)
    with pytest.raises((ResourceLimitError, SchemaError, RuntimeError)):
        builder().build_chunked(*frames, dates, output_dir=tmp_path / "failed", config=cfg)
    assert not (tmp_path / "failed").exists()
    assert not list(tmp_path.glob("failed.pending-*"))


def test_pins_calendar_overwrite_and_original_limits(inputs, tmp_path):
    dates, *frames = inputs
    obj = builder()
    bundle = obj.build_chunked(*frames, dates, output_dir=tmp_path / "bundle")
    with pytest.raises(FileExistsError):
        obj.build_chunked(*frames, dates, output_dir=bundle.path)
    with pytest.raises(SchemaError, match="identity"):
        obj.read_partitioned(bundle.path, calendar=dates, expected_identity_hash="0" * 64)
    with pytest.raises(SchemaError, match="binding"):
        obj.read_partitioned(bundle.path, calendar=dates[:-1])
    with pytest.raises(SchemaError, match="input"):
        obj.read_partitioned(bundle.path, calendar=dates, expected_input_hashes={})
    large = pd.concat([frames[0]] * 300, ignore_index=True)
    with pytest.raises(SchemaError, match="250000"):
        obj.snapshot.compute(large, obj.factor_refs, calendar=dates)


def test_large_definition_workload_is_real_and_bounded():
    snapshot, refs = registry(300)
    assert len(snapshot.resolve(refs)) == len({r.definition_hash for r in refs}) == 300
    assert sum(d.kind == "expression" for d in snapshot.definitions) == 286
    with pytest.raises(ConfigurationError):
        ChunkedConfig(workers=5)


def test_all_300_definitions_execute_with_deterministic_batches(tmp_path):
    dates, bars, *_ = synthetic_inputs(100, 3)
    snapshot, refs = registry(300)
    first = snapshot.compute_chunked(bars, refs, calendar=dates, output_dir=tmp_path / "first",
        config=ChunkedConfig(symbols_per_batch=1, factors_per_batch=7, dates_per_batch=13))
    second = snapshot.compute_chunked(bars.sample(frac=1, random_state=2), refs,
        calendar=dates, output_dir=tmp_path / "second",
        config=ChunkedConfig(symbols_per_batch=3, factors_per_batch=16, dates_per_batch=20))
    pd.testing.assert_frame_equal(first.to_frame(), second.to_frame(), check_exact=True)
    assert first.manifest["counts"]["unique_executed_definitions"] == 300
    # Original interpreter remains the oracle, evaluated in its existing budget.
    actual = first.to_frame()
    for start in range(0, 300, 16):
        batch = refs[start:start + 16]
        expected = snapshot.compute(bars, batch, calendar=dates)
        pd.testing.assert_frame_equal(actual[["date", "symbol"] + [r.factor_id for r in batch]],
                                      expected, check_exact=True)
