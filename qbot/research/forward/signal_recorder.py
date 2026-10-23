"""Immutable on-disk records for Phase 4 paper forward signals.

The recorder deliberately does only local validation and file publication.  It
does not fetch data, connect to a broker, or place orders.

Storage uses ``<root>/<signal-date>/<version>/``.  A date-scoped lock is
created with ``O_EXCL`` before a version is selected, the complete version is
written to a private staging directory, and one directory rename publishes
it.  Published paths are never replaced.  With no explicit version, versions
are monotonically allocated as ``v000001``, ``v000002``, and so on.  Supplying
an explicit version is useful for idempotent schedulers; an existing target is
an error rather than a successful no-op.

``activation_date`` is an explicit deployment boundary, not an inferred
historical date.  A signal date before it is rejected.  ``generated_at`` must
be timezone-aware, and *both* its represented local date and its UTC date must
be on or after ``signal_date``.  Checking both views prevents a large timezone
offset from making a historical signal appear contemporaneous.  Date inputs
are canonical ISO ``YYYY-MM-DD`` strings or date objects; datetimes are
accepted only when they are timezone-naive midnight values.

The caller must also classify calendar timeliness.  ``ON_TIME`` requires the
local ``generated_at`` date to equal ``signal_date``; the scheduling/CLI layer
is responsible for checking that the relevant market has closed.
``LATE_REISSUE`` always requires a non-empty audit reason.

Locks are intentionally never broken as "stale" by this module.  A process
cannot safely prove that another process is dead on every supported platform,
so lock contention ends in a clear timeout and requires operator inspection.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
import hashlib
import io
import json
import math
import numbers
import os
from pathlib import Path
import re
import time as time_module
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, Tuple, Union
import uuid

import pandas as pd

from qbot.research.data.schema import normalize_symbol
from qbot.research.errors import FactorLabError, SchemaError
from qbot.research.forward.integrity import (
    ForwardIntegrityError,
    assert_no_link_components,
)


FORWARD_SIGNAL_COLUMNS = (
    "signal_date",
    "data_asof",
    "generated_at",
    "model_name",
    "model_version",
    "model_bundle_sha256",
    "dataset_sha256",
    "git_commit",
    "run_id",
    "universe_version",
    "activation_id",
    "activation_manifest_sha256",
    "research_contract_sha256",
    "source_sha256",
    "timeliness",
    "reissue_reason",
    "symbol",
    "score",
    "rank",
    "target_weight",
)

_SIGNAL_VALUE_COLUMNS = ("symbol", "score", "rank", "target_weight")
FORWARD_PREDICTION_COLUMNS = tuple(
    name for name in FORWARD_SIGNAL_COLUMNS if name != "target_weight"
)
_PREDICTION_VALUE_COLUMNS = ("symbol", "score", "rank")
TOP_K_ONLY = "TOP_K_ONLY"
FULL_ELIGIBLE_CROSS_SECTION = "FULL_ELIGIBLE_CROSS_SECTION"
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SEQUENTIAL_VERSION_PATTERN = re.compile(r"^v(\d{6,})$")
_FULL_GIT_SHA_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


class SignalRecorderError(FactorLabError):
    """Base class for expected forward-recording failures."""


class SignalValidationError(SignalRecorderError):
    """Raised before publication when signal content is not canonical."""


class SignalVersionConflictError(SignalRecorderError):
    """Raised when a requested immutable version path already exists."""


class SignalLockError(SignalRecorderError):
    """Raised when an exclusive publication lock cannot be managed safely."""


class SignalLockTimeoutError(SignalLockError):
    """Raised when the date-scoped exclusive lock is not acquired in time."""


def _safe_signal_path(path: Path) -> Path:
    try:
        return assert_no_link_components(path)
    except ForwardIntegrityError as exc:
        raise SignalValidationError(str(exc)) from exc


@dataclass(frozen=True)
class SignalRecord:
    """Paths and content identity of one successfully published version."""

    signal_date: date
    version: str
    directory: Path
    signals_path: Path
    manifest_path: Path
    sha256: str
    manifest_sha256: str
    row_count: int
    predictions_path: Optional[Path] = None
    predictions_sha256: Optional[str] = None
    prediction_row_count: int = 0
    prediction_scope: str = TOP_K_ONLY
    _prediction_frame: Optional[pd.DataFrame] = field(default=None, repr=False, compare=False)

    @property
    def completion_path(self) -> Path:
        """Append-only success receipt; absent until the producer run succeeds."""

        return self.directory / "completion"

    @property
    def predictions(self) -> pd.DataFrame:
        """Return a defensive copy of the verified full prediction snapshot.

        Legacy Top-K signals never constitute full-cross-sectional evidence.
        Consumers must obtain this record through ``load_signal_record`` with
        its default completion requirement, not regenerate scores after the
        event or treat the writer's unfinalized return as a successful run.
        """

        if self.prediction_scope != FULL_ELIGIBLE_CROSS_SECTION or self._prediction_frame is None:
            raise SignalValidationError(
                "TOP_K_ONLY record has no full-cross-sectional prediction evidence; retrospective rescoring is forbidden"
            )
        return self._prediction_frame.copy(deep=True)


def _canonical_date(value: object, name: str) -> date:
    """Return a date without silently discarding a meaningful time or zone."""

    if value is None or value is pd.NaT:
        raise SignalValidationError("%s must be a canonical date" % name)
    if isinstance(value, datetime):
        try:
            offset = value.utcoffset()
        except (TypeError, ValueError) as exc:
            raise SignalValidationError("%s has an invalid timezone" % name) from exc
        if value.tzinfo is not None or offset is not None:
            raise SignalValidationError("%s must be a timezone-naive date" % name)
        if value.time() != time.min or getattr(value, "nanosecond", 0) != 0:
            raise SignalValidationError("%s must not contain a time-of-day" % name)
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not _DATE_PATTERN.fullmatch(value):
        raise SignalValidationError("%s must use canonical YYYY-MM-DD form" % name)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SignalValidationError("%s is not a valid calendar date" % name) from exc


def _aware_datetime(value: object, name: str) -> datetime:
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            raise SignalValidationError("%s must be timezone-aware" % name)
        value = value.to_pydatetime()
    if isinstance(value, str):
        candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        try:
            value = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise SignalValidationError(
                "%s is not a valid ISO datetime" % name
            ) from exc
    if not isinstance(value, datetime):
        raise SignalValidationError("%s must be a timezone-aware datetime" % name)
    try:
        offset = value.utcoffset()
    except (TypeError, ValueError) as exc:
        raise SignalValidationError("%s has an invalid timezone" % name) from exc
    if value.tzinfo is None or offset is None:
        raise SignalValidationError("%s must include a UTC offset" % name)
    return value


def _clean_text(name: str, value: object, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SignalValidationError("%s must be a non-empty canonical string" % name)
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise SignalValidationError("%s contains invalid text" % name)
    return value


def _git_sha(value: object) -> str:
    text = _clean_text("git_commit", value, maximum=64)
    if not _FULL_GIT_SHA_PATTERN.fullmatch(text):
        raise SignalValidationError("git_commit must be a full 40- or 64-hex Git SHA")
    return text.lower()


def _sha256(value: object, name: str) -> str:
    text = _clean_text(name, value, maximum=64)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", text):
        raise SignalValidationError("%s must be a SHA-256 digest" % name)
    return text.lower()


def _version_name(value: object) -> str:
    text = _clean_text("version", value, maximum=64)
    if not _VERSION_PATTERN.fullmatch(text):
        raise SignalValidationError(
            "version may contain only letters, digits, dot, underscore, and hyphen"
        )
    return text


def _validated_temporal_metadata(
    *, signal_date: object, data_asof: object, generated_at: object,
    activation_date: object, timeliness: object, reissue_reason: object,
) -> Tuple[date, date, datetime, str, str]:
    """Use identical close-known/date semantics at publication and reading.

    This deliberately has no current-clock check or new timezone policy.
    Historical saved evidence remains readable, and calendar/market-close
    classification stays in the existing scheduling layer. Local self-hashes
    do not supply an external timestamp or prevent rewriting an entire bundle.
    """
    day = _canonical_date(signal_date, "signal_date")
    asof = _canonical_date(data_asof, "data_asof")
    activation = _canonical_date(activation_date, "activation_date")
    generated = _aware_datetime(generated_at, "generated_at")
    if day < activation:
        raise SignalValidationError(
            "signal_date %s predates activation_date %s; historical backfill is forbidden"
            % (day.isoformat(), activation.isoformat())
        )
    if asof > day:
        raise SignalValidationError("data_asof must not be later than signal_date")
    if generated.date() < day:
        raise SignalValidationError("generated_at local date must not be earlier than signal_date")
    if generated.astimezone(timezone.utc).date() < day:
        raise SignalValidationError("generated_at UTC date must not be earlier than signal_date")
    timing = _clean_text("timeliness", timeliness, maximum=32)
    if timing not in ("ON_TIME", "LATE_REISSUE"):
        raise SignalValidationError("timeliness must be ON_TIME or LATE_REISSUE")
    if timing == "ON_TIME":
        if generated.date() != day:
            raise SignalValidationError("ON_TIME requires generated_at local date to equal signal_date")
        if reissue_reason not in (None, ""):
            raise SignalValidationError("ON_TIME must not include a reissue_reason")
        reason = ""
    else:
        reason = _clean_text("reissue_reason", reissue_reason)
    return day, asof, generated, timing, reason


def _finite_real(name: str, value: object) -> float:
    if isinstance(value, (bool,)) or not isinstance(value, numbers.Real):
        raise SignalValidationError("%s must be a real number" % name)
    number = float(value)
    if not math.isfinite(number):
        raise SignalValidationError("%s must be finite" % name)
    return number


def _positive_rank(value: object) -> int:
    if isinstance(value, (bool,)) or not isinstance(value, numbers.Integral):
        raise SignalValidationError("rank must be an integer")
    rank = int(value)
    if rank <= 0:
        raise SignalValidationError("rank must be positive")
    return rank


def _as_frame(
    signals: Union[pd.DataFrame, Iterable[Mapping[str, Any]]],
) -> pd.DataFrame:
    if isinstance(signals, pd.DataFrame):
        return signals.copy()
    if isinstance(signals, Mapping):
        return pd.DataFrame([dict(signals)])
    try:
        return pd.DataFrame(list(signals))
    except (TypeError, ValueError) as exc:
        raise SignalValidationError(
            "signals must be a DataFrame or iterable of row mappings"
        ) from exc


def _validate_existing_metadata(
    frame: pd.DataFrame,
    metadata: Mapping[str, str],
    generated_at: datetime,
) -> None:
    """Reject contradictory reserved columns instead of silently replacing them."""

    for column in ("signal_date", "data_asof"):
        if column not in frame.columns:
            continue
        expected = metadata[column]
        for value in frame[column].tolist():
            if _canonical_date(value, "signals.%s" % column).isoformat() != expected:
                raise SignalValidationError(
                    "signals.%s does not match the recording metadata" % column
                )
    if "generated_at" in frame.columns:
        expected_utc = generated_at.astimezone(timezone.utc)
        for value in frame["generated_at"].tolist():
            actual = _aware_datetime(value, "signals.generated_at")
            if actual.astimezone(timezone.utc) != expected_utc:
                raise SignalValidationError(
                    "signals.generated_at does not match the recording metadata"
                )
    for column in (
        "model_name",
        "model_version",
        "model_bundle_sha256",
        "dataset_sha256",
        "git_commit",
        "run_id",
        "universe_version",
        "activation_id",
        "activation_manifest_sha256",
        "research_contract_sha256",
        "source_sha256",
        "timeliness",
        "reissue_reason",
    ):
        if column not in frame.columns:
            continue
        for value in frame[column].tolist():
            if column == "git_commit":
                actual = _git_sha(value)
            elif column in (
                "model_bundle_sha256",
                "dataset_sha256",
                "universe_version",
                "activation_manifest_sha256",
                "research_contract_sha256",
                "source_sha256",
            ):
                actual = _sha256(value, column)
            elif column == "reissue_reason" and metadata[column] == "":
                actual = (
                    ""
                    if value is None or value == ""
                    else _clean_text(column, value)
                )
            else:
                actual = _clean_text(column, value)
            if actual != metadata[column]:
                raise SignalValidationError(
                    "signals.%s does not match the recording metadata" % column
                )


def _canonical_signals(
    signals: Union[pd.DataFrame, Iterable[Mapping[str, Any]]],
    metadata: Mapping[str, str],
    generated_at: datetime,
) -> pd.DataFrame:
    frame = _as_frame(signals)
    missing = sorted(set(_SIGNAL_VALUE_COLUMNS).difference(frame.columns))
    if missing:
        raise SignalValidationError(
            "signals missing required columns: %s" % ", ".join(missing)
        )
    if frame.empty:
        raise SignalValidationError("signals must contain at least one row")
    _validate_existing_metadata(frame, metadata, generated_at)

    rows = []
    for row_number, row in enumerate(
        frame[list(_SIGNAL_VALUE_COLUMNS)].itertuples(index=False, name=None), start=1
    ):
        raw_symbol, raw_score, raw_rank, raw_weight = row
        if not isinstance(raw_symbol, str) or raw_symbol != raw_symbol.strip():
            raise SignalValidationError(
                "symbol in row %d must already be canonical" % row_number
            )
        try:
            canonical_symbol = normalize_symbol(raw_symbol)
        except SchemaError as exc:
            raise SignalValidationError(
                "symbol in row %d is invalid: %r" % (row_number, raw_symbol)
            ) from exc
        if canonical_symbol != raw_symbol:
            raise SignalValidationError(
                "symbol in row %d must already be canonical" % row_number
            )
        score = _finite_real("score in row %d" % row_number, raw_score)
        rank = _positive_rank(raw_rank)
        weight = _finite_real("target_weight in row %d" % row_number, raw_weight)
        if weight < 0.0 or weight > 1.0:
            raise SignalValidationError("target_weight must be between 0 and 1")
        rows.append(
            {
                "symbol": canonical_symbol,
                "score": score,
                "rank": rank,
                "target_weight": weight,
            }
        )

    canonical = pd.DataFrame(rows)
    if canonical["symbol"].duplicated().any():
        raise SignalValidationError("signal symbols must be unique")
    if canonical["rank"].duplicated().any():
        raise SignalValidationError("signal ranks must be unique")
    expected_ranks = list(range(1, len(canonical) + 1))
    if sorted(canonical["rank"].tolist()) != expected_ranks:
        raise SignalValidationError(
            "signal ranks must be contiguous from 1 through row count"
        )

    by_rank = canonical.sort_values("rank", kind="mergesort").reset_index(drop=True)
    expected_order = canonical.sort_values(
        ["score", "symbol"], ascending=[False, True], kind="mergesort"
    ).reset_index(drop=True)
    if by_rank["symbol"].tolist() != expected_order["symbol"].tolist():
        raise SignalValidationError(
            "ranks must follow descending score with canonical-symbol tie breaking"
        )
    if not math.isclose(
        math.fsum(by_rank["target_weight"].tolist()),
        1.0,
        rel_tol=0.0,
        abs_tol=1e-10,
    ):
        raise SignalValidationError("target_weight must sum to 1")

    metadata_columns = FORWARD_SIGNAL_COLUMNS[: FORWARD_SIGNAL_COLUMNS.index("symbol")]
    for column in reversed(metadata_columns):
        by_rank.insert(0, column, metadata[column])
    return by_rank[list(FORWARD_SIGNAL_COLUMNS)]


def _cross_section_size(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, numbers.Integral) or int(value) <= 0:
        raise SignalValidationError("cross_section_size must be a positive integer, not a verified flag")
    return int(value)


def _canonical_predictions(
    predictions: pd.DataFrame,
    metadata: Mapping[str, str],
    generated_at: datetime,
    cross_section_size: object,
) -> pd.DataFrame:
    if not isinstance(predictions, pd.DataFrame):
        raise SignalValidationError("predictions must be a DataFrame of the complete eligible cross-section")
    size = _cross_section_size(cross_section_size)
    frame = predictions.copy()
    if frame.columns.has_duplicates:
        raise SignalValidationError("predictions must not contain duplicate column names")
    missing = sorted(set(_PREDICTION_VALUE_COLUMNS).difference(frame.columns))
    if missing:
        raise SignalValidationError("predictions missing required columns: %s" % missing)
    if len(frame) != size:
        raise SignalValidationError("prediction row count must equal cross_section_size")
    _validate_existing_metadata(frame, metadata, generated_at)
    rows = []
    for raw_symbol, raw_score, raw_rank in frame[list(_PREDICTION_VALUE_COLUMNS)].itertuples(index=False, name=None):
        if not isinstance(raw_symbol, str) or raw_symbol != raw_symbol.strip():
            raise SignalValidationError("prediction symbols must already be canonical")
        try:
            canonical_symbol = normalize_symbol(raw_symbol)
        except SchemaError as exc:
            raise SignalValidationError("prediction symbol is invalid: %r" % raw_symbol) from exc
        if canonical_symbol != raw_symbol:
            raise SignalValidationError("prediction symbols must already be canonical")
        rows.append({"symbol": canonical_symbol, "score": _finite_real("prediction score", raw_score),
                     "rank": _positive_rank(raw_rank)})
    canonical = pd.DataFrame(rows)
    if canonical["symbol"].duplicated().any():
        raise SignalValidationError("prediction symbols must be unique")
    if canonical["rank"].duplicated().any():
        raise SignalValidationError("prediction ranks must be unique")
    if sorted(canonical["rank"].tolist()) != list(range(1, size + 1)):
        raise SignalValidationError("prediction ranks must be contiguous from 1 through cross_section_size")
    by_rank = canonical.sort_values("rank", kind="mergesort").reset_index(drop=True)
    by_score = canonical.sort_values(["score", "symbol"], ascending=[False, True], kind="mergesort").reset_index(drop=True)
    if by_rank["symbol"].tolist() != by_score["symbol"].tolist():
        raise SignalValidationError("prediction ranks must follow descending score with canonical-symbol tie breaking")
    metadata_columns = FORWARD_SIGNAL_COLUMNS[: FORWARD_SIGNAL_COLUMNS.index("symbol")]
    for column in reversed(metadata_columns):
        by_rank.insert(0, column, metadata[column])
    return by_rank[list(FORWARD_PREDICTION_COLUMNS)]


def _validate_prediction_prefix(signals: pd.DataFrame, predictions: pd.DataFrame) -> None:
    columns = list(_PREDICTION_VALUE_COLUMNS)
    if len(signals) > len(predictions) or not signals[columns].reset_index(drop=True).equals(
        predictions[columns].head(len(signals)).reset_index(drop=True)
    ):
        raise SignalValidationError("Top-K signals must exactly match the full prediction prefix (symbols, scores and ranks)")


def _write_exclusive(path: Path, content: bytes) -> None:
    """Create and durably write one staging file without overwrite semantics."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(str(path), flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability; Windows does not expose this uniformly."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _cleanup_staging(path: Path) -> None:
    """Remove only files created by this recorder from its UUID staging path."""

    for name in ("manifest.sha256", "manifest.json", "predictions.csv", "signals.csv"):
        candidate = path / name
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return
    try:
        path.rmdir()
    except OSError:
        pass


class SignalRecorder:
    """Publish strictly validated, append-only forward-signal versions.

    Parameters
    ----------
    root:
        Local output directory.  The class never opens a URL or broker API.
    activation_date:
        Frozen deployment boundary.  Recording a prior signal date is always
        rejected; callers should keep this value in reviewed configuration.
    lock_timeout:
        Seconds to wait for the date-scoped exclusive lock.  Zero means one
        immediate acquisition attempt.  Existing locks are never auto-removed.
    lock_poll_interval:
        Maximum interval between acquisition attempts.
    """

    def __init__(
        self,
        root: Union[str, os.PathLike],
        *,
        activation_date: object,
        lock_timeout: float = 10.0,
        lock_poll_interval: float = 0.05,
    ) -> None:
        self.root = _safe_signal_path(Path(root))
        self.activation_date = _canonical_date(activation_date, "activation_date")
        if isinstance(lock_timeout, bool) or not isinstance(lock_timeout, numbers.Real):
            raise SignalValidationError("lock_timeout must be a non-negative number")
        if isinstance(lock_poll_interval, bool) or not isinstance(
            lock_poll_interval, numbers.Real
        ):
            raise SignalValidationError("lock_poll_interval must be a positive number")
        self.lock_timeout = float(lock_timeout)
        self.lock_poll_interval = float(lock_poll_interval)
        if not math.isfinite(self.lock_timeout) or self.lock_timeout < 0.0:
            raise SignalValidationError("lock_timeout must be a non-negative number")
        if (
            not math.isfinite(self.lock_poll_interval)
            or self.lock_poll_interval <= 0.0
        ):
            raise SignalValidationError("lock_poll_interval must be a positive number")

    def lock_path(self, signal_date: object) -> Path:
        """Return the lock path for diagnostics without acquiring it."""

        day = _canonical_date(signal_date, "signal_date")
        return self.root / (".%s.signal.lock" % day.isoformat())

    @contextmanager
    def _exclusive_date_lock(self, signal_date: date) -> Iterator[None]:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SignalLockError("cannot create signal root %s" % self.root) from exc
        _safe_signal_path(self.root)
        if not self.root.is_dir():
            raise SignalLockError("signal root is not a directory: %s" % self.root)

        lock_path = self.lock_path(signal_date)
        token = uuid.uuid4().hex
        deadline = time_module.monotonic() + self.lock_timeout
        descriptor: Optional[int] = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    str(lock_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError as exc:
                remaining = deadline - time_module.monotonic()
                if remaining <= 0.0:
                    raise SignalLockTimeoutError(
                        "timed out acquiring exclusive signal lock %s "
                        "after %.3f seconds"
                        % (lock_path, self.lock_timeout)
                    ) from exc
                time_module.sleep(min(self.lock_poll_interval, remaining))
            except OSError as exc:
                raise SignalLockError(
                    "cannot create exclusive signal lock %s" % lock_path
                ) from exc

        lock_payload = (
            json.dumps(
                {
                    "pid": os.getpid(),
                    "token": token,
                    "signal_date": signal_date.isoformat(),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        try:
            os.write(descriptor, lock_payload)
            os.fsync(descriptor)
        except OSError as exc:
            try:
                lock_path.unlink()
            except OSError:
                pass
            raise SignalLockError(
                "cannot initialize signal lock %s" % lock_path
            ) from exc
        finally:
            os.close(descriptor)
        _fsync_directory(self.root)

        body_failed = False
        try:
            yield
        except BaseException:
            body_failed = True
            raise
        finally:
            try:
                try:
                    payload = json.loads(lock_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError) as exc:
                    raise SignalLockError(
                        "exclusive signal lock disappeared or became invalid: %s"
                        % lock_path
                    ) from exc
                if payload.get("token") != token:
                    raise SignalLockError(
                        "exclusive signal lock ownership changed: %s" % lock_path
                    )
                try:
                    lock_path.unlink()
                except OSError as exc:
                    raise SignalLockError(
                        "cannot release exclusive signal lock %s" % lock_path
                    ) from exc
                _fsync_directory(self.root)
            except SignalLockError:
                if not body_failed:
                    raise

    @staticmethod
    def _next_version(date_directory: Path) -> str:
        highest = 0
        for child in date_directory.iterdir():
            match = _SEQUENTIAL_VERSION_PATTERN.fullmatch(child.name)
            if match:
                highest = max(highest, int(match.group(1)))
        return "v%06d" % (highest + 1)

    def record(
        self,
        signals: Union[pd.DataFrame, Iterable[Mapping[str, Any]]],
        *,
        signal_date: object,
        data_asof: object,
        generated_at: object,
        model_name: str,
        model_version: str,
        model_bundle_sha256: str,
        dataset_sha256: str,
        git_commit: str,
        run_id: str,
        universe_version: str,
        activation_id: str,
        activation_manifest_sha256: str,
        research_contract_sha256: str,
        source_sha256: str,
        timeliness: str,
        reissue_reason: Optional[str] = None,
        version: Optional[str] = None,
        predictions: Optional[pd.DataFrame] = None,
        cross_section_size: Optional[int] = None,
    ) -> SignalRecord:
        """Validate and atomically publish one new immutable signal version.

        ``signals`` must contain ``symbol``, ``score``, ``rank``, and
        ``target_weight``.  Symbols must already be canonical, ranks must be a
        contiguous score-descending ordering (symbol ascending breaks ties),
        scores and weights must be finite, weights must lie in ``[0, 1]``, and
        total target weight must equal one within ``1e-10`` absolute tolerance.
        Any reserved metadata columns already present in the input must match
        the explicit arguments exactly.  ``timeliness`` is caller-supplied
        because this storage layer intentionally has no exchange calendar or
        market-close policy.

        Supplying ``predictions`` requires the exact positive integer
        ``cross_section_size`` of the eligible scoring population. The full
        canonical score/rank table and its Top-K prefix are published in one
        schema-3 directory. Omitting predictions writes a schema-2
        ``TOP_K_ONLY`` record, never full-cross-sectional evidence.
        """

        day, asof, generated, timing, reason = _validated_temporal_metadata(
            signal_date=signal_date, data_asof=data_asof, generated_at=generated_at,
            activation_date=self.activation_date, timeliness=timeliness,
            reissue_reason=reissue_reason,
        )

        metadata = {
            "signal_date": day.isoformat(),
            "data_asof": asof.isoformat(),
            "generated_at": generated.isoformat(),
            "model_name": _clean_text("model_name", model_name),
            "model_version": _clean_text("model_version", model_version),
            "model_bundle_sha256": _sha256(
                model_bundle_sha256, "model_bundle_sha256"
            ),
            "dataset_sha256": _sha256(dataset_sha256, "dataset_sha256"),
            "git_commit": _git_sha(git_commit),
            "run_id": _clean_text("run_id", run_id),
            "universe_version": _sha256(universe_version, "universe_version"),
            "activation_id": _clean_text("activation_id", activation_id),
            "activation_manifest_sha256": _sha256(
                activation_manifest_sha256, "activation_manifest_sha256"
            ),
            "research_contract_sha256": _sha256(
                research_contract_sha256, "research_contract_sha256"
            ),
            "source_sha256": _sha256(source_sha256, "source_sha256"),
            "timeliness": timing,
            "reissue_reason": reason,
        }
        expected_activation_id = "%s-%s" % (
            self.activation_date.isoformat(),
            metadata["research_contract_sha256"][:16],
        )
        if metadata["activation_id"] != expected_activation_id:
            raise SignalValidationError(
                "activation_id does not match activation date and research contract"
            )
        canonical = _canonical_signals(signals, metadata, generated)
        full_predictions = None
        prediction_content = None
        prediction_digest = None
        if predictions is None:
            if cross_section_size is not None:
                raise SignalValidationError("cross_section_size requires full predictions")
        else:
            full_predictions = _canonical_predictions(predictions, metadata, generated, cross_section_size)
            _validate_prediction_prefix(canonical, full_predictions)
            prediction_content = full_predictions.to_csv(
                index=False, lineterminator="\n", float_format="%.17g"
            ).encode("utf-8")
            prediction_digest = hashlib.sha256(prediction_content).hexdigest()
        requested_version = _version_name(version) if version is not None else None
        csv_content = canonical.to_csv(
            index=False,
            lineterminator="\n",
            float_format="%.17g",
        ).encode("utf-8")
        digest = hashlib.sha256(csv_content).hexdigest()

        with self._exclusive_date_lock(day):
            date_directory = self.root / day.isoformat()
            try:
                date_directory.mkdir(exist_ok=True)
            except OSError as exc:
                raise SignalRecorderError(
                    "cannot create signal-date directory %s" % date_directory
                ) from exc
            if not date_directory.is_dir():
                raise SignalRecorderError(
                    "signal-date path is not a directory: %s" % date_directory
                )
            _safe_signal_path(date_directory)

            selected_version = requested_version or self._next_version(date_directory)
            target = date_directory / selected_version
            _safe_signal_path(target)
            if os.path.lexists(str(target)):
                raise SignalVersionConflictError(
                    "immutable signal version already exists: %s" % target
                )

            staging = date_directory / (
                ".%s.tmp-%s" % (selected_version, uuid.uuid4().hex)
            )
            try:
                staging.mkdir(mode=0o700, exist_ok=False)
            except FileExistsError as exc:
                raise SignalVersionConflictError(
                    "exclusive signal staging path already exists: %s" % staging
                ) from exc
            except OSError as exc:
                raise SignalRecorderError(
                    "cannot create signal staging directory %s" % staging
                ) from exc

            manifest: Dict[str, Any] = {
                "artifact_type": "qbot-forward-signals",
                "manifest_version": 3 if full_predictions is not None else 2,
                "live_trading": False,
                "version": selected_version,
                "activation_date": self.activation_date.isoformat(),
                "signal_date": metadata["signal_date"],
                "data_asof": metadata["data_asof"],
                "generated_at": metadata["generated_at"],
                "model_name": metadata["model_name"],
                "model_version": metadata["model_version"],
                "model_bundle_sha256": metadata["model_bundle_sha256"],
                "dataset_sha256": metadata["dataset_sha256"],
                "git_commit": metadata["git_commit"],
                "run_id": metadata["run_id"],
                "universe_version": metadata["universe_version"],
                "activation_id": metadata["activation_id"],
                "activation_manifest_sha256": metadata[
                    "activation_manifest_sha256"
                ],
                "research_contract_sha256": metadata["research_contract_sha256"],
                "source_sha256": metadata["source_sha256"],
                "timeliness": metadata["timeliness"],
                "reissue_reason": metadata["reissue_reason"],
                "row_count": len(canonical),
                "columns": list(FORWARD_SIGNAL_COLUMNS),
                "signals_file": "signals.csv",
                "sha256": digest,
                "prediction_scope": FULL_ELIGIBLE_CROSS_SECTION if full_predictions is not None else TOP_K_ONLY,
                "files": {
                    "signals.csv": {
                        "bytes": len(csv_content),
                        "sha256": digest,
                    }
                },
            }
            if full_predictions is not None:
                manifest.update({
                    "predictions_file": "predictions.csv",
                    "prediction_columns": list(FORWARD_PREDICTION_COLUMNS),
                    "prediction_row_count": len(full_predictions),
                    "cross_section_size": _cross_section_size(cross_section_size),
                    "prediction_sha256": prediction_digest,
                })
                manifest["files"]["predictions.csv"] = {
                    "bytes": len(prediction_content), "sha256": prediction_digest,
                }
            manifest_content = (
                json.dumps(
                    manifest, ensure_ascii=False, indent=2, sort_keys=True
                )
                + "\n"
            ).encode("utf-8")
            manifest_digest = hashlib.sha256(manifest_content).hexdigest()
            published = False
            try:
                _write_exclusive(staging / "signals.csv", csv_content)
                if prediction_content is not None:
                    _write_exclusive(staging / "predictions.csv", prediction_content)
                _write_exclusive(staging / "manifest.json", manifest_content)
                _write_exclusive(
                    staging / "manifest.sha256",
                    (manifest_digest + "\n").encode("ascii"),
                )
                _fsync_directory(staging)
                if os.path.lexists(str(target)):
                    raise SignalVersionConflictError(
                        "immutable signal version already exists: %s" % target
                    )
                try:
                    os.rename(str(staging), str(target))
                except OSError as exc:
                    if os.path.lexists(str(target)):
                        raise SignalVersionConflictError(
                            "immutable signal version already exists: %s" % target
                        ) from exc
                    raise SignalRecorderError(
                        "atomic signal publication failed for %s" % target
                    ) from exc
                published = True
                _fsync_directory(date_directory)
            except FileExistsError as exc:
                raise SignalVersionConflictError(
                    "signal file already exists and will not be overwritten under %s"
                    % staging
                ) from exc
            finally:
                if not published:
                    _cleanup_staging(staging)

        return SignalRecord(
            signal_date=day,
            version=selected_version,
            directory=target,
            signals_path=target / "signals.csv",
            manifest_path=target / "manifest.json",
            sha256=digest,
            manifest_sha256=manifest_digest,
            row_count=len(canonical),
            predictions_path=target / "predictions.csv" if full_predictions is not None else None,
            predictions_sha256=prediction_digest,
            prediction_row_count=len(full_predictions) if full_predictions is not None else 0,
            prediction_scope=FULL_ELIGIBLE_CROSS_SECTION if full_predictions is not None else TOP_K_ONLY,
            _prediction_frame=full_predictions,
        )

    # Names used by scheduler-style callers; all routes retain identical rules.
    append = record
    write = record


def record_signals(
    signals: Union[pd.DataFrame, Iterable[Mapping[str, Any]]],
    output_dir: Union[str, os.PathLike],
    *,
    activation_date: object,
    signal_date: object,
    data_asof: object,
    generated_at: object,
    model_name: str,
    model_version: str,
    model_bundle_sha256: str,
    dataset_sha256: str,
    git_commit: str,
    run_id: str,
    universe_version: str,
    activation_id: str,
    activation_manifest_sha256: str,
    research_contract_sha256: str,
    source_sha256: str,
    timeliness: str,
    reissue_reason: Optional[str] = None,
    version: Optional[str] = None,
    lock_timeout: float = 10.0,
    lock_poll_interval: float = 0.05,
    predictions: Optional[pd.DataFrame] = None,
    cross_section_size: Optional[int] = None,
) -> SignalRecord:
    """One-call wrapper around :class:`SignalRecorder`.

    All time and append-only contracts are the same as ``SignalRecorder``;
    this helper exists for simple daily schedulers that do not retain objects.
    """

    recorder = SignalRecorder(
        output_dir,
        activation_date=activation_date,
        lock_timeout=lock_timeout,
        lock_poll_interval=lock_poll_interval,
    )
    return recorder.record(
        signals,
        signal_date=signal_date,
        data_asof=data_asof,
        generated_at=generated_at,
        model_name=model_name,
        model_version=model_version,
        model_bundle_sha256=model_bundle_sha256,
        dataset_sha256=dataset_sha256,
        git_commit=git_commit,
        run_id=run_id,
        universe_version=universe_version,
        activation_id=activation_id,
        activation_manifest_sha256=activation_manifest_sha256,
        research_contract_sha256=research_contract_sha256,
        source_sha256=source_sha256,
        timeliness=timeliness,
        reissue_reason=reissue_reason,
        version=version,
        predictions=predictions,
        cross_section_size=cross_section_size,
    )


def _validate_completion_receipt(path: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Validate the append-only proof that the producer run finished cleanly."""

    if path.is_symlink() or not path.is_dir():
        raise SignalValidationError("signal completion receipt must be a directory")
    entries = list(path.iterdir())
    if {entry.name for entry in entries} != {
        "receipt.json",
        "receipt.sha256",
        "run_manifest.json",
    }:
        raise SignalValidationError("signal completion receipt inventory is invalid")
    if any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise SignalValidationError("signal completion receipt entries must be regular files")
    content = (path / "receipt.json").read_bytes()
    try:
        recorded_digest = (path / "receipt.sha256").read_text(
            encoding="ascii"
        ).strip()
    except (OSError, UnicodeError) as exc:
        raise SignalValidationError("signal completion digest is unreadable") from exc
    recorded_digest = _sha256(recorded_digest, "completion receipt sha256")
    if hashlib.sha256(content).hexdigest() != recorded_digest:
        raise SignalValidationError("signal completion receipt SHA-256 mismatch")
    try:
        receipt = json.loads(content.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise SignalValidationError("signal completion receipt is unreadable") from exc
    required = {
        "artifact_type",
        "receipt_version",
        "run_id",
        "run_completed_at",
        "run_manifest_sha256",
        "signal_manifest_sha256",
        "signal_sha256",
        "signal_rows",
    }
    if (
        not isinstance(receipt, dict)
        or set(receipt) != required
        or receipt.get("artifact_type") != "qbot-forward-signal-completion"
        or receipt.get("receipt_version") != 1
    ):
        raise SignalValidationError("signal completion receipt schema is invalid")
    _clean_text("completion run_id", receipt.get("run_id"))
    _aware_datetime(receipt.get("run_completed_at"), "run_completed_at")
    for name in (
        "run_manifest_sha256",
        "signal_manifest_sha256",
        "signal_sha256",
    ):
        _sha256(receipt.get(name), name)
    if isinstance(receipt.get("signal_rows"), bool) or not isinstance(
        receipt.get("signal_rows"), int
    ) or receipt["signal_rows"] <= 0:
        raise SignalValidationError("completion signal_rows must be a positive integer")
    run_content = (path / "run_manifest.json").read_bytes()
    if hashlib.sha256(run_content).hexdigest() != receipt["run_manifest_sha256"]:
        raise SignalValidationError("completion run manifest SHA-256 mismatch")
    try:
        producer = json.loads(run_content.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise SignalValidationError("completion run manifest is unreadable") from exc
    producer_expected = {
        "run_id": receipt["run_id"],
        "command": "forward-signal",
        "status": "success",
        "completed_at": receipt["run_completed_at"],
        "signal_sha256": receipt["signal_sha256"],
        "signal_manifest_sha256": receipt["signal_manifest_sha256"],
        "signal_rows": receipt["signal_rows"],
    }
    if any(producer.get(name) != value for name, value in producer_expected.items()):
        raise SignalValidationError(
            "completion run manifest does not prove a successful signal run"
        )
    # Both objects describe the same verified run-manifest bytes. The loader
    # must not re-open the mutable path later to verify full-prediction fields.
    return receipt, producer


def _validate_prediction_producer(producer: Mapping[str, Any], record: SignalRecord) -> None:
    if record.predictions_path is None:
        return
    rows = producer.get("prediction_rows")
    outputs = producer.get("outputs")
    if (
        isinstance(rows, bool) or not isinstance(rows, int)
        or rows != record.prediction_row_count
        or producer.get("prediction_sha256") != record.predictions_sha256
        or not isinstance(outputs, dict)
        or outputs.get("forward_predictions") != str(record.predictions_path)
    ):
        raise SignalValidationError("producer run does not authorize full prediction evidence")


def finalize_signal_record(
    directory: Union[str, os.PathLike],
    run_manifest_path: Union[str, os.PathLike],
) -> SignalRecord:
    """Append a success receipt only after the producer run manifest is final.

    The signal payload is deliberately published before the mutable run
    manifest can be marked successful.  This second, append-only step makes
    that ordering safe: consumers accept only records with a receipt that
    binds the immutable signal hashes to a successful ``forward-signal`` run.
    A crash before this function leaves an invalid orphan version rather than
    a falsely formal prediction; a later invocation must publish a new
    version and never replaces the orphan.
    """

    record = load_signal_record(directory, require_completion=False)
    target = record.completion_path
    if os.path.lexists(str(target)):
        raise SignalVersionConflictError(
            "signal completion receipt already exists: %s" % target
        )
    producer_path = _safe_signal_path(Path(run_manifest_path))
    if producer_path.is_symlink() or not producer_path.is_file():
        raise SignalValidationError("producer run manifest must be a regular file")
    run_content = producer_path.read_bytes()
    try:
        producer = json.loads(run_content.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise SignalValidationError("producer run manifest is unreadable") from exc
    signal_manifest = json.loads(record.manifest_path.read_text(encoding="utf-8"))
    expected = {
        "run_id": signal_manifest["run_id"],
        "command": "forward-signal",
        "status": "success",
        "signal_sha256": record.sha256,
        "signal_manifest_sha256": record.manifest_sha256,
        "signal_rows": record.row_count,
    }
    mismatches = [
        name for name, value in expected.items() if producer.get(name) != value
    ]
    outputs = producer.get("outputs")
    if not isinstance(outputs, dict):
        mismatches.append("outputs")
    else:
        if outputs.get("forward_signals") != str(record.signals_path):
            mismatches.append("outputs.forward_signals")
        if outputs.get("forward_signal_manifest") != str(record.manifest_path):
            mismatches.append("outputs.forward_signal_manifest")
    completed_at = producer.get("completed_at")
    try:
        _aware_datetime(completed_at, "producer completed_at")
    except SignalValidationError:
        mismatches.append("completed_at")
    if mismatches:
        raise SignalValidationError(
            "producer run does not authorize signal completion (%s)"
            % ", ".join(sorted(set(mismatches)))
        )
    _validate_prediction_producer(producer, record)

    receipt = {
        "artifact_type": "qbot-forward-signal-completion",
        "receipt_version": 1,
        "run_id": signal_manifest["run_id"],
        "run_completed_at": completed_at,
        "run_manifest_sha256": hashlib.sha256(run_content).hexdigest(),
        "signal_manifest_sha256": record.manifest_sha256,
        "signal_sha256": record.sha256,
        "signal_rows": record.row_count,
    }
    receipt_content = (
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    receipt_digest = hashlib.sha256(receipt_content).hexdigest()
    staging = record.directory.parent / (
        ".%s.completion.tmp-%s" % (record.version, uuid.uuid4().hex)
    )
    try:
        staging.mkdir(mode=0o700, exist_ok=False)
        _write_exclusive(staging / "receipt.json", receipt_content)
        _write_exclusive(
            staging / "receipt.sha256", (receipt_digest + "\n").encode("ascii")
        )
        _write_exclusive(staging / "run_manifest.json", run_content)
        _fsync_directory(staging)
        if os.path.lexists(str(target)):
            raise SignalVersionConflictError(
                "signal completion receipt already exists: %s" % target
            )
        os.rename(str(staging), str(target))
        _fsync_directory(record.directory)
    finally:
        if staging.exists():
            for name in ("receipt.json", "receipt.sha256", "run_manifest.json"):
                try:
                    (staging / name).unlink()
                except OSError:
                    pass
            try:
                staging.rmdir()
            except OSError:
                pass
    return load_signal_record(record.directory)


def load_signal_record(
    directory: Union[str, os.PathLike],
    *,
    require_completion: bool = True,
) -> SignalRecord:
    """Verify one published signal version before downstream evaluation.

    The manifest sidecar and CSV digest are checked before parsing.  The
    parsed rows are then passed through the same strict canonical validator
    used at publication, preventing a modified but internally inconsistent
    artifact from being accepted by later realized-return jobs.
    """

    path = _safe_signal_path(Path(directory))
    if path.is_symlink() or not path.is_dir():
        raise SignalValidationError("signal record must be a local directory")
    entries = list(path.iterdir())
    base_files = {"signals.csv", "manifest.json", "manifest.sha256"}
    names = {entry.name for entry in entries}
    allowed_base_files = (base_files, base_files.union({"predictions.csv"}))
    if require_completion and (
        "completion" not in names or names.difference({"completion"}) not in allowed_base_files
    ):
        raise SignalValidationError(
            "signal record must contain exactly signals.csv, manifest.json, "
            "manifest.sha256, and a completion receipt"
        )
    if not require_completion and names.difference({"completion"}) not in allowed_base_files:
        raise SignalValidationError(
            "unfinalized signal record must contain exactly its three base files"
        )
    base_entries = [entry for entry in entries if entry.name != "completion"]
    if any(entry.is_symlink() or not entry.is_file() for entry in base_entries):
        raise SignalValidationError("signal record entries must be regular files")
    completion, completion_producer = (
        _validate_completion_receipt(path / "completion")
        if "completion" in names
        else (None, None)
    )
    manifest_content = (path / "manifest.json").read_bytes()
    try:
        manifest_digest = (path / "manifest.sha256").read_text(
            encoding="ascii"
        ).strip()
    except (OSError, UnicodeError) as exc:
        raise SignalValidationError("manifest.sha256 is unreadable") from exc
    manifest_digest = _sha256(manifest_digest, "manifest.sha256")
    if hashlib.sha256(manifest_content).hexdigest() != manifest_digest:
        raise SignalValidationError("signal manifest SHA-256 mismatch")
    try:
        manifest = json.loads(manifest_content.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise SignalValidationError("signal manifest is unreadable") from exc
    required = {
        "artifact_type",
        "manifest_version",
        "live_trading",
        "version",
        "activation_date",
        "signal_date",
        "data_asof",
        "generated_at",
        "model_name",
        "model_version",
        "model_bundle_sha256",
        "dataset_sha256",
        "git_commit",
        "run_id",
        "universe_version",
        "activation_id",
        "activation_manifest_sha256",
        "research_contract_sha256",
        "source_sha256",
        "timeliness",
        "reissue_reason",
        "row_count",
        "columns",
        "signals_file",
        "sha256",
        "files",
    }
    schema_version = manifest.get("manifest_version") if isinstance(manifest, dict) else None
    if type(schema_version) is not int or schema_version not in (2, 3):
        raise SignalValidationError("signal manifest schema is invalid")
    prediction_fields = {
        "prediction_scope", "predictions_file", "prediction_columns",
        "prediction_row_count", "cross_section_size", "prediction_sha256",
    }
    valid_fields = (
        (required, required.union({"prediction_scope"})) if schema_version == 2
        else (required.union(prediction_fields),)
    )
    if (
        set(manifest) not in valid_fields
        or manifest.get("artifact_type") != "qbot-forward-signals"
        or manifest.get("live_trading") is not False
    ):
        raise SignalValidationError("signal manifest schema is invalid")
    full_scope = schema_version == 3
    scope = manifest.get("prediction_scope", TOP_K_ONLY)
    expected_scope = FULL_ELIGIBLE_CROSS_SECTION if full_scope else TOP_K_ONLY
    if scope != expected_scope:
        raise SignalValidationError("signal prediction_scope is invalid")
    declared_base_files = base_files.union({"predictions.csv"}) if full_scope else base_files
    if names.difference({"completion"}) != declared_base_files:
        raise SignalValidationError("signal record must contain exactly its schema-declared files")
    if completion is not None:
        completion_expected = {
            "run_id": manifest.get("run_id"),
            "signal_manifest_sha256": manifest_digest,
            "signal_sha256": manifest.get("sha256"),
            "signal_rows": manifest.get("row_count"),
        }
        completion_mismatches = [
            name
            for name, expected in completion_expected.items()
            if completion.get(name) != expected
        ]
        if completion_mismatches:
            raise SignalValidationError(
                "completion receipt differs from signal record (%s)"
                % ", ".join(completion_mismatches)
            )
    version = _version_name(manifest["version"])
    if path.name != version:
        raise SignalValidationError("signal version directory does not match manifest")
    signal_day, _, generated, _, _ = _validated_temporal_metadata(
        signal_date=manifest["signal_date"], data_asof=manifest["data_asof"],
        generated_at=manifest["generated_at"], activation_date=manifest["activation_date"],
        timeliness=manifest["timeliness"], reissue_reason=manifest["reissue_reason"],
    )
    activation_day = _canonical_date(manifest["activation_date"], "activation_date")
    if path.parent.name != signal_day.isoformat():
        raise SignalValidationError("signal date directory does not match manifest")
    if manifest.get("signals_file") != "signals.csv":
        raise SignalValidationError("signal manifest has an invalid signals_file")
    if manifest.get("columns") != list(FORWARD_SIGNAL_COLUMNS):
        raise SignalValidationError("signal manifest columns are invalid")
    recorded_csv_digest = _sha256(manifest.get("sha256"), "sha256")
    file_entry = manifest.get("files")
    expected_csv_files = {"signals.csv", "predictions.csv"} if full_scope else {"signals.csv"}
    if not isinstance(file_entry, dict) or set(file_entry) != expected_csv_files:
        raise SignalValidationError("signal manifest files inventory is invalid")
    signal_entry = file_entry["signals.csv"]
    if not isinstance(signal_entry, dict) or set(signal_entry) != {"bytes", "sha256"}:
        raise SignalValidationError("signals.csv manifest entry is invalid")
    if _sha256(signal_entry.get("sha256"), "signals.csv sha256") != recorded_csv_digest:
        raise SignalValidationError("signals.csv digests disagree in manifest")
    csv_content = (path / "signals.csv").read_bytes()
    if signal_entry.get("bytes") != len(csv_content):
        raise SignalValidationError("signals.csv byte count mismatch")
    if hashlib.sha256(csv_content).hexdigest() != recorded_csv_digest:
        raise SignalValidationError("signals.csv SHA-256 mismatch")
    try:
        frame = pd.read_csv(
            io.BytesIO(csv_content),
            dtype={"symbol": str},
            keep_default_na=False,
            float_precision="round_trip",
        )
    except (UnicodeError, ValueError, TypeError) as exc:
        raise SignalValidationError("signals.csv is unreadable") from exc
    if list(frame.columns) != list(FORWARD_SIGNAL_COLUMNS):
        raise SignalValidationError("signals.csv columns are invalid")
    if isinstance(manifest.get("row_count"), bool) or not isinstance(
        manifest.get("row_count"), int
    ):
        raise SignalValidationError("signal row_count must be an integer")
    if manifest["row_count"] != len(frame) or len(frame) <= 0:
        raise SignalValidationError("signal row_count does not match signals.csv")
    metadata_columns = FORWARD_SIGNAL_COLUMNS[: FORWARD_SIGNAL_COLUMNS.index("symbol")]
    metadata = {name: manifest[name] for name in metadata_columns}
    expected_activation_id = "%s-%s" % (
        activation_day.isoformat(),
        _sha256(
            manifest["research_contract_sha256"],
            "research_contract_sha256",
        )[:16],
    )
    if manifest["activation_id"] != expected_activation_id:
        raise SignalValidationError(
            "activation_id does not match activation date and research contract"
        )
    canonical = _canonical_signals(frame, metadata, generated)
    canonical_csv = canonical.to_csv(
        index=False,
        lineterminator="\n",
        float_format="%.17g",
    ).encode("utf-8")
    if canonical_csv != csv_content:
        raise SignalValidationError("signals.csv is not in canonical serialized form")
    full_predictions = None
    prediction_digest = None
    if full_scope:
        size = _cross_section_size(manifest.get("cross_section_size"))
        prediction_count = _cross_section_size(manifest.get("prediction_row_count"))
        if prediction_count != size:
            raise SignalValidationError("prediction row count must equal cross_section_size")
        if manifest.get("predictions_file") != "predictions.csv":
            raise SignalValidationError("signal manifest has an invalid predictions_file")
        if manifest.get("prediction_columns") != list(FORWARD_PREDICTION_COLUMNS):
            raise SignalValidationError("signal manifest prediction columns are invalid")
        prediction_digest = _sha256(manifest.get("prediction_sha256"), "prediction_sha256")
        prediction_entry = file_entry["predictions.csv"]
        if not isinstance(prediction_entry, dict) or set(prediction_entry) != {"bytes", "sha256"}:
            raise SignalValidationError("predictions.csv manifest entry is invalid")
        if _sha256(prediction_entry.get("sha256"), "predictions.csv sha256") != prediction_digest:
            raise SignalValidationError("predictions.csv digests disagree in manifest")
        prediction_content = (path / "predictions.csv").read_bytes()
        if type(prediction_entry.get("bytes")) is not int or prediction_entry["bytes"] != len(prediction_content):
            raise SignalValidationError("predictions.csv byte count mismatch")
        if hashlib.sha256(prediction_content).hexdigest() != prediction_digest:
            raise SignalValidationError("predictions.csv SHA-256 mismatch")
        try:
            prediction_frame = pd.read_csv(
                io.BytesIO(prediction_content), dtype={"symbol": str},
                keep_default_na=False, float_precision="round_trip",
            )
        except (UnicodeError, ValueError, TypeError) as exc:
            raise SignalValidationError("predictions.csv is unreadable") from exc
        if list(prediction_frame.columns) != list(FORWARD_PREDICTION_COLUMNS):
            raise SignalValidationError("predictions.csv columns are invalid")
        full_predictions = _canonical_predictions(prediction_frame, metadata, generated, size)
        _validate_prediction_prefix(canonical, full_predictions)
        if full_predictions.to_csv(index=False, lineterminator="\n", float_format="%.17g").encode("utf-8") != prediction_content:
            raise SignalValidationError("predictions.csv is not in canonical serialized form")
    record = SignalRecord(
        signal_date=signal_day,
        version=version,
        directory=path,
        signals_path=path / "signals.csv",
        manifest_path=path / "manifest.json",
        sha256=recorded_csv_digest,
        manifest_sha256=manifest_digest,
        row_count=len(frame),
        predictions_path=path / "predictions.csv" if full_scope else None,
        predictions_sha256=prediction_digest,
        prediction_row_count=len(full_predictions) if full_scope else 0,
        prediction_scope=scope,
        _prediction_frame=full_predictions,
    )
    if completion_producer is not None:
        _validate_prediction_producer(completion_producer, record)
    return record


def load_prediction_record(
    directory: Union[str, os.PathLike], *, require_completion: bool = True
) -> pd.DataFrame:
    """Return verified full predictions; legacy Top-K cannot be rescored later."""

    return load_signal_record(directory, require_completion=require_completion).predictions


__all__ = [
    "FORWARD_SIGNAL_COLUMNS",
    "FORWARD_PREDICTION_COLUMNS",
    "TOP_K_ONLY",
    "FULL_ELIGIBLE_CROSS_SECTION",
    "SignalLockError",
    "SignalLockTimeoutError",
    "SignalRecord",
    "SignalRecorder",
    "SignalRecorderError",
    "SignalValidationError",
    "SignalVersionConflictError",
    "finalize_signal_record",
    "load_signal_record",
    "load_prediction_record",
    "record_signals",
]
