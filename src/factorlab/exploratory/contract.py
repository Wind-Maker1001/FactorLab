"""Strict local input contract; never issues formal financial qualification."""

import hashlib
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd
from pyarrow import ArrowException

from factorlab.data.schema import (
    CANONICAL_BAR_COLUMNS,
    normalize_symbol,
    validate_bars,
)
from factorlab.errors import SchemaError


REQUIRED_MARKERS = (
    "EXPLORATORY",
    "UNVERIFIED",
    "HISTORICALLY_RECONSTRUCTED",
    "NO LIVE MONEY",
    "NOT FORMAL CSI500 EVIDENCE",
)


class ExploratoryContractError(ValueError):
    """Missing, inconsistent, or inappropriate exploratory evidence."""


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def manifest_version(manifest):
    payload = {key: value for key, value in manifest.items() if key != "data_version"}
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _fail(message):
    raise ExploratoryContractError(message)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        _fail("explicit %s required" % name)
    return value


def _day(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _fail("%s must be an ISO trading date" % name)
    try:
        return pd.Timestamp(value)
    except ValueError as exc:
        raise ExploratoryContractError("invalid %s" % name) from exc


def _instant(value, name):
    _text(value, name)
    try:
        result = pd.Timestamp(value)
    except (ValueError, TypeError) as exc:
        raise ExploratoryContractError("invalid %s" % name) from exc
    if pd.isna(result) or result.tzinfo is None:
        _fail("%s must be timezone-aware" % name)
    return result


def assert_exploratory_only(metadata, purpose="EXPLORATORY"):
    if not isinstance(metadata, dict):
        _fail("exploratory metadata must be an object")
    if purpose != "EXPLORATORY":
        _fail("exploratory artifacts cannot be FORMAL, FORWARD, or LIVE inputs")
    if metadata.get("purpose") != "EXPLORATORY":
        _fail("explicit EXPLORATORY purpose required")
    if metadata.get("formal_admission") is not False:
        _fail(
            "exploratory/historically reconstructed inputs cannot acquire formal admission"
        )
    if metadata.get("qualification") != "UNVERIFIED":
        _fail("exploratory inputs cannot upgrade financial qualification")


def validate_manifest(manifest, allow_synthetic=False):
    assert_exploratory_only(manifest)
    if manifest.get("schema") != "EXPLORATORY_DATASET_V1":
        _fail("unsupported exploratory dataset schema")
    if manifest.get("historical_status") != "HISTORICALLY_RECONSTRUCTED":
        _fail("historically reconstructed source must be disclosed")
    if type(manifest.get("synthetic_only")) is not bool:
        _fail("explicit boolean synthetic boundary required")
    if manifest["synthetic_only"] and not allow_synthetic:
        _fail("SYNTHETIC_TEST_ONLY cannot supply a real empirical report")
    if manifest.get("units") != {
        "price": "CNY",
        "volume": "SHARES",
        "amount": "CNY",
        "turnover": "RATIO",
    }:
        _fail("units must explicitly be CNY / SHARES / CNY / RATIO")
    if manifest.get("price_basis") != {"canonical": "RAW", "raw": "RAW", "adj": "HFQ"}:
        _fail("RAW/HFQ price fields must have separate declared roles")
    if manifest.get("benchmark_basis") != "CSI500_PRICE_INDEX_UNADJUSTED":
        _fail(
            "benchmark must be declared separately as an unadjusted CSI500 price index"
        )
    start = _day(manifest.get("research_start"), "research_start")
    end = _day(manifest.get("research_end"), "research_end")
    if start > end:
        _fail("research dates are reversed")
    warmup = manifest.get("warmup_min_sessions")
    if type(warmup) is not int or warmup < 130:
        _fail("at least 130 actual trading sessions of warmup required")
    _text(manifest.get("vendor"), "single vendor")
    version = _text(manifest.get("source_version"), "source_version")
    _instant(manifest.get("retrieved_at"), "retrieved_at")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {
        "bars",
        "benchmark",
        "calendar",
        "sample",
    }:
        _fail("bars, benchmark, actual calendar, and frozen sample files required")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        _fail("inspectable source artifacts required")
    for item in list(files.values()) + sources:
        if not isinstance(item, dict) or item.get("source_version") != version:
            _fail("data source version mismatch; mixed histories are not allowed")
        _text(item.get("path"), "artifact path")
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))):
            _fail("artifact SHA256 required")
    if manifest.get("data_version") != manifest_version(manifest):
        _fail("dataset manifest semantic hash mismatch")


def _dates(values, name):
    try:
        dates = pd.DatetimeIndex(pd.to_datetime(values, errors="raise"))
    except (TypeError, ValueError) as exc:
        raise ExploratoryContractError("invalid %s dates" % name) from exc
    if dates.hasnans or dates.tz is not None or not dates.equals(dates.normalize()):
        _fail("%s dates must be nonmissing timezone-naive midnight" % name)
    return dates


def validate_dataset_frames(bars, benchmark, calendar, manifest, sample):
    """Validate observed fields without filling gaps or certifying HFQ math."""
    sessions = _dates(calendar, "calendar")
    if (
        sessions.empty
        or sessions.has_duplicates
        or not sessions.is_monotonic_increasing
    ):
        _fail("actual calendar must be unique, sorted, and nonempty")
    if not isinstance(sample, dict) or sample.get("pit") is not False:
        _fail("non-PIT convenience sample cannot assert PIT membership")
    if sample.get("classification") != "NON_PIT_CONVENIENCE":
        _fail("NON_PIT_CONVENIENCE survivorship bias disclosure required")
    _text(sample.get("selection_method"), "selection_method")
    selection = _day(sample.get("selection_date"), "selection_date")
    frozen = _instant(sample.get("frozen_at"), "frozen_at")
    retrieved = _instant(manifest.get("retrieved_at"), "retrieved_at")
    if (
        sessions > retrieved.tz_convert("Asia/Shanghai").normalize().tz_localize(None)
    ).any():
        _fail("actual calendar contains sessions after acquisition")
    if frozen > retrieved or selection > frozen.tz_convert(
        "Asia/Shanghai"
    ).normalize().tz_localize(None):
        _fail("sample must be frozen before acquisition")
    symbols = sample.get("symbols")
    if (
        not isinstance(symbols, list)
        or not symbols
        or len(symbols) != len(set(symbols))
    ):
        _fail("unique pre-frozen symbols required")
    try:
        if any(normalize_symbol(value) != value for value in symbols):
            _fail("sample symbols must be canonical")
    except SchemaError as exc:
        raise ExploratoryContractError("sample symbols must be canonical") from exc
    start = _day(manifest.get("research_start"), "research_start")
    end = _day(manifest.get("research_end"), "research_end")
    if sum(sessions < start) < manifest["warmup_min_sessions"]:
        _fail("insufficient actual trading-session warmup")
    if not ((sessions >= start) & (sessions <= end)).any():
        _fail("no actual research sessions")
    for frame, name in ((bars, "stock"), (benchmark, "benchmark")):
        if not set(CANONICAL_BAR_COLUMNS).issubset(frame.columns):
            _fail(
                "%s requires explicit RAW and HFQ OHLC; fields cannot be inferred"
                % name
            )
    if bars.empty or not set(bars["symbol"]).issubset(symbols):
        _fail("stock history absent or outside the pre-frozen sample")
    for frame, name in ((bars, "stock"), (benchmark, "benchmark")):
        _dates(frame["date"], name)
        try:
            validate_bars(frame, allow_missing_ohlc=True)
        except SchemaError as exc:
            raise ExploratoryContractError("%s schema: %s" % (name, exc)) from exc
        if not frame["date"].isin(sessions).all():
            _fail("%s observations outside actual trading calendar" % name)
        if (
            frame["date"]
            .gt(retrieved.tz_convert("Asia/Shanghai").normalize().tz_localize(None))
            .any()
        ):
            _fail("%s prices dated after acquisition" % name)
    if benchmark.empty or set(benchmark["symbol"]) != {"000905.SH"}:
        _fail("real CSI500 benchmark 000905.SH required")
    if benchmark["date"].duplicated().any():
        _fail("duplicate benchmark dates")
    for field in ("open", "high", "low", "close"):
        raw, adj = benchmark["raw_" + field], benchmark["adj_" + field]
        if not raw.isna().eq(adj.isna()).all() or not np.allclose(
            raw.dropna(),
            adj.dropna(),
            rtol=0,
            atol=0,
        ):
            _fail(
                "CSI500 price-index raw and adj aliases must identify the same observed prices"
            )
    if not benchmark["adj_factor"].dropna().eq(1).all():
        _fail("price index must not claim an equity HFQ adjustment algorithm")
    # A raw amount/share-volume VWAP sanity check catches 100-share lots and
    # ten-thousand-yuan amounts erroneously declared as canonical units.
    active = bars["volume"].gt(0) & bars["amount"].notna()
    vwap = bars.loc[active, "amount"] / bars.loc[active, "volume"]
    low = bars.loc[active, "raw_low"]
    high = bars.loc[active, "raw_high"]
    if ((vwap < low * 0.99 - 0.01) | (vwap > high * 1.01 + 0.01)).any():
        _fail("raw amount/share-volume violates raw OHLC units")
    values = bars["volume"].dropna().to_numpy(dtype=float)
    if not np.isclose(values, np.round(values), rtol=0, atol=1e-6).all():
        _fail("volume must be integral shares")
    # Ratios above 10 are not silently treated as percentages. Ordinary large
    # turnover is retained for diagnostics, rather than clipped at 100%.
    if bars["turnover"].dropna().gt(10).any():
        _fail("turnover ratio implausible; inspect percentage/ratio units")


def _artifact(root, item):
    path = Path(item["path"])
    if path.is_absolute() or ".." in path.parts:
        _fail("artifact paths must remain within the private dataset")
    resolved = (root / path).resolve()
    if root != resolved and root not in resolved.parents:
        _fail("artifact escapes dataset directory")
    if not resolved.is_file() or sha256_file(resolved) != item["sha256"]:
        _fail("missing artifact or source/product hash mismatch: %s" % path.name)
    return resolved


def _decode_input(path, role, reader):
    """A valid byte hash does not guarantee a decodable market-data file."""
    try:
        return reader(path)
    except (OSError, ValueError, TypeError, ArrowException) as exc:
        raise ExploratoryContractError(
            "cannot decode %s input (%s)" % (role, type(exc).__name__)
        ) from exc


def _read_json(path):
    def reject_nonfinite(value):
        raise ValueError("nonfinite JSON constant: %s" % value)

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_nonfinite)


def load_dataset(manifest_path, allow_synthetic=False):
    path = Path(manifest_path).resolve()
    manifest = _decode_input(path, "manifest", _read_json)
    validate_manifest(manifest, allow_synthetic=allow_synthetic)
    try:
        paths = {
            key: _artifact(path.parent, item) for key, item in manifest["files"].items()
        }
        for item in manifest["sources"]:
            _artifact(path.parent, item)
    except OSError as exc:
        raise ExploratoryContractError("cannot read hashed dataset artifacts") from exc
    bars = _decode_input(paths["bars"], "bars Parquet", pd.read_parquet)
    benchmark = _decode_input(paths["benchmark"], "benchmark Parquet", pd.read_parquet)
    calendar_frame = _decode_input(paths["calendar"], "calendar CSV", pd.read_csv)
    if "date" not in calendar_frame.columns:
        _fail("calendar missing required date column")
    calendar = _dates(calendar_frame["date"], "calendar")
    sample = _decode_input(paths["sample"], "frozen sample JSON", _read_json)
    try:
        validate_dataset_frames(bars, benchmark, calendar, manifest, sample)
    except ExploratoryContractError:
        raise
    except (SchemaError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ExploratoryContractError(
            "invalid structured dataset input (%s)" % type(exc).__name__
        ) from exc
    # These attrs aid inspection only. Existing formal admission continues to
    # require its own replayable qualification and PIT evidence.
    for frame in (bars, benchmark):
        frame.attrs.update(
            purpose="EXPLORATORY",
            formal_admission=False,
            qualification="UNVERIFIED",
            data_version=manifest["data_version"],
        )
    manifest = dict(manifest, sample=sample)
    return bars, benchmark, calendar, manifest
