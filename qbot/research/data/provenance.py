"""Strict receipts for the verified market-bar snapshots used by a dataset.

Receipts bind the *entire* per-symbol cache, not only a selected date window.
Consequently an append outside the research window conservatively requires a
dataset rebuild. Storage generations and metadata timestamps are evidence, not
semantic data identity: republishing identical bytes does not change the version.
Only pass snapshots returned by ``load_snapshot(verify_hash=True)``; a receipt
cannot independently re-hash Parquet bytes after they have been decoded.
"""

import hashlib
import json
import re
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

import pandas as pd

from qbot.research.data.cache import BarCacheSnapshot
from qbot.research.data.schema import normalize_symbol
from qbot.research.errors import DataQualityError, SchemaError


Identity = Tuple[str, str]
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_RE = re.compile(r"^[0-9a-f]{32}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COMMON_FIELDS = {"role", "symbol", "state"}
_MISSING_FIELDS = _COMMON_FIELDS | {"expected_source", "expected_source_version"}
_PRESENT_FIELDS = _COMMON_FIELDS | {
    "metadata_version", "generation_id", "data_file", "metadata_sha256",
    "source", "source_version", "content_sha256", "row_count",
    "data_start", "data_end", "covered_ranges",
}


def _fail(detail: str) -> None:
    raise DataQualityError("bar data provenance: %s" % detail)


def _exact_fields(value: Any, fields: set, context: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        _fail("%s has missing or unknown fields" % context)


def _symbol(value: Any) -> str:
    if not isinstance(value, str):
        _fail("symbol must be a canonical string")
    try:
        canonical = normalize_symbol(value)
    except SchemaError as exc:
        raise DataQualityError("bar data provenance: invalid symbol") from exc
    if canonical != value:
        _fail("symbol is not canonical: %r" % value)
    return canonical


def _identity(value: Any, field: str) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or len(value) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        _fail("invalid %s" % field)
    return value


def _identity_pair(value: Any) -> Identity:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        _fail("expected provider identity must contain source and source_version")
    return (_identity(value[0], "source"), _identity(value[1], "source_version"))


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        _fail("invalid %s" % field)
    return value


def _date(value: Any, field: str) -> pd.Timestamp:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        _fail("invalid %s; an ISO trading date is required" % field)
    try:
        return pd.Timestamp(value).as_unit("ns")
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataQualityError("bar data provenance: invalid %s" % field) from exc


def _ranges(value: Any) -> list:
    if not isinstance(value, list):
        _fail("covered_ranges must be a list")
    ranges = []
    for item in value:
        _exact_fields(item, {"start", "end"}, "covered_ranges entry")
        start, end = _date(item["start"], "coverage start"), _date(item["end"], "coverage end")
        if start > end:
            _fail("coverage start is after coverage end")
        ranges.append((start, end))
    merged = []
    for start, end in sorted(ranges):
        if not merged or (start.date() - merged[-1][1].date()).days > 1:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
    return [{"start": start.date().isoformat(), "end": end.date().isoformat()}
            for start, end in merged]


def _receipt(value: Any, role: str, symbol: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        _fail("receipt must be an object")
    state = value.get("state")
    if not isinstance(state, str) or state not in {"present", "missing"}:
        _fail("receipt state must be present or missing")
    _exact_fields(value, _PRESENT_FIELDS if state == "present" else _MISSING_FIELDS, "receipt")
    if value["role"] != role or value["symbol"] != symbol:
        _fail("receipt role or symbol does not match its slot")
    _symbol(value["symbol"])
    result = dict(value)
    if state == "missing":
        if role == "benchmark":
            _fail("benchmark snapshot is missing")
        _identity(result["expected_source"], "expected_source")
        _identity(result["expected_source_version"], "expected_source_version")
        return result
    _identity(result["source"], "source")
    _identity(result["source_version"], "source_version")
    _hash(result["content_sha256"], "content_sha256")
    _hash(result["metadata_sha256"], "metadata_sha256")
    version = result["metadata_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version not in {2, 3}:
        _fail("unsupported receipt metadata_version")
    generation = result["generation_id"]
    if version == 3:
        if not isinstance(generation, str) or not _GENERATION_RE.fullmatch(generation):
            _fail("invalid generation_id")
        expected_file = "%s.%s.parquet" % (symbol, generation)
    else:
        if generation is not None:
            _fail("legacy receipt must not claim a generation_id")
        expected_file = "%s.parquet" % symbol
    if result["data_file"] != expected_file:
        _fail("data_file must be the exact safe basename for its symbol and generation")
    count = result["row_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        _fail("invalid row_count")
    result["covered_ranges"] = _ranges(result["covered_ranges"])
    if count == 0:
        if result["data_start"] is not None or result["data_end"] is not None:
            _fail("empty cache must have null data bounds")
    else:
        start = _date(result["data_start"], "data_start")
        end = _date(result["data_end"], "data_end")
        if start > end:
            _fail("data_start is after data_end")
        for bound in (start, end):
            if not any(_date(item["start"], "coverage start") <= bound <=
                       _date(item["end"], "coverage end") for item in result["covered_ranges"]):
                _fail("data bounds are outside request coverage")
    return result


def validate_bar_data_provenance(
    provenance: Any,
    benchmark_symbol: Optional[str] = None,
    stock_symbols: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Return a fresh normalized receipt bundle, rejecting incomplete evidence.

    Supply the verified PIT union as ``stock_symbols`` at trust boundaries.
    Missing stocks must still occupy their slots; silent omission is invalid.
    """

    _exact_fields(provenance, {"schema_version", "benchmark", "stocks"}, "bundle")
    version = provenance["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        _fail("unsupported bundle schema_version")
    benchmark = provenance["benchmark"]
    if not isinstance(benchmark, dict):
        _fail("benchmark receipt must be an object")
    actual_symbol = _symbol(benchmark.get("symbol"))
    if benchmark_symbol is not None and _symbol(benchmark_symbol) != actual_symbol:
        _fail("benchmark symbol mismatch")
    stocks = provenance["stocks"]
    if not isinstance(stocks, dict):
        _fail("stocks must be an object keyed by canonical symbol")
    actual_stocks = {_symbol(symbol) for symbol in stocks}
    if actual_symbol in actual_stocks:
        _fail("benchmark cannot occupy a stock slot")
    if stock_symbols is not None:
        expected = [_symbol(symbol) for symbol in stock_symbols]
        if len(set(expected)) != len(expected):
            _fail("duplicate expected stock symbols")
        if set(expected) != actual_stocks:
            _fail("stock receipt coverage does not match the PIT union")
    return {
        "schema_version": 1,
        "benchmark": _receipt(benchmark, "benchmark", actual_symbol),
        "stocks": {symbol: _receipt(stocks[symbol], "stock", symbol) for symbol in sorted(stocks)},
    }


def _from_snapshot(
    role: str, symbol: str, snapshot: BarCacheSnapshot, expected_identity: Identity,
) -> Dict[str, Any]:
    if not isinstance(snapshot, BarCacheSnapshot):
        _fail("a verified BarCacheSnapshot is required")
    if not isinstance(snapshot.metadata, dict) or not isinstance(snapshot.frame, pd.DataFrame):
        _fail("invalid cache snapshot")
    if not snapshot.metadata:
        if not snapshot.frame.empty or snapshot.metadata_sha256 is not None:
            _fail("absent cache has inconsistent snapshot evidence")
        result = {
            "role": role, "symbol": symbol, "state": "missing",
            "expected_source": expected_identity[0], "expected_source_version": expected_identity[1],
        }
        return _receipt(result, role, symbol)
    metadata = snapshot.metadata
    fields = _PRESENT_FIELDS - _COMMON_FIELDS - {"generation_id", "data_file", "metadata_sha256"}
    if metadata.get("symbol") != symbol or not fields.issubset(metadata):
        _fail("snapshot metadata is incomplete or has the wrong symbol")
    if (metadata["source"], metadata["source_version"]) != expected_identity:
        _fail("snapshot provider identity differs from the expected contract")
    result = {field: metadata[field] for field in fields}
    result.update({
        "role": role, "symbol": symbol, "state": "present",
        "generation_id": metadata.get("generation_id"),
        "data_file": metadata.get("data_file", "%s.parquet" % symbol),
        "metadata_sha256": snapshot.metadata_sha256,
    })
    result = _receipt(result, role, symbol)
    if len(snapshot.frame) != result["row_count"]:
        _fail("snapshot rows differ from metadata")
    if not snapshot.frame.empty:
        if not {"date", "symbol"}.issubset(snapshot.frame):
            _fail("snapshot rows lack date or symbol")
        if set(snapshot.frame["symbol"]) != {symbol}:
            _fail("snapshot contains another symbol")
        try:
            actual_bounds = tuple(snapshot.frame["date"].agg(["min", "max"]).dt.strftime("%Y-%m-%d"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise DataQualityError("bar data provenance: invalid snapshot dates") from exc
        if actual_bounds != (result["data_start"], result["data_end"]):
            _fail("snapshot dates differ from metadata")
    return result


def build_bar_data_provenance(
    benchmark_symbol: str,
    benchmark_snapshot: BarCacheSnapshot,
    stock_snapshots: Mapping[str, BarCacheSnapshot],
    stock_identities: Mapping[str, Identity],
    benchmark_identity: Identity,
) -> Dict[str, Any]:
    """Build evidence from the same snapshots whose frames feed the pipeline."""

    benchmark_symbol = _symbol(benchmark_symbol)
    if not isinstance(stock_snapshots, Mapping) or not isinstance(stock_identities, Mapping):
        _fail("stock snapshots and identities must be symbol-keyed mappings")
    if set(stock_snapshots) != set(stock_identities):
        _fail("stock snapshot and provider-identity slots differ")
    stocks = {}
    for symbol in sorted(_symbol(item) for item in stock_snapshots):
        stocks[symbol] = _from_snapshot("stock", symbol, stock_snapshots[symbol],
                                      _identity_pair(stock_identities[symbol]))
    return validate_bar_data_provenance({
        "schema_version": 1,
        "benchmark": _from_snapshot("benchmark", benchmark_symbol, benchmark_snapshot,
                                    _identity_pair(benchmark_identity)),
        "stocks": stocks,
    }, benchmark_symbol=benchmark_symbol, stock_symbols=stock_snapshots)


def bar_data_version(provenance: Any) -> str:
    """Hash canonical data semantics, excluding storage and metadata locators."""

    bundle = validate_bar_data_provenance(provenance)
    def semantic(receipt: Dict[str, Any]) -> Dict[str, Any]:
        if receipt["state"] == "missing":
            return {"role": receipt["role"], "symbol": receipt["symbol"], "state": "missing",
                    "source": receipt["expected_source"],
                    "source_version": receipt["expected_source_version"]}
        return {field: receipt[field] for field in (
            "role", "symbol", "state", "source", "source_version", "content_sha256",
            "row_count", "data_start", "data_end", "covered_ranges",
        )}
    payload = {
        "schema_version": 1,
        "benchmark": semantic(bundle["benchmark"]),
        "stocks": {symbol: semantic(receipt) for symbol, receipt in bundle["stocks"].items()},
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True,
                         separators=(",", ":"), allow_nan=False).encode("ascii")
    return hashlib.sha256(b"factorlab-bar-data-v1\0" + encoded).hexdigest()


__all__ = ["build_bar_data_provenance", "validate_bar_data_provenance", "bar_data_version"]
