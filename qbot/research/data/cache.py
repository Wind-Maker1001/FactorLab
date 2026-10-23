"""Atomic per-symbol Parquet cache with explicit request coverage."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import pandas as pd

try:
    from qbot.research.errors import CacheError
except ImportError:  # pragma: no cover - isolated bootstrapping only
    class CacheError(RuntimeError):
        """Fallback used only when the package foundation is unavailable."""

from qbot.research.data.providers.base import DateLike, MarketDataProvider
from qbot.research.data.schema import canonicalize_bars, empty_bars, normalize_symbol


Range = Tuple[pd.Timestamp, pd.Timestamp]
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BarCacheSnapshot:
    """Bars and source evidence read from one committed metadata generation."""

    frame: pd.DataFrame
    metadata: Dict[str, Any]
    metadata_sha256: Optional[str]


def _normalise_date(value: DateLike) -> pd.Timestamp:
    try:
        result = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise CacheError("invalid cache coverage date: %r" % (value,)) from exc
    if result.tzinfo is not None:
        result = result.tz_localize(None)
    return result.normalize()


def _merge_ranges(ranges: Iterable[Range]) -> List[Range]:
    ordered = sorted(
        ((_normalise_date(start), _normalise_date(end)) for start, end in ranges),
        key=lambda item: item[0],
    )
    merged: List[Range] = []
    for start, end in ordered:
        if start > end:
            raise CacheError("coverage start must be on or before coverage end")
        if not merged or start > merged[-1][1] + pd.Timedelta(days=1):
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _subtract_ranges(requested: Range, covered: Sequence[Range]) -> List[Range]:
    start, end = requested
    cursor = start
    gaps: List[Range] = []
    for covered_start, covered_end in _merge_ranges(covered):
        if covered_end < cursor:
            continue
        if covered_start > end:
            break
        if covered_start > cursor:
            gaps.append((cursor, min(end, covered_start - pd.Timedelta(days=1))))
        cursor = max(cursor, covered_end + pd.Timedelta(days=1))
        if cursor > end:
            break
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ParquetBarCache:
    """Cache canonical bars in one independently resumable file per symbol.

    ``covered_ranges`` records successful vendor requests, including requests
    that returned no rows.  Therefore incremental updates do not repeatedly
    fetch weekends, suspensions, or pre-listing days merely because no bar is
    stored for them.
    """

    # v3 publishes immutable data first and atomically switches a metadata
    # pointer last. Strict v2 remains readable and migrates on the next update.
    # v1 cannot prove the endpoint/adjustment that produced its bytes.
    metadata_version = 3

    def __init__(self, root: Path, lock_timeout_seconds: float = 10.0) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if (
            isinstance(lock_timeout_seconds, bool)
            or not isinstance(lock_timeout_seconds, (int, float))
            or not math.isfinite(float(lock_timeout_seconds))
            or float(lock_timeout_seconds) < 0.0
        ):
            raise CacheError("lock_timeout_seconds must be finite and non-negative")
        self.lock_timeout_seconds = float(lock_timeout_seconds)

    @contextmanager
    def _writer_lock(self, symbol: str) -> Iterator[None]:
        """Serialize read/merge/publish across processes; OS releases on exit.

        The small lock file is permanent. Its existence does not indicate an
        active writer and it must not be deleted to unlock another process.
        """

        lock_path = self.root / (".%s.write.lock" % normalize_symbol(symbol))
        if lock_path.is_symlink() or lock_path.resolve().parent != self.root:
            raise CacheError("cache writer lock path escapes its root")
        # Windows byte-range locks can cover a byte beyond EOF. Acquire that
        # native lock before initializing a newly created file: otherwise two
        # buffered handles can both observe length zero, and one may flush its
        # pending byte after the other has already locked that byte. FileIO
        # avoids carrying a deferred write into the unlock/close sequence.
        try:
            handle = lock_path.open("a+b", buffering=0)
        except OSError as exc:
            raise CacheError("cannot open cache writer lock for %s" % symbol) from exc
        with handle:
            deadline = time.monotonic() + self.lock_timeout_seconds
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:  # pragma: no cover - exercised on POSIX CI
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise CacheError(
                            "cache writer lock timeout for %s" % symbol
                        ) from exc
                    time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
            protected_error = None
            try:
                try:
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                except OSError as exc:
                    raise CacheError(
                        "cannot initialize cache writer lock for %s" % symbol
                    ) from exc
                yield
            except BaseException as exc:
                protected_error = exc
                raise
            finally:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:  # pragma: no cover - exercised on POSIX CI
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError as exc:
                    # Closing the unbuffered handle is the final OS-lock
                    # release. Preserve a primary body/initialization failure,
                    # but never report success if explicit unlocking failed.
                    if protected_error is None:
                        raise CacheError(
                            "cannot release cache writer lock for %s" % symbol
                        ) from exc
                    LOGGER.warning(
                        "cannot release cache writer lock for %s while handling %s: %s",
                        symbol, type(protected_error).__name__, exc,
                    )

    def data_path(self, symbol: str) -> Path:
        """Return the active generation, or the legacy path for an absent cache."""

        canonical_symbol = normalize_symbol(symbol)
        metadata = self.read_metadata(canonical_symbol)
        return self._data_path_from_metadata(canonical_symbol, metadata)

    def _data_path_from_metadata(
        self, symbol: str, metadata: Dict[str, Any]
    ) -> Path:
        if metadata.get("metadata_version") != 3:
            path = self.root / (symbol + ".parquet")
            if path.is_symlink() or path.resolve().parent != self.root:
                raise CacheError("legacy cache path escapes its root: %s" % path)
            return path
        generation = metadata.get("generation_id")
        filename = metadata.get("data_file")
        if (
            not isinstance(generation, str)
            or len(generation) != 32
            or any(character not in "0123456789abcdef" for character in generation)
            or filename != "%s.%s.parquet" % (symbol, generation)
        ):
            raise CacheError("invalid cache generation pointer for %s" % symbol)
        path = self.root / filename
        if path.is_symlink() or path.resolve().parent != self.root:
            raise CacheError("cache generation path escapes its root: %s" % path)
        return path

    def metadata_path(self, symbol: str) -> Path:
        return self.root / (normalize_symbol(symbol) + ".metadata.json")

    def read_metadata(self, symbol: str) -> Dict[str, Any]:
        return self._read_metadata_with_digest(symbol)[0]

    def _read_metadata_with_digest(
        self, symbol: str
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        path = self.metadata_path(symbol)
        try:
            content = path.read_bytes()
            value = json.loads(content.decode("utf-8"))
        except FileNotFoundError:
            return {}, None
        except (OSError, ValueError) as exc:
            raise CacheError("cannot read cache metadata: %s" % path) from exc
        if not isinstance(value, dict) or not value:
            raise CacheError("cache metadata must be a non-empty JSON object: %s" % path)
        return value, hashlib.sha256(content).hexdigest()

    @staticmethod
    def _normalise_identity(value: Optional[str], label: str) -> str:
        normalised = "unknown" if value is None else str(value).strip()
        if not normalised:
            raise CacheError("cache %s must be a non-empty string" % label)
        return normalised

    def _validated_metadata(self, symbol: str) -> Dict[str, Any]:
        """Validate that data and metadata form one auditable cache artifact."""

        return self._validated_metadata_snapshot(symbol)[0]

    def _validated_metadata_snapshot(
        self, symbol: str
    ) -> Tuple[Dict[str, Any], Optional[str]]:

        canonical_symbol = normalize_symbol(symbol)
        # Read the pointer once. A concurrent publisher can switch the current
        # pointer, but cannot change this snapshot's generation data file.
        metadata, metadata_digest = self._read_metadata_with_digest(canonical_symbol)
        if metadata and (
            not isinstance(metadata.get("metadata_version"), int)
            or isinstance(metadata.get("metadata_version"), bool)
            or metadata.get("metadata_version") not in (2, self.metadata_version)
        ):
            raise CacheError(
                "unsupported cache metadata version for %s: %r"
                % (canonical_symbol, metadata.get("metadata_version"))
            )
        data_path = self._data_path_from_metadata(canonical_symbol, metadata)
        if not metadata:
            if data_path.exists():
                raise CacheError(
                    "incomplete cache artifact for %s; missing %s"
                    % (canonical_symbol, self.metadata_path(canonical_symbol))
                )
            # Unreferenced generations from interrupted first writes are not
            # committed coverage, and must not prevent a resumable retry.
            return {}, None
        if not data_path.is_file():
            raise CacheError(
                "incomplete cache artifact for %s; missing %s"
                % (canonical_symbol, data_path)
            )
        if metadata.get("symbol") != canonical_symbol:
            raise CacheError(
                "cache metadata symbol mismatch for %s" % canonical_symbol
            )
        for field in ("source", "source_version", "content_sha256"):
            value = metadata.get(field)
            if not isinstance(value, str) or not value.strip():
                raise CacheError(
                    "cache metadata has no valid %s for %s"
                    % (field, canonical_symbol)
                )
        content_hash = metadata["content_sha256"]
        if len(content_hash) != 64 or any(
            character not in "0123456789abcdef" for character in content_hash.lower()
        ):
            raise CacheError(
                "cache metadata has an invalid content_sha256 for %s"
                % canonical_symbol
            )
        row_count = metadata.get("row_count")
        if (
            isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count < 0
        ):
            raise CacheError(
                "cache metadata has an invalid row_count for %s" % canonical_symbol
            )
        data_start = metadata.get("data_start")
        data_end = metadata.get("data_end")
        if (data_start is None) != (data_end is None):
            raise CacheError(
                "cache metadata has incomplete data bounds for %s"
                % canonical_symbol
            )
        if data_start is not None:
            start = _normalise_date(data_start)
            end = _normalise_date(data_end)
            if start > end:
                raise CacheError(
                    "cache metadata data_start is after data_end for %s"
                    % canonical_symbol
                )
        self._coverage_from_metadata(metadata)
        return metadata, metadata_digest

    def assert_source_identity(
        self,
        symbol: str,
        *,
        source: str,
        source_version: Optional[str],
    ) -> None:
        """Fail rather than silently reuse bars from another data contract."""

        canonical_symbol = normalize_symbol(symbol)
        metadata = self._validated_metadata(canonical_symbol)
        self._assert_source_matches(
            canonical_symbol, metadata, source, source_version
        )

    def _assert_source_matches(
        self,
        symbol: str,
        metadata: Dict[str, Any],
        source: str,
        source_version: Optional[str],
    ) -> None:
        expected_source = self._normalise_identity(source, "source")
        expected_version = self._normalise_identity(source_version, "source_version")
        if not metadata:
            return
        actual = (metadata["source"], metadata["source_version"])
        expected = (expected_source, expected_version)
        if actual != expected:
            raise CacheError(
                "cache source identity mismatch for %s: cached=%r, expected=%r; "
                "archive and rebuild this symbol cache or use a separate data_root"
                % (symbol, actual, expected)
            )

    @staticmethod
    def _coverage_from_metadata(metadata: Dict[str, Any]) -> List[Range]:
        ranges: List[Range] = []
        for value in metadata.get("covered_ranges", []):
            if not isinstance(value, dict) or "start" not in value or "end" not in value:
                raise CacheError("invalid covered_ranges entry in cache metadata")
            ranges.append((_normalise_date(value["start"]), _normalise_date(value["end"])))
        return _merge_ranges(ranges)

    def missing_ranges(
        self,
        symbol: str,
        start: DateLike,
        end: DateLike,
    ) -> List[Range]:
        """Return uncovered inclusive calendar-date ranges for a request."""

        requested = (_normalise_date(start), _normalise_date(end))
        if requested[0] > requested[1]:
            raise CacheError("cache request start must be on or before end")
        covered = self._coverage_from_metadata(
            self._validated_metadata(normalize_symbol(symbol))
        )
        return _subtract_ranges(requested, covered)

    def load(
        self,
        symbol: str,
        verify_hash: bool = True,
        expected_source: Optional[str] = None,
        expected_source_version: Optional[str] = None,
    ) -> pd.DataFrame:
        """Load and validate a symbol cache, returning an empty canonical frame."""

        return self.load_snapshot(
            symbol,
            verify_hash=verify_hash,
            expected_source=expected_source,
            expected_source_version=expected_source_version,
        ).frame

    def load_snapshot(
        self,
        symbol: str,
        verify_hash: bool = True,
        expected_source: Optional[str] = None,
        expected_source_version: Optional[str] = None,
    ) -> BarCacheSnapshot:
        """Read bars and provenance from the same committed generation.

        The pointer and Parquet bytes are each read once, so hashes, source
        evidence, and returned rows cannot describe different concurrent
        updates. Strict legacy v2 caches use the same single-byte-snapshot
        validation; their next successful update publishes a v3 generation.
        """

        canonical_symbol = normalize_symbol(symbol)
        if (expected_source is None) != (expected_source_version is None):
            raise CacheError(
                "expected_source and expected_source_version must be provided together"
            )
        metadata, metadata_digest = self._validated_metadata_snapshot(canonical_symbol)
        if expected_source is not None:
            self._assert_source_matches(
                canonical_symbol,
                metadata,
                expected_source,
                expected_source_version,
            )
        if not metadata:
            return BarCacheSnapshot(empty_bars(), {}, None)
        path = self._data_path_from_metadata(canonical_symbol, metadata)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise CacheError("cannot read cached Parquet: %s" % path) from exc
        expected_hash = metadata.get("content_sha256")
        if verify_hash and hashlib.sha256(content).hexdigest() != expected_hash:
            raise CacheError("cache content hash mismatch: %s" % path)
        try:
            frame = pd.read_parquet(io.BytesIO(content))
        except Exception as exc:
            raise CacheError("cannot read cached Parquet: %s" % path) from exc
        try:
            frame = canonicalize_bars(
                frame, symbol=canonical_symbol, allow_missing_ohlc=True
            )
        except Exception as exc:
            raise CacheError("cached bars failed schema validation: %s" % path) from exc
        if len(frame) != metadata["row_count"]:
            raise CacheError("cache row_count mismatch: %s" % path)
        actual_start = (
            frame["date"].min().date().isoformat() if not frame.empty else None
        )
        actual_end = (
            frame["date"].max().date().isoformat() if not frame.empty else None
        )
        if (actual_start, actual_end) != (
            metadata.get("data_start"),
            metadata.get("data_end"),
        ):
            raise CacheError("cache data bounds mismatch: %s" % path)
        coverage = self._coverage_from_metadata(metadata)
        if not frame.empty and not coverage:
            raise CacheError("cache has bars without request coverage: %s" % path)
        if not frame.empty:
            covered = pd.Series(False, index=frame.index)
            for start, end in coverage:
                covered |= frame["date"].between(start, end)
            if not covered.all():
                raise CacheError("cached bars fall outside request coverage: %s" % path)
        return BarCacheSnapshot(frame, metadata, metadata_digest)

    @staticmethod
    def _atomic_parquet(frame: pd.DataFrame, target: Path) -> None:
        temporary = target.with_name(".%s.tmp-%s" % (target.name, uuid.uuid4().hex))
        try:
            frame.to_parquet(temporary, index=False)
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(target))
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _atomic_json(value: Dict[str, Any], target: Path) -> None:
        temporary = target.with_name(".%s.tmp-%s" % (target.name, uuid.uuid4().hex))
        payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(target))
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def update(
        self,
        symbol: str,
        bars: pd.DataFrame,
        covered_start: DateLike,
        covered_end: DateLike,
        source: str,
        source_version: Optional[str] = None,
    ) -> pd.DataFrame:
        """Merge under a process lock and atomically publish a new generation."""

        canonical_symbol = normalize_symbol(symbol)
        with self._writer_lock(canonical_symbol):
            return self._update_locked(
                canonical_symbol, bars, covered_start, covered_end, source, source_version
            )

    def _update_locked(
        self,
        symbol: str,
        bars: pd.DataFrame,
        covered_start: DateLike,
        covered_end: DateLike,
        source: str,
        source_version: Optional[str],
    ) -> pd.DataFrame:

        canonical_symbol = normalize_symbol(symbol)
        normalised_source = self._normalise_identity(source, "source")
        normalised_source_version = self._normalise_identity(
            source_version, "source_version"
        )
        self.assert_source_identity(
            canonical_symbol,
            source=normalised_source,
            source_version=normalised_source_version,
        )
        coverage = (_normalise_date(covered_start), _normalise_date(covered_end))
        if coverage[0] > coverage[1]:
            raise CacheError("covered_start must be on or before covered_end")
        try:
            incoming = canonicalize_bars(
                bars, symbol=canonical_symbol, allow_missing_ohlc=True
            )
        except Exception as exc:
            raise CacheError("incoming bars failed schema validation") from exc
        outside = ~incoming["date"].between(coverage[0], coverage[1])
        if outside.any():
            raise CacheError("incoming bars fall outside the declared coverage")

        current = self.load(
            canonical_symbol,
            expected_source=normalised_source,
            expected_source_version=normalised_source_version,
        )
        if current.empty:
            merged = incoming
        elif incoming.empty:
            merged = current
        else:
            # A later successful request is authoritative for overlapping dates.
            merged = pd.concat([current, incoming], ignore_index=True)
            merged = merged.drop_duplicates(["date", "symbol"], keep="last")
            merged = canonicalize_bars(
                merged, symbol=canonical_symbol, allow_missing_ohlc=True
            )

        old_metadata = self.read_metadata(canonical_symbol)
        covered_ranges = self._coverage_from_metadata(old_metadata)
        covered_ranges = _merge_ranges(covered_ranges + [coverage])
        generation = uuid.uuid4().hex
        data_path = self.root / ("%s.%s.parquet" % (canonical_symbol, generation))
        if data_path.exists():
            raise CacheError("new cache generation already exists: %s" % data_path)
        metadata_path = self.metadata_path(canonical_symbol)
        try:
            self._atomic_parquet(merged, data_path)
            metadata: Dict[str, Any] = {
                "metadata_version": self.metadata_version,
                "generation_id": generation,
                "data_file": data_path.name,
                "symbol": canonical_symbol,
                "source": normalised_source,
                "source_version": normalised_source_version,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "row_count": int(len(merged)),
                "data_start": (
                    merged["date"].min().date().isoformat() if not merged.empty else None
                ),
                "data_end": (
                    merged["date"].max().date().isoformat() if not merged.empty else None
                ),
                "covered_ranges": [
                    {
                        "start": start.date().isoformat(),
                        "end": end.date().isoformat(),
                    }
                    for start, end in covered_ranges
                ],
                "content_sha256": _sha256(data_path),
            }
            self._atomic_json(metadata, metadata_path)
        except Exception as exc:
            raise CacheError("failed to atomically update cache for %s" % canonical_symbol) from exc
        return merged

    def update_from_provider(
        self,
        provider: MarketDataProvider,
        symbol: str,
        start: DateLike,
        end: DateLike,
        is_index: bool = False,
    ) -> pd.DataFrame:
        """Fetch only uncovered ranges and update the cache deterministically."""

        canonical_symbol = normalize_symbol(symbol)
        source = provider.__class__.__name__
        try:
            identity_method = getattr(provider, "cache_source_version", None)
            if callable(identity_method):
                source_version = str(identity_method(canonical_symbol, is_index))
            else:
                source_version = str(getattr(provider, "source_version", "unknown"))
        except Exception as exc:
            raise CacheError(
                "cannot resolve provider cache identity for %s" % canonical_symbol
            ) from exc
        self.assert_source_identity(
            canonical_symbol,
            source=source,
            source_version=source_version,
        )
        for gap_start, gap_end in self.missing_ranges(
            canonical_symbol, start, end
        ):
            if is_index:
                bars = provider.get_index_bars(
                    canonical_symbol, gap_start, gap_end
                )
            else:
                bars = provider.get_daily_bars(
                    canonical_symbol, gap_start, gap_end
                )
            self.update(
                canonical_symbol,
                bars,
                covered_start=gap_start,
                covered_end=gap_end,
                source=source,
                source_version=source_version,
            )
        return self.load(
            canonical_symbol,
            expected_source=source,
            expected_source_version=source_version,
        )


__all__ = ["BarCacheSnapshot", "ParquetBarCache"]
