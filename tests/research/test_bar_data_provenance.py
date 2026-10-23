"""Snapshot-bound dataset provenance; no provider or network is required."""

from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

from factorlab.data.cache import BarCacheSnapshot, ParquetBarCache
from factorlab.data.provenance import (
    bar_data_version, build_bar_data_provenance, validate_bar_data_provenance,
)
from factorlab.data.schema import canonicalize_bars, empty_bars
from factorlab.errors import DataQualityError


BENCHMARK = "000905.SH"
STOCK = "600000.SH"
MISSING = "000001.SZ"
IDENTITY = ("fixture", "akshare=fixture;contract=raw-hfq-v1")


def _snapshot(symbol=STOCK, generation="a" * 32, metadata_version=3):
    frame = canonicalize_bars(pd.DataFrame({
        "date": ["2024-01-02", "2024-01-03"], "symbol": symbol,
        "open": [10., 11.], "high": [11., 12.], "low": [9., 10.],
        "close": [10., 11.], "volume": 1000., "amount": 10000., "turnover": .01,
    }))
    metadata = {
        "metadata_version": metadata_version, "symbol": symbol,
        "source": IDENTITY[0], "source_version": IDENTITY[1],
        "content_sha256": "1" * 64, "row_count": 2,
        "data_start": "2024-01-02", "data_end": "2024-01-03",
        "covered_ranges": [{"start": "2024-01-01", "end": "2024-01-05"}],
        "updated_at": "2024-01-10T00:00:00Z",
    }
    if metadata_version == 3:
        metadata.update(generation_id=generation, data_file="%s.%s.parquet" % (symbol, generation))
    return BarCacheSnapshot(frame, metadata, "2" * 64)


def _bundle():
    return build_bar_data_provenance(
        BENCHMARK, _snapshot(BENCHMARK),
        {STOCK: _snapshot(), MISSING: BarCacheSnapshot(empty_bars(), {}, None)},
        {STOCK: IDENTITY, MISSING: IDENTITY}, IDENTITY,
    )


def test_complete_bundle_includes_absent_pit_stocks_and_snapshot_locators():
    bundle = _bundle()
    assert bundle["schema_version"] == 1
    assert bundle["benchmark"]["role"] == "benchmark"
    assert bundle["stocks"][STOCK]["metadata_sha256"] == "2" * 64
    assert bundle["stocks"][STOCK]["generation_id"] == "a" * 32
    assert bundle["stocks"][MISSING] == {
        "role": "stock", "symbol": MISSING, "state": "missing",
        "expected_source": IDENTITY[0], "expected_source_version": IDENTITY[1],
    }
    assert len(bar_data_version(bundle)) == 64
    assert list(bundle["stocks"]) == sorted([STOCK, MISSING])


def test_version_uses_documented_domain_and_canonical_semantics():
    bundle = _bundle()
    payload = deepcopy(bundle)
    for receipt in [payload["benchmark"], *payload["stocks"].values()]:
        if receipt["state"] == "present":
            for field in ["metadata_version", "metadata_sha256", "generation_id", "data_file"]:
                del receipt[field]
        else:
            receipt["source"] = receipt.pop("expected_source")
            receipt["source_version"] = receipt.pop("expected_source_version")
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True,
                         separators=(",", ":"), allow_nan=False).encode("ascii")
    assert bar_data_version(bundle) == hashlib.sha256(b"factorlab-bar-data-v1\0" + encoded).hexdigest()


def test_generation_metadata_locator_and_legacy_migration_do_not_change_data_version():
    first = _bundle()
    changed = deepcopy(first)
    for receipt in [changed["benchmark"], changed["stocks"][STOCK]]:
        receipt["generation_id"] = "b" * 32
        receipt["data_file"] = "%s.%s.parquet" % (receipt["symbol"], "b" * 32)
        receipt["metadata_sha256"] = "3" * 64
    assert changed != first
    assert bar_data_version(changed) == bar_data_version(first)
    for receipt in [changed["benchmark"], changed["stocks"][STOCK]]:
        receipt["metadata_version"] = 2
        receipt["generation_id"] = None
        receipt["data_file"] = "%s.parquet" % receipt["symbol"]
    assert bar_data_version(changed) == bar_data_version(first)


def test_sorted_merged_coverage_and_stock_order_have_same_semantics_without_mutation():
    first = _bundle()
    changed = deepcopy(first)
    changed["stocks"] = dict(reversed(list(changed["stocks"].items())))
    changed["stocks"][STOCK]["covered_ranges"] = [
        {"start": "2024-01-03", "end": "2024-01-05"},
        {"start": "2024-01-01", "end": "2024-01-02"},
        {"start": "2024-01-02", "end": "2024-01-04"},
    ]
    original = deepcopy(changed)
    normalized = validate_bar_data_provenance(changed, BENCHMARK, [MISSING, STOCK])
    assert normalized == first
    assert bar_data_version(changed) == bar_data_version(first)
    assert changed == original


@pytest.mark.parametrize("field,value", [
    ("source", "other-provider"), ("source_version", "different-contract"),
    ("content_sha256", "f" * 64), ("row_count", 3),
    ("data_start", "2024-01-01"), ("data_end", "2024-01-04"),
    ("covered_ranges", [{"start": "2024-01-01", "end": "2024-01-06"}]),
])
def test_changed_semantic_evidence_invalidates_version(field, value):
    first = _bundle()
    changed = deepcopy(first)
    changed["stocks"][STOCK][field] = value
    assert bar_data_version(changed) != bar_data_version(first)


def test_filling_previously_missing_cache_changes_version_even_with_no_bars():
    first = _bundle()
    snapshot = _snapshot(MISSING)
    snapshot.frame.drop(snapshot.frame.index, inplace=True)
    snapshot.metadata.update(row_count=0, data_start=None, data_end=None)
    changed = build_bar_data_provenance(
        BENCHMARK, _snapshot(BENCHMARK), {STOCK: _snapshot(), MISSING: snapshot},
        {STOCK: IDENTITY, MISSING: IDENTITY}, IDENTITY,
    )
    assert changed["stocks"][MISSING]["state"] == "present"
    assert bar_data_version(changed) != bar_data_version(first)


@pytest.mark.parametrize("field,value", [
    ("state", []), ("state", "unknown"), ("role", "benchmark"),
    ("symbol", MISSING), ("metadata_version", True), ("metadata_version", 1),
    ("metadata_version", 3.), ("generation_id", "../escape"),
    ("data_file", "../outside.parquet"), ("data_file", "C:/absolute.parquet"),
    ("data_file", "%s.%s.parquet" % (MISSING, "a" * 32)),
    ("metadata_sha256", "x" * 64), ("content_sha256", "a" * 63),
    ("source", " "), ("source_version", "contract\nunsafe"),
    ("row_count", True), ("row_count", -1), ("data_start", "2024-02-30"),
    ("data_start", "0001-01-01"), ("data_end", "9999-12-31"),
    ("data_end", None), ("data_start", "2024-01-06"),
    ("covered_ranges", []), ("covered_ranges", {}),
    ("covered_ranges", [{"start": "2024-01-03", "end": "2024-01-01"}]),
    ("covered_ranges", [{"start": "2024-01-01", "end": "2024-01-05", "extra": True}]),
])
def test_invalid_receipt_fails_strictly(field, value):
    bundle = _bundle()
    bundle["stocks"][STOCK][field] = value
    with pytest.raises(DataQualityError, match="bar data provenance"):
        bar_data_version(bundle)


@pytest.mark.parametrize("slot", ["bundle", "receipt", "missing"])
def test_unknown_fields_are_rejected(slot):
    bundle = _bundle()
    target = bundle if slot == "bundle" else bundle["stocks"][STOCK if slot == "receipt" else MISSING]
    target["future_ignored_field"] = 1
    with pytest.raises(DataQualityError, match="unknown fields"):
        validate_bar_data_provenance(bundle)


@pytest.mark.parametrize("symbols", [[STOCK], [STOCK, MISSING, "600001.SH"], [STOCK, STOCK, MISSING]])
def test_consumer_must_match_entire_pit_union_without_duplicates(symbols):
    with pytest.raises(DataQualityError):
        validate_bar_data_provenance(_bundle(), BENCHMARK, symbols)


def test_wrong_benchmark_and_noncanonical_keys_rejected():
    with pytest.raises(DataQualityError, match="benchmark symbol mismatch"):
        validate_bar_data_provenance(_bundle(), "000300.SH")
    bundle = _bundle()
    bundle["stocks"]["sh600000"] = bundle["stocks"].pop(STOCK)
    with pytest.raises(DataQualityError, match="not canonical"):
        validate_bar_data_provenance(bundle)


def test_missing_benchmark_cannot_be_recorded_as_valid_provenance():
    with pytest.raises(DataQualityError, match="benchmark snapshot is missing"):
        build_bar_data_provenance(BENCHMARK, BarCacheSnapshot(empty_bars(), {}, None), {}, {}, IDENTITY)


def test_expected_contract_required_for_every_snapshot():
    with pytest.raises(DataQualityError, match="slots differ"):
        build_bar_data_provenance(BENCHMARK, _snapshot(BENCHMARK), {STOCK: _snapshot()}, {}, IDENTITY)
    with pytest.raises(DataQualityError, match="expected contract"):
        build_bar_data_provenance(BENCHMARK, _snapshot(BENCHMARK), {STOCK: _snapshot()},
                                  {STOCK: ("unrelated", "provider")}, IDENTITY)


@pytest.mark.parametrize("mutation", ["rows", "symbol", "date", "missing_metadata", "missing_frame", "missing_digest"])
def test_snapshot_metadata_and_rows_cannot_disagree(mutation):
    snapshot = _snapshot()
    if mutation == "rows":
        snapshot.frame.drop(0, inplace=True)
    elif mutation == "symbol":
        snapshot.frame.loc[0, "symbol"] = MISSING
    elif mutation == "date":
        snapshot.frame.loc[0, "date"] = pd.Timestamp("2024-01-01")
    elif mutation == "missing_metadata":
        snapshot.metadata.clear()
    elif mutation == "missing_frame":
        snapshot = BarCacheSnapshot(empty_bars(), snapshot.metadata, snapshot.metadata_sha256)
    elif mutation == "missing_digest":
        snapshot = BarCacheSnapshot(snapshot.frame, snapshot.metadata, None)
    with pytest.raises(DataQualityError):
        build_bar_data_provenance(BENCHMARK, _snapshot(BENCHMARK), {STOCK: snapshot}, {STOCK: IDENTITY}, IDENTITY)


def test_receipt_keeps_pinned_generation_after_cache_pointer_changes(tmp_path):
    cache = ParquetBarCache(tmp_path)
    for symbol in [BENCHMARK, STOCK]:
        cache.update(symbol, _snapshot(symbol).frame, "2024-01-01", "2024-01-05",
                     source=IDENTITY[0], source_version=IDENTITY[1])
    benchmark = cache.load_snapshot(BENCHMARK, expected_source=IDENTITY[0], expected_source_version=IDENTITY[1])
    pinned = cache.load_snapshot(STOCK, expected_source=IDENTITY[0], expected_source_version=IDENTITY[1])
    first = build_bar_data_provenance(BENCHMARK, benchmark, {STOCK: pinned}, {STOCK: IDENTITY}, IDENTITY)
    modified = pinned.frame.copy()
    modified.loc[0, ["close", "raw_close", "adj_close"]] = 10.5
    cache.update(STOCK, modified, "2024-01-01", "2024-01-05",
                 source=IDENTITY[0], source_version=IDENTITY[1])
    still_pinned = build_bar_data_provenance(BENCHMARK, benchmark, {STOCK: pinned}, {STOCK: IDENTITY}, IDENTITY)
    current = build_bar_data_provenance(BENCHMARK, benchmark, {STOCK: cache.load_snapshot(STOCK)},
                                        {STOCK: IDENTITY}, IDENTITY)
    assert still_pinned == first
    assert current["stocks"][STOCK]["generation_id"] != first["stocks"][STOCK]["generation_id"]
    assert bar_data_version(current) != bar_data_version(first)
