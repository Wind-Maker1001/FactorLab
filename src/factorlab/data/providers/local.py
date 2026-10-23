"""Validated local diagnostics and source-qualified static market snapshots.

The source declaration is supplied by the importer; it does not establish
independent source authority. PIT membership and security status are separate
inputs. A changed bundle has a different provider/cache identity and must use
a separate cache root or an explicitly rebuilt cache. Legacy v1 bundles are
readable diagnostics; research requires replayable v2 qualification regardless
of the importer-supplied source label.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
import numbers
import os
from pathlib import Path
import re
import threading
import uuid
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from factorlab.data.providers.base import DateLike, MarketDataProvider
from factorlab.data.schema import (
    CANONICAL_BAR_COLUMNS,
    NUMERIC_BAR_COLUMNS,
    canonicalize_bars,
    normalize_symbol,
)
from factorlab.errors import (
    PointInTimeUniverseRequiredError,
    ProviderError,
    SchemaError,
)


LOCAL_MARKET_SCHEMA_VERSION = 1
LOCAL_MARKET_CONTRACTS = {
    "stock_adjustment": "hfq",
    "index_adjustment": "none",
    "price": "CNY",
    "volume": "shares",
    "amount": "CNY",
    "turnover": "fraction",
    "calendar": "benchmark-trading-dates",
    "stock_snapshot": "static-historical",
}
SOURCE_DECLARATION = "user-provided-not-independently-verified"
LOGGER = logging.getLogger(__name__)
_HASH = re.compile(r"^[0-9a-f]{64}$")
_GENERATION = re.compile(r"^[0-9a-f]{32}$")
_MANIFEST_FIELDS = {
    "schema_version", "source", "source_version", "source_declaration",
    "generated_at", "benchmark_symbol", "contracts", "generation_id",
    "stocks_file", "benchmark_file", "stocks_file_sha256",
    "benchmark_file_sha256", "input_sha256", "input_files", "stocks_rows",
    "benchmark_rows", "stocks_start", "stocks_end", "benchmark_start",
    "benchmark_end", "stock_symbols", "bundle_version",
}
_LOCATOR_FIELDS = {
    "generated_at", "generation_id", "stocks_file", "benchmark_file",
    "input_files", "bundle_version", "qualification_dir",
}
_QUALIFICATION_FIELDS = {"qualification_dir", "qualification_manifest_sha256", "qualification_report"}


def _fail(detail: str) -> ProviderError:
    return ProviderError("local market data: %s" % detail)


def _identity(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.casefold() in {"unknown", "none", "null", "n/a"}
        or not value.isprintable()
        or len(value) > 2048
    ):
        raise _fail("%s must be an explicit, printable, non-unknown string" % name)
    return value


def _symbol(value: Any) -> str:
    if not isinstance(value, str):
        raise _fail("symbol must be a canonical string")
    try:
        canonical = normalize_symbol(value)
    except SchemaError as exc:
        raise _fail("invalid symbol %r" % value) from exc
    if value != canonical:
        raise _fail("symbol must already be canonical: %r" % value)
    return canonical


def _strict_dates(values: pd.Series) -> pd.Series:
    if values.isna().any():
        raise _fail("date must not be missing")
    if isinstance(values.dtype, pd.DatetimeTZDtype):
        raise _fail("date must be timezone-naive")
    if pd.api.types.is_numeric_dtype(values.dtype):
        raise _fail("date must not be a numeric timestamp")
    try:
        if pd.api.types.is_datetime64_any_dtype(values.dtype):
            parsed = pd.to_datetime(values, errors="raise")
        else:
            # The vectorized string check rejects mixed objects and numeric
            # values instead of coercing them into an apparent trading date.
            matches = values.str.fullmatch(r"\d{4}-\d{2}-\d{2}")
            if not matches.fillna(False).all():
                raise _fail("date strings must use canonical YYYY-MM-DD form")
            parsed = pd.to_datetime(values, format="%Y-%m-%d", errors="raise")
    except ProviderError:
        raise
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise _fail("date must use YYYY-MM-DD strings or naive datetime64") from exc
    if isinstance(parsed.dtype, pd.DatetimeTZDtype):
        raise _fail("date must be timezone-naive")
    if not parsed.eq(parsed.dt.normalize()).all():
        raise _fail("date must not contain a time-of-day")
    return parsed.astype("datetime64[ns]")


def _strict_bars(frame: pd.DataFrame, *, is_index: bool, benchmark_symbol: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise _fail("stock and benchmark inputs must both be non-empty tables")
    if frame.columns.duplicated().any():
        raise _fail("duplicate input columns are not allowed")
    required = set(CANONICAL_BAR_COLUMNS)
    missing = sorted(required.difference(frame.columns))
    extra = sorted(set(frame.columns).difference(required))
    if missing or extra:
        raise _fail("explicit canonical/raw/adjusted columns required; missing=%s extra=%s" % (missing, extra))
    result = frame.loc[:, list(CANONICAL_BAR_COLUMNS)].copy()
    result["date"] = _strict_dates(result["date"])
    try:
        symbols_valid = result["symbol"].str.fullmatch(r"\d{6}\.(?:SH|SZ)").fillna(False)
    except AttributeError as exc:
        raise _fail("symbol must already be canonical") from exc
    if not symbols_valid.all():
        raise _fail("symbol must already be canonical")
    symbols = set(result["symbol"].unique())
    if is_index and symbols != {benchmark_symbol}:
        raise _fail("benchmark input must contain only %s" % benchmark_symbol)
    if not is_index and benchmark_symbol in symbols:
        raise _fail("benchmark must not occupy a stock-data slot")
    for column in NUMERIC_BAR_COLUMNS:
        if pd.api.types.is_bool_dtype(result[column].dtype):
            raise _fail("%s must contain real numeric market values" % column)
        try:
            result[column] = pd.to_numeric(result[column], errors="raise")
        except (TypeError, ValueError) as exc:
            raise _fail("%s contains a non-numeric market value" % column) from exc
    try:
        clean = canonicalize_bars(result, allow_missing_ohlc=not is_index)
    except SchemaError as exc:
        raise _fail("invalid bar schema: %s" % exc) from exc
    for field in ("open", "high", "low", "close"):
        if not clean[field].notna().eq(clean["raw_" + field].notna()).all():
            raise _fail("canonical and raw OHLC must have identical missing observations")
    paired = clean["raw_close"].notna().eq(clean["adj_close"].notna())
    if not paired.all() or (
        clean["raw_close"].notna() & clean["adj_factor"].isna()
    ).any():
        raise _fail("present raw prices require explicit adjusted prices and adj_factor")
    if is_index:
        if clean["adj_factor"].isna().any() or not np.allclose(clean["adj_factor"], 1.0, rtol=1e-12, atol=1e-12):
            raise _fail("benchmark adj_factor must be 1")
        for field in ("open", "high", "low", "close"):
            if not np.allclose(clean["raw_" + field], clean["adj_" + field], rtol=1e-12, atol=1e-12):
                raise _fail("benchmark raw and adjusted prices must be identical")
    return clean


def _read_input(path: Path) -> Tuple[pd.DataFrame, str]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise _fail("cannot read input %s: %s" % (path, exc)) from exc
    try:
        if path.suffix.lower() == ".csv":
            frame = pd.read_csv(io.BytesIO(content))
        elif path.suffix.lower() in {".parquet", ".pq"}:
            frame = pd.read_parquet(io.BytesIO(content))
        else:
            raise _fail("input files must be CSV or Parquet")
    except ProviderError:
        raise
    except Exception as exc:
        raise _fail("cannot parse input %s: %s" % (path, exc)) from exc
    return frame, hashlib.sha256(content).hexdigest()


def _root(path: Path, *, create: bool = False) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise _fail("bundle root must not be a symlink")
    try:
        output = requested.resolve()
        if create:
            output.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError) as exc:
        raise _fail("bundle root is inaccessible: %s" % requested) from exc
    if not output.is_dir():
        raise _fail("bundle root does not exist: %s" % output)
    return output


def _artifact(root: Path, filename: str) -> Path:
    path = root / filename
    try:
        resolved_parent = path.resolve().parent
    except (OSError, RuntimeError) as exc:
        raise _fail("bundle artifact path cannot be resolved") from exc
    if path.is_symlink() or resolved_parent != root:
        raise _fail("bundle artifact path escapes its root or uses a symlink")
    return path


def _write_immutable_parquet(frame: pd.DataFrame, destination: Path) -> None:
    # A failed write may leave an unreferenced orphan. Exclusive creation
    # preserves every previous generation, including a UUID-collision case.
    with destination.open("xb") as handle:
        frame.to_parquet(handle, index=False)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_manifest(payload: Dict[str, Any], destination: Path) -> None:
    temporary = destination.with_name(".%s.tmp-%s" % (destination.name, uuid.uuid4().hex))
    owns_temporary = False
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            owns_temporary = True
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(destination))
    finally:
        if owns_temporary:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                # Keep the original publication exception and never delete
                # unrelated history or a colliding writer's temporary file.
                LOGGER.warning("cannot remove local manifest temporary %s: %s", temporary, exc)


def _bundle_version(manifest: Dict[str, Any]) -> str:
    payload = {name: value for name, value in manifest.items() if name not in _LOCATOR_FIELDS}
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    prefix = b"factorlab-local-market-v2\0" if manifest.get("schema_version") == 2 else b"factorlab-local-market-v1\0"
    return hashlib.sha256(prefix + encoded).hexdigest()


def _table_statistics(stocks: pd.DataFrame, benchmark: pd.DataFrame) -> Dict[str, Any]:
    return {
        "stocks_rows": int(len(stocks)),
        "benchmark_rows": int(len(benchmark)),
        "stocks_start": stocks["date"].min().strftime("%Y-%m-%d"),
        "stocks_end": stocks["date"].max().strftime("%Y-%m-%d"),
        "benchmark_start": benchmark["date"].min().strftime("%Y-%m-%d"),
        "benchmark_end": benchmark["date"].max().strftime("%Y-%m-%d"),
        "stock_symbols": sorted(stocks["symbol"].unique().tolist()),
    }


def _validate_pair(stocks: pd.DataFrame, benchmark: pd.DataFrame) -> None:
    if not stocks["date"].isin(benchmark["date"]).all():
        raise _fail("stock dates must be present in the benchmark trading calendar")


def publish_local_market_data(
    stock_path: Path,
    benchmark_path: Path,
    output_dir: Path,
    source: str,
    source_version: str,
    benchmark_symbol: str = "000905.SH",
    qualification_manifest: Optional[Path] = None,
) -> Dict[str, Any]:
    """Publish a declared HFQ stock/raw-index bundle; commit the pointer last.

    Calling this function declares stocks to be HFQ and volumes/amounts/
    turnovers to use shares/CNY/fractions. These conventions cannot be inferred
    or proven from price values alone. Nothing is downloaded or filled.
    """

    source = _identity(source, "source")
    source_version = _identity(source_version, "source_version")
    benchmark_symbol = _symbol(benchmark_symbol)
    raw_stocks, stocks_input_sha = _read_input(Path(stock_path))
    raw_benchmark, benchmark_input_sha = _read_input(Path(benchmark_path))
    stocks = _strict_bars(raw_stocks, is_index=False, benchmark_symbol=benchmark_symbol)
    benchmark = _strict_bars(raw_benchmark, is_index=True, benchmark_symbol=benchmark_symbol)
    _validate_pair(stocks, benchmark)
    qualification = None
    if qualification_manifest is not None:
        from factorlab.data.market_qualification import load_qualification
        qualification = load_qualification(Path(qualification_manifest), stocks=stocks, benchmark=benchmark)
        report = qualification[0]
        if any(report.stages[name] != "PASS" for name in ("RAW", "ADJUSTMENT", "STATUS", "PIT")):
            raise _fail("v2 qualification is incomplete; retain a diagnostic package only")
        if not report.formal_admission and not report.synthetic_only:
            raise _fail("real v2 data require full source/time qualification")
        if report.source_version != source_version:
            raise _fail("qualification source_version differs from declared source")
    elif "tdx" in source.casefold():
        raise _fail("TDX-derived prices require a replayable v2 qualification; v1 declarations cannot promote RAW")
    output = _root(Path(output_dir), create=True)
    generation_id = uuid.uuid4().hex
    manifest: Dict[str, Any] = {
        "schema_version": 2 if qualification is not None else LOCAL_MARKET_SCHEMA_VERSION,
        "source": source,
        "source_version": source_version,
        "source_declaration": SOURCE_DECLARATION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark_symbol": benchmark_symbol,
        "contracts": deepcopy(LOCAL_MARKET_CONTRACTS),
        "generation_id": generation_id,
        "stocks_file": "stocks.%s.parquet" % generation_id,
        "benchmark_file": "benchmark.%s.parquet" % generation_id,
        "input_sha256": {"stocks": stocks_input_sha, "benchmark": benchmark_input_sha},
        "input_files": {
            "stocks": str(Path(stock_path).resolve()),
            "benchmark": str(Path(benchmark_path).resolve()),
        },
        **_table_statistics(stocks, benchmark),
    }
    try:
        stocks_file = _artifact(output, manifest["stocks_file"])
        benchmark_file = _artifact(output, manifest["benchmark_file"])
        manifest_file = _artifact(output, "manifest.json")
        if qualification is not None:
            from dataclasses import asdict
            from factorlab.data.tdx import read_snapshot, safe_path
            report, _, _, _, qualification_digest = qualification
            original_root = safe_path(Path(qualification_manifest).parent)
            content, actual = read_snapshot(original_root / "manifest.json", original_root)
            if actual != qualification_digest:
                raise _fail("qualification changed during publication")
            qualification_payload = json.loads(content)
            manifest["qualification_dir"] = "qualification.%s" % generation_id
            qualification_root = _artifact(output, manifest["qualification_dir"])
            qualification_root.mkdir()
            for filename, digest in qualification_payload["artifacts"].items():
                data, actual = read_snapshot(original_root / filename, original_root)
                if actual != digest:
                    raise _fail("qualification artifact changed during publication")
                with (qualification_root / filename).open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            with (qualification_root / "manifest.json").open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            copied = load_qualification(qualification_root / "manifest.json", stocks=stocks, benchmark=benchmark)
            if copied[4] != qualification_digest:
                raise _fail("copied qualification digest differs")
            manifest["qualification_manifest_sha256"] = qualification_digest
            manifest["qualification_report"] = asdict(report)
        _write_immutable_parquet(stocks, stocks_file)
        _write_immutable_parquet(benchmark, benchmark_file)
        manifest["stocks_file_sha256"] = hashlib.sha256(stocks_file.read_bytes()).hexdigest()
        manifest["benchmark_file_sha256"] = hashlib.sha256(benchmark_file.read_bytes()).hexdigest()
        manifest["bundle_version"] = _bundle_version(manifest)
        _atomic_manifest(manifest, manifest_file)
    except ProviderError:
        raise
    except Exception as exc:
        raise _fail("cannot publish bundle: %s: %s" % (type(exc).__name__, exc)) from exc
    return deepcopy(manifest)


def _validate_manifest(manifest: Any, benchmark_symbol: str) -> Dict[str, Any]:
    if not isinstance(manifest, dict):
        raise _fail("manifest must be an object")
    schema = manifest.get("schema_version")
    expected_fields = _MANIFEST_FIELDS | (_QUALIFICATION_FIELDS if schema == 2 else set())
    if set(manifest) != expected_fields:
        raise _fail("manifest has missing or unknown fields")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema not in {1, 2}:
        raise _fail("unsupported manifest schema")
    _identity(manifest["source"], "source")
    _identity(manifest["source_version"], "source_version")
    if manifest["source_declaration"] != SOURCE_DECLARATION:
        raise _fail("manifest source declaration is unsupported")
    if manifest["contracts"] != LOCAL_MARKET_CONTRACTS:
        raise _fail("manifest price/unit contracts are unsupported")
    if manifest["benchmark_symbol"] != benchmark_symbol:
        raise _fail("manifest benchmark symbol does not match requested benchmark")
    generation = manifest["generation_id"]
    if (
        not isinstance(generation, str)
        or not _GENERATION.fullmatch(generation)
        or manifest["stocks_file"] != "stocks.%s.parquet" % generation
        or manifest["benchmark_file"] != "benchmark.%s.parquet" % generation
    ):
        raise _fail("manifest generation artifact names are not exact safe basenames")
    for name in ("stocks_file_sha256", "benchmark_file_sha256", "bundle_version"):
        if not isinstance(manifest[name], str) or not _HASH.fullmatch(manifest[name]):
            raise _fail("manifest %s is invalid" % name)
    if schema == 2:
        if manifest["qualification_dir"] != "qualification.%s" % generation:
            raise _fail("unsafe qualification generation name")
        if not isinstance(manifest["qualification_manifest_sha256"], str) or not _HASH.fullmatch(manifest["qualification_manifest_sha256"]):
            raise _fail("invalid qualification manifest hash")
        if not isinstance(manifest["qualification_report"], dict):
            raise _fail("qualification report required")
    for name in ("input_sha256", "input_files"):
        values = manifest[name]
        if not isinstance(values, dict) or set(values) != {"stocks", "benchmark"}:
            raise _fail("manifest %s is incomplete" % name)
        for value in values.values():
            if name == "input_sha256":
                if not isinstance(value, str) or not _HASH.fullmatch(value):
                    raise _fail("manifest input hashes are invalid")
            else:
                _identity(value, "input path")
    try:
        generated_at = datetime.fromisoformat(manifest["generated_at"])
    except (TypeError, ValueError) as exc:
        raise _fail("manifest generated_at is invalid") from exc
    if generated_at.tzinfo is None:
        raise _fail("manifest generated_at must include a timezone")
    for name in ("stocks_rows", "benchmark_rows"):
        value = manifest[name]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise _fail("manifest %s must be a positive integer" % name)
    for prefix in ("stocks", "benchmark"):
        start, end = manifest[prefix + "_start"], manifest[prefix + "_end"]
        dates = _strict_dates(pd.Series([start, end]))
        if dates.iloc[0] > dates.iloc[1]:
            raise _fail("manifest date bounds are inverted")
    symbols = manifest["stock_symbols"]
    if not isinstance(symbols, list) or not symbols:
        raise _fail("manifest stock_symbols must be a non-empty canonical list")
    for symbol in symbols:
        _symbol(symbol)
    if symbols != sorted(set(symbols)) or benchmark_symbol in symbols:
        raise _fail("manifest stock_symbols must be sorted and unique without benchmark")
    if _bundle_version(manifest) != manifest["bundle_version"]:
        raise _fail("manifest bundle_version does not match its semantics")
    return deepcopy(manifest)


def _request_date(value: DateLike, label: str) -> pd.Timestamp:
    if isinstance(value, (numbers.Number, bool, np.bool_)):
        raise _fail("%s must not be a numeric timestamp" % label)
    try:
        if isinstance(value, str) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise _fail("%s must use canonical YYYY-MM-DD form" % label)
        date = pd.Timestamp(value)
    except ProviderError:
        raise
    except (TypeError, ValueError) as exc:
        raise _fail("%s is not a valid trading date" % label) from exc
    if pd.isna(date) or date.tzinfo is not None or date != date.normalize():
        raise _fail("%s must be a timezone-naive trading date without a time-of-day" % label)
    return date


class LocalParquetProvider(MarketDataProvider):
    """Pin and verify one static generation without any network operations."""

    def __init__(self, root: Path, benchmark_symbol: str = "000905.SH") -> None:
        self.root = Path(root)
        self.benchmark_symbol = _symbol(benchmark_symbol)
        self._manifest: Optional[Dict[str, Any]] = None
        self._manifest_sha256: Optional[str] = None
        self._stocks: Optional[pd.DataFrame] = None
        self._benchmark: Optional[pd.DataFrame] = None
        self._stock_indices: Dict[str, Any] = {}
        self._load_lock = threading.RLock()
        self._qualification = None

    def _ensure_loaded(self) -> None:
        if self._manifest is not None:
            return
        with self._load_lock:
            if self._manifest is not None:
                return
            root = _root(self.root)
            try:
                manifest_content = _artifact(root, "manifest.json").read_bytes()
                raw_manifest = json.loads(manifest_content.decode("utf-8"))
            except (OSError, UnicodeError, ValueError) as exc:
                raise _fail("manifest is missing or unreadable") from exc
            manifest = _validate_manifest(raw_manifest, self.benchmark_symbol)
            frames = {}
            for role, is_index in (("stocks", False), ("benchmark", True)):
                try:
                    content = _artifact(root, manifest[role + "_file"]).read_bytes()
                except OSError as exc:
                    raise _fail("%s artifact is missing or unreadable" % role) from exc
                if hashlib.sha256(content).hexdigest() != manifest[role + "_file_sha256"]:
                    raise _fail("%s Parquet file SHA-256 mismatch" % role)
                try:
                    frame = pd.read_parquet(io.BytesIO(content))
                except Exception as exc:
                    raise _fail("%s Parquet artifact cannot be parsed" % role) from exc
                frames[role] = _strict_bars(frame, is_index=is_index, benchmark_symbol=self.benchmark_symbol)
            stocks, benchmark = frames["stocks"], frames["benchmark"]
            _validate_pair(stocks, benchmark)
            expected_stats = _table_statistics(stocks, benchmark)
            if any(manifest[name] != value for name, value in expected_stats.items()):
                raise _fail("manifest row counts, date bounds or symbols disagree with artifacts")
            qualification = None
            if manifest["schema_version"] == 2:
                from dataclasses import asdict
                from factorlab.data.market_qualification import load_qualification
                qualification = load_qualification(
                    _artifact(root, manifest["qualification_dir"]) / "manifest.json",
                    stocks=stocks, benchmark=benchmark,
                )
                if (qualification[4] != manifest["qualification_manifest_sha256"]
                        or json.dumps(asdict(qualification[0]), sort_keys=True) != json.dumps(manifest["qualification_report"], sort_keys=True)
                        or qualification[0].source_version != manifest["source_version"]):
                    raise _fail("pinned qualification disagrees with local bundle")
            self.root = root
            self._stocks = stocks
            self._benchmark = benchmark
            self._stock_indices = stocks.groupby("symbol", sort=False).indices
            self._manifest_sha256 = hashlib.sha256(manifest_content).hexdigest()
            self._qualification = qualification
            # Set the publication flag last, only after every check succeeds.
            self._manifest = manifest

    @property
    def source_version(self) -> str:
        self._ensure_loaded()
        return "local-parquet-v%d;bundle=%s" % (self._manifest["schema_version"], self._manifest["bundle_version"])

    @property
    def qualification_report(self) -> Optional[Dict[str, Any]]:
        self._ensure_loaded()
        return deepcopy(self._manifest.get("qualification_report"))

    def assert_research_admission(self, membership: pd.DataFrame, calendar: pd.DatetimeIndex,
                                  bars: pd.DataFrame) -> None:
        """Compatibility entry; financial approval uses the shared replay gate."""
        from factorlab.data.admission import assert_formal_market_admission
        self._ensure_loaded()
        assert_formal_market_admission(
            self, membership=membership, calendar=calendar, bars=bars,
            benchmark=self._benchmark.loc[self._benchmark.date.isin(calendar)],
            research_start=membership.date.min(), research_end=membership.date.max(),
        )

    def get_research_qualification(self):
        """Expose the pinned recipe; only the common replay gate can admit it."""
        from factorlab.data.providers.base import MarketQualificationEvidence
        self._ensure_loaded()
        if self._qualification is None:
            return None
        return MarketQualificationEvidence(
            _artifact(self.root, self._manifest["qualification_dir"]) / "manifest.json",
            self._manifest["qualification_manifest_sha256"],
            self._manifest["source_version"], self._stocks, self._benchmark,
        )

    def assert_security_binding(self, status: pd.DataFrame, listings: pd.DataFrame) -> None:
        self._ensure_loaded()
        if self._qualification is None:
            return
        _, inputs, keys, _, _ = self._qualification
        index = pd.MultiIndex.from_frame(keys)
        left = status.set_index(["date", "symbol"])[["trade_status", "is_st"]].reindex(index)
        right = inputs.status.set_index(["date", "symbol"])[["trade_status", "is_st"]].reindex(index)
        if not left.eq(right).all().all():
            raise _fail("actual security status differs from qualification")
        left_list = listings.set_index("symbol")["listing_date"]
        right_list = inputs.listings.set_index("symbol")["listing_date"]
        if not left_list.reindex(right_list.index).equals(right_list):
            raise _fail("actual listing dates differ from qualification")

    def assert_execution_admission(self) -> None:
        from factorlab.data.admission import assert_formal_market_admission
        self._ensure_loaded()
        if self._qualification is None:
            raise _fail("execution requires replayable v2 qualification")
        _, inputs, _, calendar, _ = self._qualification
        assert_formal_market_admission(
            self, membership=inputs.membership, calendar=calendar,
            bars=self._stocks, benchmark=self._benchmark,
            research_start=inputs.research_start, research_end=inputs.research_end,
            status=inputs.status, listings=inputs.listings, require_execution=True,
        )

    @property
    def bundle_version(self) -> str:
        self._ensure_loaded()
        return self._manifest["bundle_version"]

    @property
    def manifest_sha256(self) -> str:
        self._ensure_loaded()
        return self._manifest_sha256

    @property
    def bundle_manifest(self) -> Dict[str, Any]:
        return self.get_bundle_manifest()

    def get_bundle_manifest(self) -> Dict[str, Any]:
        self._ensure_loaded()
        return deepcopy(self._manifest)

    def cache_source_version(self, symbol: str, is_index: bool) -> str:
        symbol = _symbol(symbol)
        if is_index and symbol != self.benchmark_symbol:
            raise _fail("only the declared benchmark index is available")
        contract = "index-raw-v1" if is_index else "stock-raw+hfq-v1"
        return "%s;contract=%s" % (self.source_version, contract)

    @staticmethod
    def _window(frame: pd.DataFrame, start: DateLike, end: DateLike) -> pd.DataFrame:
        start_date, end_date = _request_date(start, "start"), _request_date(end, "end")
        if start_date > end_date:
            raise _fail("start must be on or before end")
        return frame.loc[frame["date"].between(start_date, end_date)].reset_index(drop=True).copy()

    def get_daily_bars(self, symbol: str, start: DateLike, end: DateLike) -> pd.DataFrame:
        self._ensure_loaded()
        symbol = _symbol(symbol)
        indices = self._stock_indices.get(symbol)
        if indices is None:
            raise _fail("stock symbol %s is absent from the static bundle" % symbol)
        return self._window(self._stocks.iloc[indices], start, end)

    def get_index_bars(self, symbol: str, start: DateLike, end: DateLike) -> pd.DataFrame:
        self._ensure_loaded()
        if _symbol(symbol) != self.benchmark_symbol:
            raise _fail("only the declared benchmark index is available")
        return self._window(self._benchmark, start, end)

    def get_trade_calendar(self, start: DateLike, end: DateLike) -> pd.DatetimeIndex:
        selected = self.get_index_bars(self.benchmark_symbol, start, end)
        return pd.DatetimeIndex(selected["date"]).unique().sort_values()

    def get_index_constituents(self, symbol: str, as_of: Optional[DateLike] = None) -> pd.DataFrame:
        raise PointInTimeUniverseRequiredError(
            "local market-data files are not a PIT universe; import historical membership separately"
        )


__all__ = ["LocalParquetProvider", "publish_local_market_data", "LOCAL_MARKET_CONTRACTS"]
