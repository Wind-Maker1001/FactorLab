"""Validated, point-in-time security status and listing-date artifacts.

The two tables form one integrity-bound bundle.  They are never inferred,
forward/backward filled, or accepted partially because doing so would silently
change the daily eligible universe.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
from numbers import Real
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
import uuid

import numpy as np
import pandas as pd

from factorlab.errors import (
    PointInTimeUniverseError,
    SecurityStatusDataError,
)

from .csi500 import (
    CANONICAL_SYMBOL,
    _atomic_json,
    _atomic_parquet,
    _universe_write_lock,
    dataframe_sha256,
    strict_import_date,
    validate_daily_membership,
)


STATUS_COLUMNS = ("date", "symbol", "trade_status", "is_st")
LISTING_COLUMNS = ("symbol", "listing_date")
STATUS_FILENAME = "security_status.parquet"
LISTING_FILENAME = "listing_dates.parquet"
MANIFEST_FILENAME = "security_data_manifest.json"
SECURITY_DATA_SCHEMA_VERSION = 1
SECURITY_DATA_COVERAGE = "all-point-in-time-membership-keys-v1"
SECURITY_DATA_INDEX_SYMBOL = "000905.SH"
LOCAL_SECURITY_SOURCE_VERSION = "user-security-data-import-v1"
BAOSTOCK_SECURITY_SOURCE_VERSION = "baostock-security-history-v1"
_SOURCE_VERSIONS = {
    "local-import": LOCAL_SECURITY_SOURCE_VERSION,
    "BaoStockPointInTimeProvider": BAOSTOCK_SECURITY_SOURCE_VERSION,
}
_KNOWN_SOURCES = set(_SOURCE_VERSIONS)
_STATUS_GENERATION_FILE = re.compile(r"^security_status\.[0-9a-f]{32}\.parquet$")
_LISTING_GENERATION_FILE = re.compile(r"^listing_dates\.[0-9a-f]{32}\.parquet$")


def _fail(detail: str) -> SecurityStatusDataError:
    return SecurityStatusDataError(detail)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text.lower())


def _strict_date(value: Any, name: str) -> pd.Timestamp:
    try:
        return strict_import_date(value, name)
    except (PointInTimeUniverseError, TypeError, ValueError) as exc:
        raise _fail(str(exc).split(": ", 1)[-1]) from exc


def _calendar(values: Iterable[Any]) -> pd.DatetimeIndex:
    parsed = [_strict_date(value, "calendar date") for value in list(values)]
    dates = pd.DatetimeIndex(parsed)
    if dates.empty:
        raise _fail("benchmark trading calendar is empty")
    if dates.has_duplicates:
        raise _fail("benchmark trading calendar contains duplicates")
    if not dates.is_monotonic_increasing:
        raise _fail("benchmark trading calendar must be strictly increasing")
    return dates


def _canonical_input_symbol(value: Any, name: str = "symbol") -> str:
    if not isinstance(value, str):
        raise _fail("%s must be a canonical string" % name)
    symbol = value.strip()
    if symbol != value or symbol != symbol.upper() or not CANONICAL_SYMBOL.fullmatch(symbol):
        raise _fail("%s is not canonical: %r" % (name, value))
    return symbol


def _binary_value(value: Any, name: str, *, boolean: bool) -> Any:
    if value is None or value is pd.NA or value is pd.NaT or (
        isinstance(value, float) and pd.isna(value)
    ):
        raise _fail("%s must not be null" % name)
    if isinstance(value, (bool, np.bool_)):
        if not boolean:
            raise _fail("%s must be numeric 0 or 1" % name)
        result = int(value)
    elif isinstance(value, Real):
        numeric = float(value)
        if not numeric.is_integer() or numeric not in {0.0, 1.0}:
            raise _fail("%s must be 0 or 1" % name)
        result = int(numeric)
    elif isinstance(value, str):
        text = value.strip().lower()
        allowed = {"0": 0, "1": 1}
        if boolean:
            allowed.update({"false": 0, "true": 1})
        if text not in allowed:
            raise _fail("%s must be 0 or 1" % name)
        result = allowed[text]
    else:
        raise _fail("%s must be 0 or 1" % name)
    return bool(result) if boolean else result


def _validated_membership(membership: pd.DataFrame) -> pd.DataFrame:
    if membership.empty:
        raise _fail("point-in-time membership is empty")
    try:
        clean = validate_daily_membership(membership, expected_member_count=None)
    except (PointInTimeUniverseError, TypeError, ValueError) as exc:
        raise _fail("invalid point-in-time membership: %s" % exc) from exc
    if clean.empty:
        raise _fail("point-in-time membership has no active members")
    return clean


def _calendar_sha256(calendar: pd.DatetimeIndex) -> str:
    return dataframe_sha256(pd.DataFrame({"date": calendar}))


def _symbols_sha256(symbols: Iterable[str]) -> str:
    value = "\n".join(sorted(symbols)) + "\n"
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def validate_security_data(
    status: pd.DataFrame,
    listings: pd.DataFrame,
    *,
    calendar: Iterable[Any],
    membership: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Validate exact schemas and complete date-effective coverage.

    Every point-in-time membership key must have an explicit status row.
    Additional rows are retained when they belong to the same symbol set and
    benchmark calendar.  Execution code separately requires status for each
    order date, so no post-delisting rows need to be fabricated here.
    """

    dates = _calendar(calendar)
    members = _validated_membership(membership)
    calendar_set = set(dates)
    member_dates = set(pd.DatetimeIndex(members["date"].unique()))
    if not member_dates.issubset(calendar_set):
        invalid = sorted(member_dates.difference(calendar_set))
        raise _fail(
            "point-in-time membership contains dates outside the benchmark calendar: %s"
            % [value.strftime("%Y-%m-%d") for value in invalid[:5]]
        )

    if set(status.columns) != set(STATUS_COLUMNS):
        missing = sorted(set(STATUS_COLUMNS).difference(status.columns))
        extra = sorted(set(status.columns).difference(STATUS_COLUMNS))
        raise _fail("security status columns mismatch; missing=%s extra=%s" % (missing, extra))
    if set(listings.columns) != set(LISTING_COLUMNS):
        missing = sorted(set(LISTING_COLUMNS).difference(listings.columns))
        extra = sorted(set(listings.columns).difference(LISTING_COLUMNS))
        raise _fail("listing-date columns mismatch; missing=%s extra=%s" % (missing, extra))
    if status.empty or listings.empty:
        raise _fail("security status and listing-date tables must not be empty")

    clean_listings = listings.loc[:, LISTING_COLUMNS].copy()
    clean_listings["symbol"] = clean_listings["symbol"].map(_canonical_input_symbol)
    clean_listings["listing_date"] = pd.Series(
        [_strict_date(value, "listing_date") for value in clean_listings["listing_date"].tolist()],
        index=clean_listings.index,
        dtype="datetime64[ns]",
    )
    if clean_listings["symbol"].duplicated().any():
        duplicates = clean_listings.loc[
            clean_listings["symbol"].duplicated(keep=False), "symbol"
        ].unique()
        raise _fail("duplicate listing dates for %s" % sorted(duplicates)[:5])

    expected_symbols = set(members["symbol"].unique())
    listing_symbols = set(clean_listings["symbol"])
    if listing_symbols != expected_symbols:
        raise _fail(
            "listing symbol coverage mismatch; missing=%s extra=%s"
            % (
                sorted(expected_symbols.difference(listing_symbols))[:10],
                sorted(listing_symbols.difference(expected_symbols))[:10],
            )
        )
    first_membership = members.groupby("symbol", sort=True)["date"].min()
    listing_by_symbol = clean_listings.set_index("symbol")["listing_date"].reindex(
        first_membership.index
    )
    late = listing_by_symbol[listing_by_symbol > first_membership]
    if not late.empty:
        raise _fail("listing_date is after first PIT membership for %s" % sorted(late.index)[:5])
    in_span = listing_by_symbol.between(dates.min(), dates.max())
    non_session = listing_by_symbol.loc[in_span & ~listing_by_symbol.isin(dates)]
    if not non_session.empty:
        raise _fail(
            "listing_date inside the calendar span must be a benchmark session for %s"
            % sorted(non_session.index)[:5]
        )

    clean_status = status.loc[:, STATUS_COLUMNS].copy()
    clean_status["date"] = pd.Series(
        [_strict_date(value, "date") for value in clean_status["date"].tolist()],
        index=clean_status.index,
        dtype="datetime64[ns]",
    )
    clean_status["symbol"] = clean_status["symbol"].map(_canonical_input_symbol)
    clean_status["trade_status"] = pd.Series(
        [
            _binary_value(value, "trade_status", boolean=False)
            for value in clean_status["trade_status"]
        ],
        index=clean_status.index,
        dtype="int8",
    )
    clean_status["is_st"] = pd.Series(
        [_binary_value(value, "is_st", boolean=True) for value in clean_status["is_st"]],
        index=clean_status.index,
        dtype="bool",
    )
    if clean_status.duplicated(["date", "symbol"]).any():
        examples = clean_status.loc[
            clean_status.duplicated(["date", "symbol"], keep=False), ["date", "symbol"]
        ].head(5)
        raise _fail("duplicate security status keys: %s" % examples.to_dict("records"))
    status_symbols = set(clean_status["symbol"])
    if status_symbols != expected_symbols:
        raise _fail(
            "status symbol coverage mismatch; missing=%s extra=%s"
            % (
                sorted(expected_symbols.difference(status_symbols))[:10],
                sorted(status_symbols.difference(expected_symbols))[:10],
            )
        )
    invalid_dates = sorted(set(clean_status["date"]).difference(calendar_set))
    if invalid_dates:
        raise _fail(
            "security status contains non-calendar dates: %s"
            % [value.strftime("%Y-%m-%d") for value in invalid_dates[:5]]
        )

    status_listing_dates = clean_status["symbol"].map(listing_by_symbol)
    before_listing = clean_status["date"] < status_listing_dates
    if before_listing.any():
        examples = clean_status.loc[
            before_listing, ["date", "symbol"]
        ].head(5)
        raise _fail(
            "security status predates listing_date: %s"
            % examples.to_dict("records")
        )

    expected_keys = members.loc[:, ["date", "symbol"]]
    actual_keys = clean_status.loc[:, ["date", "symbol"]]
    expected_index = pd.MultiIndex.from_frame(expected_keys)
    actual_index = pd.MultiIndex.from_frame(actual_keys)
    missing_keys = expected_index.difference(actual_index)
    if len(missing_keys):
        raise _fail(
            "point-in-time status coverage is incomplete; missing=%s"
            % list(missing_keys[:5])
        )

    clean_status = clean_status.sort_values(
        ["date", "symbol"], kind="mergesort"
    ).reset_index(drop=True)
    clean_listings = clean_listings.sort_values("symbol", kind="mergesort").reset_index(drop=True)
    return clean_status, clean_listings


def _version_payload(
    *,
    source: str,
    source_version: str,
    universe_sha256: str,
    calendar_sha256: str,
    status_sha256: str,
    listings_sha256: str,
    input_sha256: Mapping[str, str],
) -> Dict[str, Any]:
    return {
        "calendar_sha256": calendar_sha256,
        "coverage_semantics": SECURITY_DATA_COVERAGE,
        "input_sha256": dict(sorted(input_sha256.items())),
        "listings_sha256": listings_sha256,
        "source": source,
        "source_version": source_version,
        "status_sha256": status_sha256,
        "universe_sha256": universe_sha256,
    }


def _security_data_version(**values: Any) -> str:
    payload = json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def publish_security_data(
    status: pd.DataFrame,
    listings: pd.DataFrame,
    *,
    output_dir: Path,
    calendar: Iterable[Any],
    membership: pd.DataFrame,
    universe_sha256: str,
    source: str,
    source_version: str,
    input_hashes: Optional[Mapping[str, str]] = None,
    input_files: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Validate and publish a complete bundle, with the manifest written last."""

    if source not in _KNOWN_SOURCES:
        raise _fail("unsupported security-data source %r" % source)
    if source_version != _SOURCE_VERSIONS[source]:
        raise _fail("unsupported security-data source_version for %s" % source)
    if not _is_sha256(universe_sha256):
        raise _fail("universe_sha256 is invalid")
    members = _validated_membership(membership)
    actual_universe_sha256 = dataframe_sha256(members)
    if actual_universe_sha256 != universe_sha256:
        raise _fail("universe_sha256 does not match point-in-time membership")
    dates = _calendar(calendar)
    clean_status, clean_listings = validate_security_data(
        status, listings, calendar=dates, membership=members
    )
    clean_input_hashes = dict(input_hashes or {})
    clean_input_files = dict(input_files or {})
    if source == "local-import" and set(clean_input_hashes) != {"status", "listings"}:
        raise _fail("local import requires status and listings input hashes")
    if source == "local-import" and set(clean_input_files) != {"status", "listings"}:
        raise _fail("local import requires status and listings input paths")
    if source != "local-import" and (clean_input_hashes or clean_input_files):
        raise _fail("provider data must not claim local input files")
    if any(not _is_sha256(value) for value in clean_input_hashes.values()):
        raise _fail("input hashes must be SHA-256 values")
    if any(
        not isinstance(value, str) or not value.strip()
        for value in clean_input_files.values()
    ):
        raise _fail("input paths must be non-empty strings")

    output = Path(output_dir)
    status_alias_path = output / STATUS_FILENAME
    listings_alias_path = output / LISTING_FILENAME
    manifest_path = output / MANIFEST_FILENAME
    status_sha256 = dataframe_sha256(clean_status)
    listings_sha256 = dataframe_sha256(clean_listings)
    calendar_sha256 = _calendar_sha256(dates)
    version_values = _version_payload(
        source=source,
        source_version=str(source_version),
        universe_sha256=universe_sha256,
        calendar_sha256=calendar_sha256,
        status_sha256=status_sha256,
        listings_sha256=listings_sha256,
        input_sha256=clean_input_hashes,
    )
    version = _security_data_version(**version_values)
    generation_id = uuid.uuid4().hex
    status_filename = "security_status.%s.parquet" % generation_id
    listings_filename = "listing_dates.%s.parquet" % generation_id
    status_path = output / status_filename
    listings_path = output / listings_filename

    try:
        with _universe_write_lock(output):
            # Generation files are immutable once referenced.  Canonical files
            # are convenience aliases only; verified consumers follow the
            # manifest so an interrupted refresh leaves the previous
            # generation readable.
            _atomic_parquet(clean_status, status_path)
            _atomic_parquet(clean_listings, listings_path)
            status_file_sha256 = _sha256_bytes(status_path.read_bytes())
            listings_file_sha256 = _sha256_bytes(listings_path.read_bytes())
            _atomic_parquet(clean_status, status_alias_path)
            _atomic_parquet(clean_listings, listings_alias_path)
            manifest: Dict[str, Any] = {
                "schema_version": SECURITY_DATA_SCHEMA_VERSION,
                "source": source,
                "source_version": str(source_version),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "coverage_semantics": SECURITY_DATA_COVERAGE,
                "index_symbol": SECURITY_DATA_INDEX_SYMBOL,
                "start": dates.min().strftime("%Y-%m-%d"),
                "end": dates.max().strftime("%Y-%m-%d"),
                "trading_dates": int(len(dates)),
                "membership_start": members["date"].min().strftime("%Y-%m-%d"),
                "membership_end": members["date"].max().strftime("%Y-%m-%d"),
                "membership_trading_dates": int(members["date"].nunique()),
                "symbols": int(clean_listings["symbol"].nunique()),
                "symbols_sha256": _symbols_sha256(clean_listings["symbol"]),
                "status_rows": int(len(clean_status)),
                "listing_rows": int(len(clean_listings)),
                "universe_sha256": universe_sha256,
                "calendar_sha256": calendar_sha256,
                "status_sha256": status_sha256,
                "listings_sha256": listings_sha256,
                "generation_id": generation_id,
                "status_file": status_filename,
                "listings_file": listings_filename,
                "status_alias": STATUS_FILENAME,
                "listings_alias": LISTING_FILENAME,
                "status_file_sha256": status_file_sha256,
                "listings_file_sha256": listings_file_sha256,
                "input_sha256": clean_input_hashes,
                "input_files": clean_input_files,
                "security_data_version": version,
            }
            _atomic_json(manifest, manifest_path)
    except Exception as exc:
        raise _fail(
            "cannot publish security data: %s: %s"
            % (type(exc).__name__, exc)
        ) from exc
    return manifest


def _read_import(path: Path) -> Tuple[pd.DataFrame, str]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise _fail("cannot read import file %s: %s" % (path, exc)) from exc
    try:
        if path.suffix.lower() == ".csv":
            frame = pd.read_csv(io.BytesIO(content))
        elif path.suffix.lower() in {".parquet", ".pq"}:
            frame = pd.read_parquet(io.BytesIO(content))
        else:
            raise _fail("security-data imports must be CSV or Parquet")
    except SecurityStatusDataError:
        raise
    except Exception as exc:
        raise _fail("cannot parse import file %s: %s" % (path, exc)) from exc
    return frame, _sha256_bytes(content)


def import_security_data(
    status_path: Path,
    listings_path: Path,
    *,
    output_dir: Path,
    calendar: Iterable[Any],
    membership: pd.DataFrame,
    universe_sha256: str,
    source_version: str = LOCAL_SECURITY_SOURCE_VERSION,
) -> Dict[str, Any]:
    """Import an auditable local status/listing bundle from CSV or Parquet."""

    status, status_input_sha256 = _read_import(Path(status_path))
    listings, listings_input_sha256 = _read_import(Path(listings_path))
    return publish_security_data(
        status,
        listings,
        output_dir=output_dir,
        calendar=calendar,
        membership=membership,
        universe_sha256=universe_sha256,
        source="local-import",
        source_version=source_version,
        input_hashes={
            "status": status_input_sha256,
            "listings": listings_input_sha256,
        },
        input_files={
            "status": str(Path(status_path).resolve()),
            "listings": str(Path(listings_path).resolve()),
        },
    )


def load_verified_security_data(
    output_dir: Path,
    *,
    calendar: Iterable[Any],
    membership: pd.DataFrame,
    universe_sha256: str,
    expected_provider: Optional[str] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    """Load one manifest-bound generation and reject any mismatch or tamper."""

    output = Path(output_dir)
    manifest_path = output / MANIFEST_FILENAME
    try:
        manifest_content = manifest_path.read_bytes()
        manifest = json.loads(manifest_content.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise _fail("security-data manifest is missing or unreadable") from exc
    if not isinstance(manifest, dict):
        raise _fail("security-data manifest must be a JSON object")
    if manifest.get("schema_version") != SECURITY_DATA_SCHEMA_VERSION:
        raise _fail("unsupported security-data manifest schema")
    source = manifest.get("source")
    if source not in _KNOWN_SOURCES:
        raise _fail("security-data manifest has an unknown source")
    provider_sources = {
        "local": "local-import",
        "baostock": "BaoStockPointInTimeProvider",
    }
    if expected_provider is not None:
        provider = str(expected_provider).lower()
        if provider not in provider_sources:
            raise _fail("expected_provider must be local or baostock")
        if source != provider_sources[provider]:
            raise _fail("security-data source does not match status_provider=%s" % provider)
    if manifest.get("source_version") != _SOURCE_VERSIONS[source]:
        raise _fail("security-data source_version is unsupported")
    try:
        generated_at = datetime.fromisoformat(str(manifest.get("generated_at", "")))
    except ValueError as exc:
        raise _fail("security-data generated_at is invalid") from exc
    if generated_at.tzinfo is None:
        raise _fail("security-data generated_at must include a timezone")
    if manifest.get("coverage_semantics") != SECURITY_DATA_COVERAGE:
        raise _fail("security-data coverage semantics are unsupported")
    if manifest.get("index_symbol") != SECURITY_DATA_INDEX_SYMBOL:
        raise _fail("security-data index_symbol is unsupported")
    generation_id = manifest.get("generation_id")
    status_filename = manifest.get("status_file")
    listings_filename = manifest.get("listings_file")
    if (
        not isinstance(generation_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", generation_id)
        or not isinstance(status_filename, str)
        or not _STATUS_GENERATION_FILE.fullmatch(status_filename)
        or not isinstance(listings_filename, str)
        or not _LISTING_GENERATION_FILE.fullmatch(listings_filename)
        or status_filename != "security_status.%s.parquet" % generation_id
        or listings_filename != "listing_dates.%s.parquet" % generation_id
        or manifest.get("status_alias") != STATUS_FILENAME
        or manifest.get("listings_alias") != LISTING_FILENAME
    ):
        raise _fail("security-data manifest contains unexpected artifact names")

    dates = _calendar(calendar)
    members = _validated_membership(membership)
    actual_universe_sha256 = dataframe_sha256(members)
    if universe_sha256 != actual_universe_sha256:
        raise _fail("requested universe_sha256 does not match membership")
    expected_common = {
        "start": dates.min().strftime("%Y-%m-%d"),
        "end": dates.max().strftime("%Y-%m-%d"),
        "trading_dates": int(len(dates)),
        "membership_start": members["date"].min().strftime("%Y-%m-%d"),
        "membership_end": members["date"].max().strftime("%Y-%m-%d"),
        "membership_trading_dates": int(members["date"].nunique()),
        "universe_sha256": universe_sha256,
        "calendar_sha256": _calendar_sha256(dates),
    }
    for name, expected in expected_common.items():
        if manifest.get(name) != expected:
            raise _fail("security-data manifest %s does not match current inputs" % name)

    status_path = output / status_filename
    listings_path = output / listings_filename
    try:
        status_content = status_path.read_bytes()
        listings_content = listings_path.read_bytes()
    except OSError as exc:
        raise _fail("security-data Parquet artifacts are missing or unreadable") from exc
    if manifest.get("status_file_sha256") != _sha256_bytes(status_content):
        raise _fail("security status Parquet SHA-256 mismatch")
    if manifest.get("listings_file_sha256") != _sha256_bytes(listings_content):
        raise _fail("listing dates Parquet SHA-256 mismatch")
    try:
        raw_status = pd.read_parquet(io.BytesIO(status_content))
        raw_listings = pd.read_parquet(io.BytesIO(listings_content))
    except Exception as exc:
        raise _fail("security-data Parquet artifacts cannot be parsed") from exc
    clean_status, clean_listings = validate_security_data(
        raw_status, raw_listings, calendar=dates, membership=members
    )

    status_sha256 = dataframe_sha256(clean_status)
    listings_sha256 = dataframe_sha256(clean_listings)
    expected_values = {
        "symbols": int(clean_listings["symbol"].nunique()),
        "symbols_sha256": _symbols_sha256(clean_listings["symbol"]),
        "status_rows": int(len(clean_status)),
        "listing_rows": int(len(clean_listings)),
        "status_sha256": status_sha256,
        "listings_sha256": listings_sha256,
    }
    for name, expected in expected_values.items():
        if manifest.get(name) != expected:
            raise _fail("security-data manifest %s is inconsistent" % name)
    input_hashes = manifest.get("input_sha256")
    if not isinstance(input_hashes, dict) or any(
        not _is_sha256(value) for value in input_hashes.values()
    ):
        raise _fail("security-data input hashes are invalid")
    if source == "local-import" and set(input_hashes) != {"status", "listings"}:
        raise _fail("local security-data input hashes are incomplete")
    input_files = manifest.get("input_files")
    if not isinstance(input_files, dict) or any(
        not isinstance(value, str) or not value.strip()
        for value in input_files.values()
    ):
        raise _fail("security-data input paths are invalid")
    if source == "local-import" and set(input_files) != {"status", "listings"}:
        raise _fail("local security-data input paths are incomplete")
    if source != "local-import" and (input_hashes or input_files):
        raise _fail("provider security data contains unexpected local inputs")

    version_values = _version_payload(
        source=source,
        source_version=str(manifest["source_version"]),
        universe_sha256=universe_sha256,
        calendar_sha256=expected_common["calendar_sha256"],
        status_sha256=status_sha256,
        listings_sha256=listings_sha256,
        input_sha256=input_hashes,
    )
    expected_version = _security_data_version(**version_values)
    if manifest.get("security_data_version") != expected_version:
        raise _fail("security_data_version is inconsistent")
    return clean_status, clean_listings, manifest


__all__ = [
    "BAOSTOCK_SECURITY_SOURCE_VERSION",
    "LISTING_COLUMNS",
    "LISTING_FILENAME",
    "LOCAL_SECURITY_SOURCE_VERSION",
    "MANIFEST_FILENAME",
    "SECURITY_DATA_COVERAGE",
    "SECURITY_DATA_INDEX_SYMBOL",
    "SECURITY_DATA_SCHEMA_VERSION",
    "STATUS_COLUMNS",
    "STATUS_FILENAME",
    "import_security_data",
    "load_verified_security_data",
    "publish_security_data",
    "validate_security_data",
]
