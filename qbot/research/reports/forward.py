"""Append-only, headless reports for already-recorded forward predictions.

Reports describe realised labels, not paper executions or a portfolio NAV.
Publishing a report and certifying its successful producer are separate steps.
An uncompleted report is an orphan and is rejected by the default reader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Dict, Mapping, Optional, Tuple
import uuid

import matplotlib

matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from qbot.research.errors import FactorLabError
from qbot.research.forward.integrity import (
    _read_regular_file,
    assert_no_link_components,
    workflow_lock,
)


VERSION_POLICY = "FIRST_ON_TIME"
_PAIRED_TABLES = ("forward_predictions", "prediction_daily", "prediction_summary")
_CSV_TABLES = ("selected_versions", "excluded_versions")
_PLOTS = ("prediction_ic.png", "rank_ic.png", "coverage.png", "regime_performance.png")
_ARTIFACT_NAMES = frozenset(
    [name + suffix for name in _PAIRED_TABLES for suffix in (".csv", ".parquet")]
    + [name + ".csv" for name in _CSV_TABLES] + ["summary.md"] + list(_PLOTS)
)
_MANIFEST_KEYS = frozenset({
    "schema_version", "artifact_type", "run_id", "as_of", "version_policy",
    "evaluation_scope", "return_semantics", "provenance", "tables", "artifact_sha256",
})
_RETURN_SEMANTICS = "OPEN_TO_OPEN_EXCESS_LABELS_BEFORE_TRANSACTION_COSTS_NOT_PORTFOLIO_RETURNS"


class ForwardEvaluationReportError(FactorLabError):
    """Invalid report content, unsafe paths or absent completion evidence."""


@dataclass(frozen=True)
class ForwardEvaluationReport:
    directory: Path
    manifest_path: Path
    manifest_sha256: str
    _artifact_sha256: Dict[str, str] = field(repr=False)
    _manifest: Dict[str, Any] = field(repr=False)

    @property
    def artifact_sha256(self) -> Dict[str, str]:
        return dict(self._artifact_sha256)

    @property
    def manifest(self) -> Dict[str, Any]:
        return json.loads(_json_bytes(self._manifest))

    @property
    def completion_path(self) -> Path:
        return self.directory / "completion"


def _safe(path: Path) -> Path:
    try:
        return assert_no_link_components(Path(path))
    except FactorLabError as exc:
        raise ForwardEvaluationReportError(str(exc)) from exc


def _read(path: Path) -> bytes:
    try:
        return _read_regular_file(path)
    except FactorLabError as exc:
        raise ForwardEvaluationReportError(str(exc)) from exc


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _json_bytes(value: object) -> bytes:
    try:
        return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ForwardEvaluationReportError("Report metadata must be finite JSON data") from exc


def _json_object(content: bytes, name: str) -> Dict[str, Any]:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError("nonfinite JSON value")

    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=unique_pairs, parse_constant=nonfinite)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        # A valid JSON numeral such as 1e999 can overflow the Python decoder
        # without invoking parse_constant. Reject that nonfinite result too.
        json.dumps(value, allow_nan=False)
        return value
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ForwardEvaluationReportError("%s is not a strict JSON object" % name) from exc


def _run_id(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}", value) is None:
        raise ForwardEvaluationReportError("run_id must be a safe nonempty identifier")
    return value


def _date(value: object) -> str:
    if not isinstance(value, str):
        raise ForwardEvaluationReportError("as_of must be canonical YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ForwardEvaluationReportError("as_of must be canonical YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ForwardEvaluationReportError("as_of must be canonical YYYY-MM-DD")
    return value


def _aware_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ForwardEvaluationReportError("Producer completed_at must be timezone-aware")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("missing timezone")
    except ValueError as exc:
        raise ForwardEvaluationReportError("Producer completed_at must be timezone-aware") from exc
    return value


def _frame(value: object, name: str) -> pd.DataFrame:
    if not isinstance(value, pd.DataFrame) or len(value.columns) == 0:
        raise ForwardEvaluationReportError("%s must be a DataFrame with declared columns" % name)
    if not value.columns.is_unique or any(not isinstance(item, str) or not item for item in value.columns):
        raise ForwardEvaluationReportError("%s has invalid or duplicate columns" % name)
    result = value.copy(deep=True).reset_index(drop=True)
    for column in result:
        series = result[column]
        if pd.api.types.is_complex_dtype(series.dtype):
            raise ForwardEvaluationReportError("%s contains complex values" % name)
        if pd.api.types.is_numeric_dtype(series.dtype):
            values = series.to_numpy(dtype=float, na_value=np.nan)
            if np.isinf(values).any():
                raise ForwardEvaluationReportError("%s contains infinite values" % name)
        elif series.dtype == object:
            for value in series.dropna():
                if not isinstance(value, (str, bool, int, float, date, datetime, np.number, pd.Timestamp)):
                    raise ForwardEvaluationReportError("%s contains non-scalar object values" % name)
                if isinstance(value, (float, np.floating)) and np.isinf(value):
                    raise ForwardEvaluationReportError("%s contains infinite values" % name)
    return result


def _csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False, float_format="%.17g", lineterminator="\n").encode("utf-8")


def _parquet_bytes(frame: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    return buffer.getvalue()


def _table_schema(frame: pd.DataFrame, name: str, paired: bool) -> Dict[str, Any]:
    return {
        "rows": int(len(frame)), "columns": list(frame.columns),
        "dtypes": [str(dtype) for dtype in frame.dtypes],
        "csv_file": name + ".csv", "parquet_file": name + ".parquet" if paired else None,
    }


def _check_full_predictions(predictions: pd.DataFrame, original: pd.DataFrame) -> None:
    if len(predictions) != len(original) or not set(original.columns).issubset(predictions):
        raise ForwardEvaluationReportError("Evaluation must retain the complete recorded prediction cross-section")
    required = {"signal_date", "symbol", "score", "rank"}
    if not required.issubset(original):
        raise ForwardEvaluationReportError("Recorded predictions lack date, symbol, score or rank")
    if "timeliness" in original and not original["timeliness"].eq("ON_TIME").all():
        raise ForwardEvaluationReportError("Forward evaluation requires original ON_TIME predictions")
    left = predictions[list(original.columns)].copy()
    right = original.copy()
    for frame in (left, right):
        for column in ("signal_date", "data_asof"):
            if column in frame:
                try:
                    values = pd.to_datetime(frame[column], errors="raise")
                    if values.isna().any() or getattr(values.dt, "tz", None) is not None or not values.eq(values.dt.normalize()).all():
                        raise ValueError("not date-only")
                    frame[column] = values.dt.strftime("%Y-%m-%d")
                except (ValueError, TypeError, AttributeError) as exc:
                    raise ForwardEvaluationReportError("Prediction dates must remain date-only") from exc
        if frame.duplicated(["signal_date", "symbol"]).any():
            raise ForwardEvaluationReportError("Duplicate recorded prediction keys")
    left = left.sort_values(["signal_date", "symbol"], kind="mergesort").reset_index(drop=True)
    right = right.sort_values(["signal_date", "symbol"], kind="mergesort").reset_index(drop=True)
    try:
        pd.testing.assert_frame_equal(left, right, check_dtype=False, check_exact=True)
    except AssertionError as exc:
        raise ForwardEvaluationReportError("Evaluation altered recorded prediction values or omitted observations") from exc


def _placeholder(axis, title: str) -> None:
    axis.set_title(title)
    axis.text(0.5, 0.5, "Insufficient mature data (NaN)", ha="center", va="center", transform=axis.transAxes)
    axis.set_xticks([])
    axis.set_yticks([])


def _figure_bytes(daily: pd.DataFrame, summary: pd.DataFrame, name: str) -> bytes:
    figure, axis = plt.subplots(figsize=(9, 4.5))
    try:
        if name == "regime_performance.png":
            rows = summary.loc[summary["scope"].eq("REGIME")].copy() if "scope" in summary else summary.iloc[:0]
            values = pd.to_numeric(rows["prediction_rank_ic_mean"], errors="raise") if "prediction_rank_ic_mean" in rows else pd.Series(dtype=float)
            valid = values.notna()
            if valid.any() and "period" in rows:
                axis.bar(rows.loc[valid, "period"].astype(str), values.loc[valid])
                axis.set_title("Recorded forward RankIC by market regime")
                axis.set_ylabel("Mean RankIC")
            else:
                _placeholder(axis, "Forward regime diagnostics")
        else:
            columns = {
                "prediction_ic.png": ("prediction_ic",),
                "rank_ic.png": ("prediction_rank_ic",),
                "coverage.png": ("coverage", "top_label_coverage", "bottom_label_coverage"),
            }[name]
            x_values = pd.to_datetime(daily["date"], errors="raise") if "date" in daily else np.arange(len(daily))
            plotted = False
            for column in columns:
                if column in daily:
                    values = pd.to_numeric(daily[column], errors="raise")
                    if values.notna().any():
                        axis.plot(x_values, values, label=column)
                        plotted = True
            if plotted:
                axis.set_title("Forward %s" % name.removesuffix(".png"))
                axis.legend()
                axis.grid(alpha=0.2)
                if "date" in daily:
                    figure.autofmt_xdate(rotation=30, ha="right")
            else:
                _placeholder(axis, "Forward %s" % name.removesuffix(".png"))
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", dpi=120, bbox_inches="tight", metadata={"Software": "Qbot FactorLab"})
        return buffer.getvalue()
    finally:
        plt.close(figure)


def _summary_text(predictions, daily, summary, selected, excluded, as_of, provenance) -> str:
    statuses = predictions["label_status"].value_counts(dropna=False).to_dict() if "label_status" in predictions else {}
    status_lines = ["- %s: %d" % (name, count) for name, count in sorted(statuses.items(), key=lambda item: str(item[0]))]
    metric_text = summary.to_csv(index=False, float_format="%.17g", lineterminator="\n")
    missing = provenance.get("missing_signal_dates", [])
    expected = provenance.get("expected_signal_dates", [])
    synthetic = (
        any(provenance.get(name) is True for name in ("synthetic", "software_demo_only", "synthetic_for_testing_only"))
        or provenance.get("research_result") is False
    )
    warning = (
        "> SYNTHETIC SOFTWARE DEMO ONLY - NOT A RESEARCH RESULT. "
        "Artificial inputs do not demonstrate real forward prediction performance.\n\n"
        if synthetic else ""
    )
    return (
        "# Forward prediction evaluation\n\n"
        + warning + "As of: %s\n\n" % as_of
        + "Selection policy: FIRST_ON_TIME; only original ON_TIME full-cross-sectional predictions.\n"
        + "No retrospective rescoring, late-reissue replacement or Top-K-only IC.\n\n"
        + "Top/Bottom future returns are open-to-open excess-return labels before transaction costs, "
        + "not realised portfolio returns. No orders, execution backtest, NAV or live money are represented.\n"
        + "IMMATURE and MATURE_MISSING_PRICE observations retain NaN labels; missing data are never zero returns.\n\n"
        + "Recorded predictions: %d; selected versions: %d; excluded versions: %d.\n\n" % (len(predictions), len(selected), len(excluded))
        + "Expected signal dates: %s\n\nMissing signal dates: %s\n\n" % (json.dumps(expected, ensure_ascii=False), json.dumps(missing, ensure_ascii=False))
        + "## Label status and coverage\n\n" + ("\n".join(status_lines) if status_lines else "Insufficient observations; label statistics are NaN.")
        + "\n\nDaily coverage, scored observations and mature-label counts are retained in prediction_daily.csv.\n"
        + "Source hashes establish integrity and reproducibility, not independently authoritative market truth.\n\n"
        + "## Prediction diagnostics\n\n```csv\n" + metric_text + "```\n"
    )


def _identity(path: Path) -> Tuple[int, int]:
    metadata = os.lstat(str(path))
    return metadata.st_dev, metadata.st_ino


def _write_owned(path: Path, content: bytes, owned: list) -> None:
    _safe(path)
    with path.open("xb") as handle:
        owned.append((path, _identity(path)))
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _cleanup_owned(stage: Path, identity: Optional[Tuple[int, int]], owned: list) -> None:
    if identity is None:
        return
    try:
        if _safe(stage) != stage or _identity(stage) != identity:
            return
        for path, expected in reversed(owned):
            try:
                checked = _safe(path)
                if checked.parent == stage and _identity(path) == expected and stat.S_ISREG(os.lstat(str(path)).st_mode):
                    path.unlink()
            except (OSError, FactorLabError):
                pass
        stage.rmdir()  # Unknown files are never removed recursively.
    except (OSError, FactorLabError):
        pass


def _publish_bytes(target: Path, files: Mapping[str, bytes], kind: str) -> None:
    target = _safe(target)
    if os.path.lexists(str(target)):
        raise ForwardEvaluationReportError("Immutable %s already exists: %s" % (kind, target))
    target.parent.mkdir(parents=True, exist_ok=True)
    _safe(target.parent)
    stage = target.parent / (".%s.%s.tmp-%s" % (target.name, kind, uuid.uuid4().hex))
    owned, identity, published = [], None, False
    try:
        stage.mkdir(mode=0o700, exist_ok=False)
        identity = _identity(stage)
        for name, content in files.items():
            _write_owned(stage / name, content, owned)
        if os.path.lexists(str(target)):
            raise ForwardEvaluationReportError("Immutable %s already exists: %s" % (kind, target))
        _safe(target)
        os.rename(str(stage), str(target))
        published = True
    except OSError as exc:
        raise ForwardEvaluationReportError("Cannot publish immutable %s" % kind) from exc
    finally:
        if not published:
            _cleanup_owned(stage, identity, owned)


def write_forward_evaluation_report(evaluation: Any, evidence: Any, output_dir: Path, *, run_id: str, provenance: dict) -> ForwardEvaluationReport:
    """Publish a new report; default readers require its later completion receipt."""
    identifier = _run_id(run_id)
    target = _safe(output_dir)
    if target.name != identifier:
        raise ForwardEvaluationReportError("Report directory must use run_id as its name")
    if os.path.lexists(str(target)):
        raise ForwardEvaluationReportError("Immutable report already exists: %s" % target)
    as_of = _date(evaluation.as_of)
    if not isinstance(provenance, dict):
        raise ForwardEvaluationReportError("provenance must be a JSON mapping")
    metadata = _json_object(_json_bytes(provenance), "provenance")
    if metadata.get("version_policy", VERSION_POLICY) != VERSION_POLICY or metadata.get("as_of", as_of) != as_of:
        raise ForwardEvaluationReportError("Provenance selection policy or as_of differs")
    frames = {
        "forward_predictions": _frame(evaluation.predictions, "forward_predictions"),
        "prediction_daily": _frame(evaluation.daily, "prediction_daily"),
        "prediction_summary": _frame(evaluation.summary, "prediction_summary"),
        "selected_versions": _frame(evidence.selected, "selected_versions"),
        "excluded_versions": _frame(evidence.excluded, "excluded_versions"),
    }
    _check_full_predictions(frames["forward_predictions"], _frame(evidence.predictions, "recorded predictions"))
    for name in _CSV_TABLES:
        if "version_policy" in frames[name] and not frames[name]["version_policy"].eq(VERSION_POLICY).all():
            raise ForwardEvaluationReportError("Evidence has an invalid version selection policy")
    if "prediction_scope" in frames["selected_versions"] and not frames["selected_versions"]["prediction_scope"].eq("FULL_ELIGIBLE_CROSS_SECTION").all():
        raise ForwardEvaluationReportError("Selected evidence must contain full-cross-sectional predictions")
    files = {}
    schemas = {}
    for name, frame in frames.items():
        files[name + ".csv"] = _csv_bytes(frame)
        paired = name in _PAIRED_TABLES
        if paired:
            files[name + ".parquet"] = _parquet_bytes(frame)
        schemas[name] = _table_schema(frame, name, paired)
        _validate_table(schemas[name], files, name)
    files["summary.md"] = _summary_text(*[frames[name] for name in ("forward_predictions", "prediction_daily", "prediction_summary", "selected_versions", "excluded_versions")], as_of, metadata).encode("utf-8")
    for name in _PLOTS:
        files[name] = _figure_bytes(frames["prediction_daily"], frames["prediction_summary"], name)
    manifest = {
        "schema_version": 1, "artifact_type": "qbot-forward-evaluation-report",
        "run_id": identifier, "as_of": as_of, "version_policy": VERSION_POLICY,
        "evaluation_scope": "RECORDED_ON_TIME_FULL_ELIGIBLE_CROSS_SECTION",
        "return_semantics": _RETURN_SEMANTICS, "provenance": metadata,
        "tables": schemas, "artifact_sha256": {name: _sha(content) for name, content in sorted(files.items())},
    }
    content = _json_bytes(manifest)
    files["manifest.json"] = content
    files["manifest.sha256"] = (_sha(content) + "\n").encode("ascii")
    try:
        with workflow_lock(target.parent / (".%s.report.lock" % identifier)):
            _publish_bytes(target, files, "report")
    except FactorLabError as exc:
        if isinstance(exc, ForwardEvaluationReportError):
            raise
        raise ForwardEvaluationReportError(str(exc)) from exc
    except OSError as exc:
        raise ForwardEvaluationReportError("Cannot create forward evaluation report paths") from exc
    return load_forward_evaluation_report(target, require_completion=False)


def _validate_producer(producer: dict, record: ForwardEvaluationReport) -> str:
    expected = {
        "command": "evaluate-forward", "status": "success", "run_id": record.manifest["run_id"],
        "forward_evaluation_manifest_sha256": record.manifest_sha256,
        "forward_evaluation_artifact_sha256": record.artifact_sha256,
    }
    if any(producer.get(name) != value for name, value in expected.items()):
        raise ForwardEvaluationReportError("Producer run does not authorize forward evaluation completion")
    if not isinstance(producer.get("outputs"), dict) or producer["outputs"].get("forward_evaluation_report") != str(record.directory):
        raise ForwardEvaluationReportError("Producer output does not identify this forward evaluation report")
    return _aware_timestamp(producer.get("completed_at"))


def finalize_forward_evaluation_report(directory: Path, run_manifest_path: Path) -> ForwardEvaluationReport:
    """Append immutable proof from one read of the successful producer JSON."""
    record = load_forward_evaluation_report(directory, require_completion=False)
    if os.path.lexists(str(record.completion_path)):
        raise ForwardEvaluationReportError("Immutable completion receipt already exists")
    producer_content = _read(Path(run_manifest_path))
    producer = _json_object(producer_content, "producer run manifest")
    completed_at = _validate_producer(producer, record)
    receipt = {
        "receipt_version": 1, "artifact_type": "qbot-forward-evaluation-completion",
        "run_id": record.manifest["run_id"], "run_completed_at": completed_at,
        "run_manifest_sha256": _sha(producer_content),
        "report_manifest_sha256": record.manifest_sha256,
        "artifact_sha256": record.artifact_sha256,
    }
    content = _json_bytes(receipt)
    files = {"receipt.json": content, "receipt.sha256": (_sha(content) + "\n").encode("ascii"), "run_manifest.json": producer_content}
    try:
        with workflow_lock(record.directory.parent / (".%s.completion.lock" % record.manifest["run_id"])):
            _publish_bytes(record.completion_path, files, "completion")
    except FactorLabError as exc:
        if isinstance(exc, ForwardEvaluationReportError):
            raise
        raise ForwardEvaluationReportError(str(exc)) from exc
    except OSError as exc:
        raise ForwardEvaluationReportError("Cannot create forward evaluation completion paths") from exc
    return load_forward_evaluation_report(record.directory)


def _read_completion(record: ForwardEvaluationReport) -> None:
    path = _safe(record.completion_path)
    if not path.is_dir() or {item.name for item in path.iterdir()} != {"receipt.json", "receipt.sha256", "run_manifest.json"}:
        raise ForwardEvaluationReportError("Completion receipt inventory is invalid")
    content = _read(path / "receipt.json")
    if _read(path / "receipt.sha256") != (_sha(content) + "\n").encode("ascii"):
        raise ForwardEvaluationReportError("Completion receipt SHA-256 mismatch")
    receipt = _json_object(content, "completion receipt")
    required = {"receipt_version", "artifact_type", "run_id", "run_completed_at", "run_manifest_sha256", "report_manifest_sha256", "artifact_sha256"}
    if set(receipt) != required or type(receipt["receipt_version"]) is not int or receipt["receipt_version"] != 1 or receipt["artifact_type"] != "qbot-forward-evaluation-completion":
        raise ForwardEvaluationReportError("Completion receipt schema is invalid")
    producer_content = _read(path / "run_manifest.json")
    if not _is_sha(receipt["run_manifest_sha256"]) or _sha(producer_content) != receipt["run_manifest_sha256"]:
        raise ForwardEvaluationReportError("Completion producer snapshot SHA-256 mismatch")
    producer = _json_object(producer_content, "completion producer snapshot")
    completed_at = _validate_producer(producer, record)
    if (receipt["run_id"] != record.manifest["run_id"] or receipt["report_manifest_sha256"] != record.manifest_sha256
            or receipt["artifact_sha256"] != record.artifact_sha256 or receipt["run_completed_at"] != completed_at):
        raise ForwardEvaluationReportError("Completion receipt differs from the report or producer")


def _validate_table(content: dict, files: Mapping[str, bytes], name: str) -> None:
    paired = name in _PAIRED_TABLES
    keys = {"rows", "columns", "dtypes", "csv_file", "parquet_file"}
    if (not isinstance(content, dict) or set(content) != keys
            or type(content["rows"]) is not int or content["rows"] < 0
            or not isinstance(content["columns"], list) or not content["columns"]
            or any(not isinstance(value, str) or not value for value in content["columns"])
            or len(set(content["columns"])) != len(content["columns"])
            or not isinstance(content["dtypes"], list) or len(content["dtypes"]) != len(content["columns"])
            or any(not isinstance(value, str) or not value for value in content["dtypes"])
            or content["csv_file"] != name + ".csv"
            or content["parquet_file"] != (name + ".parquet" if paired else None)):
        raise ForwardEvaluationReportError("Report table schema is invalid: %s" % name)
    try:
        csv = pd.read_csv(io.BytesIO(files[name + ".csv"]), dtype=str, keep_default_na=False)
        if len(csv) != content["rows"] or list(csv.columns) != content["columns"]:
            raise ValueError("CSV schema differs")
        if paired:
            parquet = _frame(pd.read_parquet(io.BytesIO(files[name + ".parquet"])), name)
            if _table_schema(parquet, name, True) != content or _csv_bytes(parquet) != files[name + ".csv"]:
                raise ValueError("Parquet/CSV values or schemas differ")
    except (ValueError, TypeError, OSError, KeyError) as exc:
        raise ForwardEvaluationReportError("Report table bytes differ from schema: %s" % name) from exc


def load_forward_evaluation_report(directory: Path, *, require_completion: bool = True) -> ForwardEvaluationReport:
    """Verify the exact inventory, pinned bytes/schema and success snapshot."""
    if type(require_completion) is not bool:
        raise ForwardEvaluationReportError("require_completion must be boolean")
    path = _safe(directory)
    if not path.is_dir():
        raise ForwardEvaluationReportError("Forward evaluation report must be a local directory")
    names = {entry.name for entry in path.iterdir()}
    base = _ARTIFACT_NAMES | {"manifest.json", "manifest.sha256"}
    if names.difference({"completion"}) != base:
        raise ForwardEvaluationReportError("Forward evaluation report inventory is invalid")
    if require_completion and "completion" not in names:
        raise ForwardEvaluationReportError("Forward evaluation report has no successful completion receipt (orphan)")
    content = _read(path / "manifest.json")
    digest = _sha(content)
    if _read(path / "manifest.sha256") != (digest + "\n").encode("ascii"):
        raise ForwardEvaluationReportError("Forward evaluation manifest SHA-256 mismatch")
    manifest = _json_object(content, "forward evaluation manifest")
    if (set(manifest) != _MANIFEST_KEYS or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["artifact_type"] != "qbot-forward-evaluation-report" or manifest["version_policy"] != VERSION_POLICY
            or manifest["evaluation_scope"] != "RECORDED_ON_TIME_FULL_ELIGIBLE_CROSS_SECTION"
            or manifest["return_semantics"] != _RETURN_SEMANTICS or not isinstance(manifest["provenance"], dict)):
        raise ForwardEvaluationReportError("Forward evaluation manifest schema is invalid")
    if _run_id(manifest["run_id"]) != path.name:
        raise ForwardEvaluationReportError("Report run_id differs from its directory")
    _date(manifest["as_of"])
    if manifest["provenance"].get("version_policy", VERSION_POLICY) != VERSION_POLICY or manifest["provenance"].get("as_of", manifest["as_of"]) != manifest["as_of"]:
        raise ForwardEvaluationReportError("Report provenance differs from its selection/as_of")
    artifacts = manifest["artifact_sha256"]
    if not isinstance(artifacts, dict) or set(artifacts) != _ARTIFACT_NAMES or not all(_is_sha(value) for value in artifacts.values()):
        raise ForwardEvaluationReportError("Report artifact SHA-256 map is invalid")
    files = {name: _read(path / name) for name in sorted(_ARTIFACT_NAMES)}
    if any(_sha(files[name]) != artifacts[name] for name in files):
        raise ForwardEvaluationReportError("Report artifact SHA-256 mismatch")
    tables = manifest["tables"]
    if not isinstance(tables, dict) or set(tables) != set(_PAIRED_TABLES + _CSV_TABLES):
        raise ForwardEvaluationReportError("Report table inventory is invalid")
    for name, schema in tables.items():
        _validate_table(schema, files, name)
    for name in _PLOTS:
        if not files[name].startswith(b"\x89PNG\r\n\x1a\n"):
            raise ForwardEvaluationReportError("Report figure is not a PNG")
    try:
        files["summary.md"].decode("utf-8")
    except UnicodeError as exc:
        raise ForwardEvaluationReportError("Report summary is not UTF-8") from exc
    record = ForwardEvaluationReport(path, path / "manifest.json", digest, dict(artifacts), manifest)
    if "completion" in names:
        _read_completion(record)
    return record


__all__ = ["ForwardEvaluationReport", "ForwardEvaluationReportError", "write_forward_evaluation_report", "finalize_forward_evaluation_report", "load_forward_evaluation_report"]
