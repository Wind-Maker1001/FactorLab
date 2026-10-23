"""Select contemporaneous full forward scores without observing outcomes.

FIRST_ON_TIME is a fixed evidence rule, not a strategy optimization. All
in-range versions must first be intact, producer-completed records of the
same registered experiment. Legacy Top-K records and late reissues remain
visible exclusions; they never acquire retrospectively regenerated scores.
Local hashes detect accidental edits, not an externally notarized timestamp.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Union
from zoneinfo import ZoneInfo

import pandas as pd

from qbot.research.errors import DataQualityError
from qbot.research.forward.integrity import (
    ForwardIntegrityError, _read_regular_file, assert_no_link_components,
)
from qbot.research.forward.signal_recorder import (
    FULL_ELIGIBLE_CROSS_SECTION, TOP_K_ONLY, SignalRecorderError,
    _aware_datetime, _canonical_date, load_signal_record,
)


FIRST_ON_TIME = "FIRST_ON_TIME"
SELECTION_COLUMNS = (
    "signal_date", "directory", "version", "generated_at", "generated_at_utc",
    "signal_manifest_sha256", "signal_sha256", "prediction_sha256", "prediction_rows",
    "signal_rows", "prediction_scope", "model_name", "model_version", "model_bundle_sha256",
    "activation_id", "activation_manifest_sha256", "research_contract_sha256", "source_sha256",
    "dataset_sha256", "universe_version", "git_commit", "run_id", "data_asof", "version_policy",
)
_IDENTITY_FIELDS = (
    "activation_id", "activation_manifest_sha256", "research_contract_sha256", "source_sha256",
    "model_name", "model_version", "model_bundle_sha256",
)
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_LOCK = re.compile(r"^\.([0-9]{4}-[0-9]{2}-[0-9]{2})\.signal\.lock$")
_SEQUENTIAL = re.compile(r"^v([0-9]{6,})$")


class ForwardEvidenceError(DataQualityError):
    """The requested forward evidence is missing, ambiguous or inconsistent."""


@dataclass(frozen=True, init=False, eq=False)
class ForwardPredictionEvidence:
    """Immutable selection plan with defensive copies of each scalar table."""

    _predictions: pd.DataFrame = field(repr=False, compare=False)
    _selected: pd.DataFrame = field(repr=False, compare=False)
    _excluded: pd.DataFrame = field(repr=False, compare=False)

    def __init__(self, predictions: pd.DataFrame, selected: pd.DataFrame, excluded: pd.DataFrame) -> None:
        object.__setattr__(self, "_predictions", predictions.copy(deep=True))
        object.__setattr__(self, "_selected", selected.copy(deep=True))
        object.__setattr__(self, "_excluded", excluded.copy(deep=True))

    @property
    def predictions(self) -> pd.DataFrame:
        return self._predictions.copy(deep=True)

    @property
    def selected(self) -> pd.DataFrame:
        return self._selected.copy(deep=True)

    @property
    def excluded(self) -> pd.DataFrame:
        return self._excluded.copy(deep=True)


def _text(value: Any, name: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 512 or any(ord(character) < 32 for character in value)):
        raise ForwardEvidenceError("%s must be canonical nonempty text" % name)
    return value


def _hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ForwardEvidenceError("%s must be a canonical lowercase SHA-256" % name)
    return value


def _day(value: Any, name: str) -> date:
    try:
        return _canonical_date(value, name)
    except SignalRecorderError as exc:
        raise ForwardEvidenceError(str(exc)) from exc


def _safe(path: Path) -> Path:
    try:
        return assert_no_link_components(path)
    except ForwardIntegrityError as exc:
        raise ForwardEvidenceError(str(exc)) from exc


def _legacy_metadata(record: Any) -> Dict[str, Any]:
    # Legacy records cannot return a full prediction frame. Re-read only the
    # manifest, requiring the exact hash pinned by the completed-record reader.
    # No scores, model replay or mutable CSV reread is involved.
    try:
        content = _read_regular_file(record.manifest_path)
        if hashlib.sha256(content).hexdigest() != record.manifest_sha256:
            raise ForwardEvidenceError("Legacy signal manifest changed after verification")
        return json.loads(content.decode("utf-8"))
    except (ForwardIntegrityError, OSError, UnicodeError, ValueError) as exc:
        raise ForwardEvidenceError("Legacy signal manifest is unavailable or invalid") from exc


def _entry(record: Any, metadata: Dict[str, Any], generated: datetime) -> Dict[str, Any]:
    return {
        "signal_date": record.signal_date.isoformat(), "directory": str(record.directory),
        "version": record.version, "generated_at": metadata["generated_at"],
        "generated_at_utc": generated.astimezone(timezone.utc).isoformat(),
        "signal_manifest_sha256": record.manifest_sha256, "signal_sha256": record.sha256,
        "prediction_sha256": record.predictions_sha256, "prediction_rows": record.prediction_row_count,
        "signal_rows": record.row_count, "prediction_scope": record.prediction_scope,
        **{name: metadata[name] for name in _IDENTITY_FIELDS},
        **{name: metadata[name] for name in ("dataset_sha256", "universe_version", "git_commit", "run_id", "data_asof")},
        "version_policy": FIRST_ON_TIME,
    }


def _sequential_number(version: str) -> int:
    match = _SEQUENTIAL.fullmatch(version)
    if match is None:
        raise ForwardEvidenceError("Same-time ON_TIME versions are ambiguous unless all are canonical sequential versions")
    number = int(match.group(1))
    if number <= 0 or version != "v%06d" % number:
        raise ForwardEvidenceError("Same-time ON_TIME version must be a canonical positive sequential version")
    return number


def select_forward_prediction_records(
    signals_root: Union[str, Path], *, start: Any, end: Any,
    activation_id: str, activation_manifest_sha256: str, research_contract_sha256: str,
    source_sha256: str, model_name: str, model_version: str, model_bundle_sha256: str,
    market_close_hour: int = 15, market_close_minute: int = 30,
    version_policy: str = FIRST_ON_TIME,
) -> ForwardPredictionEvidence:
    """Select the earliest completed Shanghai ON_TIME version of each date.

    Date gaps are not silently invented: calendar coverage belongs to the
    later report. Identity mismatches or damaged/orphan versions fail the
    whole selection, including versions that would otherwise lose a tie.
    There is intentionally no label, return, model, or provider argument.
    """
    first, last = _day(start, "start"), _day(end, "end")
    if first > last:
        raise ForwardEvidenceError("start must not follow end")
    if version_policy != FIRST_ON_TIME:
        raise ForwardEvidenceError("Only the pre-registered FIRST_ON_TIME version_policy is supported")
    if type(market_close_hour) is not int or type(market_close_minute) is not int:
        raise ForwardEvidenceError("Market close hour/minute must be integers")
    try:
        close = time(market_close_hour, market_close_minute)
    except ValueError as exc:
        raise ForwardEvidenceError("Invalid market close time") from exc
    expected = {
        "activation_id": _text(activation_id, "activation_id"),
        "activation_manifest_sha256": _hash(activation_manifest_sha256, "activation_manifest_sha256"),
        "research_contract_sha256": _hash(research_contract_sha256, "research_contract_sha256"),
        "source_sha256": _hash(source_sha256, "source_sha256"),
        "model_name": _text(model_name, "model_name"), "model_version": _text(model_version, "model_version"),
        "model_bundle_sha256": _hash(model_bundle_sha256, "model_bundle_sha256"),
    }
    match = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2})-([0-9a-f]{16})", activation_id)
    if match is None or match.group(2) != research_contract_sha256[:16]:
        raise ForwardEvidenceError("activation_id must bind its canonical date and research contract")
    _day(match.group(1), "activation_id date")
    root = _safe(Path(signals_root))
    if not root.is_dir():
        raise ForwardEvidenceError("Forward signal evidence root is missing or not a directory")
    candidates: Dict[date, List[Dict[str, Any]]] = {}
    excluded = []
    market_zone = ZoneInfo("Asia/Shanghai")
    try:
        date_paths = sorted(root.iterdir(), key=lambda path: path.name)
        for date_path in date_paths:
            _safe(date_path)
            lock = _LOCK.fullmatch(date_path.name)
            if lock is not None:
                lock_day = _day(lock.group(1), "signal lock date")
                if first <= lock_day <= last:
                    raise ForwardEvidenceError("In-range publication lock is present; inspect active/orphan signal publication")
                continue
            if not _DATE.fullmatch(date_path.name) or not date_path.is_dir():
                raise ForwardEvidenceError("Unknown artifact at dedicated forward signal root: %s" % date_path.name)
            day = _day(date_path.name, "signal directory date")
            if not first <= day <= last:
                continue
            versions = sorted(date_path.iterdir(), key=lambda path: path.name)
            if not versions:
                raise ForwardEvidenceError("In-range signal date directory has no complete versions: %s" % day)
            for directory in versions:
                _safe(directory)
                if directory.name.startswith(".") or not directory.is_dir():
                    raise ForwardEvidenceError("Unknown/orphan artifact in signal-date directory: %s" % directory.name)
                record = load_signal_record(directory)  # Completion required, no fallback.
                predictions = record.predictions if record.prediction_scope == FULL_ELIGIBLE_CROSS_SECTION else None
                metadata = predictions.iloc[0].to_dict() if predictions is not None else _legacy_metadata(record)
                mismatches = [name for name, value in expected.items() if metadata.get(name) != value]
                if mismatches:
                    raise ForwardEvidenceError("Mixed forward experiment identity (%s): %s" % (", ".join(mismatches), directory))
                generated = _aware_datetime(metadata["generated_at"], "generated_at")
                row = _entry(record, metadata, generated)
                if record.prediction_scope == TOP_K_ONLY:
                    excluded.append(dict(row, reason=TOP_K_ONLY))
                    continue
                if metadata["timeliness"] == "LATE_REISSUE":
                    excluded.append(dict(row, reason="LATE_REISSUE"))
                    continue
                market_time = generated.astimezone(market_zone)
                if market_time.date() != day or market_time.time().replace(tzinfo=None) < close:
                    raise ForwardEvidenceError("ON_TIME record is not same-day after configured Shanghai market close: %s" % directory)
                candidates.setdefault(day, []).append({"row": row, "time": generated.astimezone(timezone.utc), "predictions": predictions})
    except (OSError, SignalRecorderError) as exc:
        raise ForwardEvidenceError("Cannot verify complete forward evidence: %s" % exc) from exc
    selected_rows, frames = [], []
    for day in sorted(candidates):
        versions = candidates[day]
        earliest = min(item["time"] for item in versions)
        tied = [item for item in versions if item["time"] == earliest]
        if len(tied) > 1:
            chosen = min(tied, key=lambda item: _sequential_number(item["row"]["version"]))
        else:
            chosen = tied[0]
        selected_rows.append(chosen["row"])
        frame = chosen["predictions"].copy(deep=True)
        frame["signal_version"] = chosen["row"]["version"]
        frame["signal_manifest_sha256"] = chosen["row"]["signal_manifest_sha256"]
        frames.append(frame)
        excluded.extend(dict(item["row"], reason="NOT_FIRST_ON_TIME") for item in versions if item is not chosen)
    if not frames:
        raise ForwardEvidenceError("No eligible completed full ON_TIME prediction records in requested range; no empty IC result is permitted")
    selected = pd.DataFrame(selected_rows, columns=SELECTION_COLUMNS)
    excluded_frame = pd.DataFrame(excluded, columns=SELECTION_COLUMNS + ("reason",))
    if not excluded_frame.empty:
        excluded_frame = excluded_frame.sort_values(["signal_date", "generated_at_utc", "version"], kind="mergesort").reset_index(drop=True)
    return ForwardPredictionEvidence(pd.concat(frames, ignore_index=True), selected, excluded_frame)


__all__ = ["FIRST_ON_TIME", "ForwardEvidenceError", "ForwardPredictionEvidence", "SELECTION_COLUMNS", "select_forward_prediction_records"]
