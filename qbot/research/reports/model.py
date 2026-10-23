"""Deterministic, headless reporting for FactorLab model experiments.

The writer consumes already-produced stage diagnostics, test predictions and
backtest results. It does not fit a model, select hyperparameters, or infer a
"winner" from test performance.  Every tabular artifact is written both as
CSV (easy to inspect) and Parquet (lossless and efficient to reload).
"""

from __future__ import annotations

import os
import json
import re
import uuid
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

import matplotlib

matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from qbot.research.backtest.fast import FastBacktestResult
from qbot.research.backtest.execution import ExecutionBacktestResult
from qbot.research.metrics.prediction import PredictionEvaluation
from qbot.research.metrics.robustness import (
    RobustnessError, RobustnessEvaluation, validate_robustness_evaluation,
)
from qbot.research.validation.walk_forward import WalkForwardResult, diagnostic_frame_sha256


class ModelReportError(ValueError):
    """Raised when model-report inputs are structurally invalid."""


_TABLE_NAMES = (
    "predictions",
    "walk_forward_results",
    "validation_results",
    "prediction_daily",
    "prediction_summary",
    "nav",
    "trades",
    "positions",
    "signals",
    "portfolio_metrics",
)

_DIAGNOSTIC_TABLES = {
    "stage_predictions": "diagnostic_predictions",
    "stage_prediction_daily": "diagnostic_daily",
    "stage_prediction_summary": "diagnostic_summary",
    "stage_prediction_audit": "diagnostic_audit",
}
_STAGE_USAGE = {
    "TRAIN_IN_SAMPLE": "diagnostic_only",
    "VALIDATION_SELECTION": "selection_and_diagnostic",
    "HISTORICAL_TEST": "evaluation_only",
}


def _diagnostic_dates(values: pd.Series, name: str) -> pd.Series:
    try:
        dates = pd.to_datetime(values, errors="raise")
        if dates.isna().any() or getattr(dates.dt, "tz", None) is not None or not dates.eq(dates.dt.normalize()).all():
            raise ValueError("not date-only")
        return dates
    except (ValueError, TypeError, AttributeError) as exc:
        raise ModelReportError("Stage diagnostics require complete date-only %s" % name) from exc


def _diagnostic_sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _diagnostic_integer(value: object, name: str, minimum: int = 0) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)) or int(value) < minimum:
        raise ModelReportError("Stage diagnostics require integer %s" % name)
    return int(value)


def _diagnostic_same(left: object, right: object) -> bool:
    left_missing, right_missing = bool(pd.isna(left)), bool(pd.isna(right))
    return left_missing and right_missing if left_missing or right_missing else bool(left == right)


def validate_stage_diagnostics(walk_forward: WalkForwardResult) -> Dict[str, pd.DataFrame]:
    """Validate producer diagnostics without fitting or rescoring any model.

    Complete stage tables bind existing scores and mature sample keys. Source
    hashes containing features require the CLI's separately verified dataset;
    these tables do not establish independently authoritative market data.
    Legacy results with all four default-empty fields explicitly return {}.
    """
    if not isinstance(walk_forward, WalkForwardResult):
        raise ModelReportError("walk_forward must be a WalkForwardResult")
    tables = {}
    for name, attribute in _DIAGNOSTIC_TABLES.items():
        frame = getattr(walk_forward, attribute)
        if not isinstance(frame, pd.DataFrame) or frame.columns.has_duplicates:
            raise ModelReportError("Stage diagnostics must be DataFrames with unique columns")
        tables[name] = frame.copy(deep=True)
    if all(frame.empty for frame in tables.values()):
        return {}
    if any(frame.empty for frame in tables.values()):
        raise ModelReportError("Stage diagnostics are incomplete: all four tables are required")
    if "fold" not in walk_forward.fold_summary or walk_forward.fold_summary["fold"].duplicated().any():
        raise ModelReportError("Stage diagnostics require unique fold audit rows")
    folds = {_diagnostic_integer(value, "fold") for value in walk_forward.fold_summary["fold"]}
    if not folds:
        raise ModelReportError("Stage diagnostics require declared folds")
    metadata = {"fold", "model_name", "stage", "fit_scope", "selection_usage"}
    predictions = tables["stage_predictions"]
    daily = tables["stage_prediction_daily"]
    summary = tables["stage_prediction_summary"]
    audit = tables["stage_prediction_audit"]
    required = {
        "stage_predictions": metadata | {"date", "symbol", "score", "rank", "entry_date", "exit_date"},
        "stage_prediction_daily": metadata | {"date", "prediction_ic", "prediction_rank_ic", "coverage"},
        "stage_prediction_summary": metadata | {"scope", "period", "prediction_ic_mean", "prediction_rank_ic_mean"},
        "stage_prediction_audit": metadata | {
            "schema_version", "selected_alpha", "selected_parameters", "model_parameters", "feature_names", "target_column",
            "selection_metric", "top_k", "minimum_cross_section", "random_seed", "model_path", "saved_model_sha256",
            "signal_start", "signal_end", "label_min_entry", "label_max_entry", "label_min_exit", "label_max_exit",
            "labeled_max_exit", "maturity_boundary", "rows", "labeled_rows", "scored_rows", "source_keys_sha256",
            "source_sha256", "fit_source_keys_sha256", "fit_source_sha256", "prediction_sha256", "daily_sha256", "summary_sha256",
        },
    }
    for name, frame in tables.items():
        if not required[name].issubset(frame.columns):
            raise ModelReportError("Stage diagnostics have missing columns: %s" % name)
        for value in frame["fold"].unique():
            _diagnostic_integer(value, "fold")
        expected_stages = {"TRAIN_IN_SAMPLE", "VALIDATION_SELECTION"} if name == "stage_predictions" else set(_STAGE_USAGE)
        pairs = set(map(tuple, frame[["fold", "stage"]].drop_duplicates().to_numpy()))
        if pairs != {(fold, stage) for fold in folds for stage in expected_stages}:
            raise ModelReportError("Stage diagnostics have incomplete or unexpected fold/stage groups")
        if not frame["fit_scope"].eq("train_only").all():
            raise ModelReportError("Stage diagnostics must identify train_only fitting")
        for stage, usage in _STAGE_USAGE.items():
            if not frame.loc[frame["stage"].eq(stage), "selection_usage"].eq(usage).all():
                raise ModelReportError("Stage diagnostics have invalid selection_usage")
        for column in frame:
            if pd.api.types.is_numeric_dtype(frame[column].dtype):
                values = frame[column].to_numpy(dtype=float, na_value=np.nan)
                if np.isinf(values).any() or pd.api.types.is_complex_dtype(frame[column].dtype):
                    raise ModelReportError("Stage diagnostics contain nonfinite or complex numeric data")
    for frame, keys in (
        (predictions, ["fold", "stage", "date", "symbol"]),
        (daily, ["fold", "stage", "date"]),
        (summary, ["fold", "stage", "scope", "period"]),
        (audit, ["fold", "stage"]),
    ):
        if frame.duplicated(keys).any():
            raise ModelReportError("Stage diagnostics contain duplicate stage observations")
    test = walk_forward.predictions
    if not isinstance(test, pd.DataFrame) or not {"fold", "model_name", "date", "symbol", "score", "rank", "entry_date", "exit_date"}.issubset(test):
        raise ModelReportError("Stage diagnostics require independent Test-only predictions")
    if test.duplicated(["date", "symbol"]).any() or set(test["fold"].unique()) != folds:
        raise ModelReportError("Independent Test predictions have invalid fold/sample keys")
    for fold in folds:
        fold_row = walk_forward.fold_summary.loc[walk_forward.fold_summary["fold"].eq(fold)].iloc[0]
        if fold_row.get("fit_scope") != "train_only":
            raise ModelReportError("Stage diagnostics conflict with fold model fitting policy")
        fold_audit = audit.loc[audit["fold"].eq(fold)]
        protocol = (
            "model_parameters", "feature_names", "target_column", "selection_metric", "top_k",
            "minimum_cross_section", "random_seed", "saved_model_sha256",
        )
        for name in protocol:
            baseline = fold_audit.iloc[0][name]
            if not all(_diagnostic_same(value, baseline) for value in fold_audit[name]):
                raise ModelReportError("Stage diagnostics disagree on the selected model/evaluation protocol")
        train = predictions.loc[predictions["fold"].eq(fold) & predictions["stage"].eq("TRAIN_IN_SAMPLE")]
        targets = fold_audit["target_column"].unique()
        if len(targets) != 1 or not isinstance(targets[0], str) or targets[0] not in train:
            raise ModelReportError("Stage diagnostics have inconsistent target columns")
        target = targets[0]
        fitted_keys = diagnostic_frame_sha256(train.loc[train[target].notna(), ["date", "symbol"]])
        if fold_audit["fit_source_keys_sha256"].nunique() != 1 or not fold_audit["fit_source_keys_sha256"].eq(fitted_keys).all():
            raise ModelReportError("Stage diagnostics fit-source keys differ from labeled Train")
        if fold_audit["fit_source_sha256"].nunique() != 1:
            raise ModelReportError("Stage diagnostics do not share one fitted Train source")
        for stage in _STAGE_USAGE:
            row = fold_audit.loc[fold_audit["stage"].eq(stage)].iloc[0]
            if _diagnostic_integer(row["schema_version"], "schema_version", 1) != 1:
                raise ModelReportError("Stage diagnostic audit schema version is invalid")
            _diagnostic_integer(row["top_k"], "top_k", 1)
            _diagnostic_integer(row["minimum_cross_section"], "minimum_cross_section", 2)
            _diagnostic_integer(row["random_seed"], "random_seed")
            source = (test.loc[test["fold"].eq(fold)].assign(stage=stage, fit_scope="train_only", selection_usage=_STAGE_USAGE[stage])
                      if stage == "HISTORICAL_TEST" else predictions.loc[predictions["fold"].eq(fold) & predictions["stage"].eq(stage)])
            stage_daily = daily.loc[daily["fold"].eq(fold) & daily["stage"].eq(stage)]
            stage_summary = summary.loc[summary["fold"].eq(fold) & summary["stage"].eq(stage)]
            if len(stage_summary.loc[stage_summary["scope"].eq("OVERALL")]) != 1:
                raise ModelReportError("Stage diagnostics require one OVERALL row per fold/stage")
            if target not in source or source["symbol"].isna().any() or source["symbol"].astype(str).str.strip().eq("").any():
                raise ModelReportError("Stage diagnostics require valid target and symbol observations")
            dates = _diagnostic_dates(source["date"], "signal dates")
            entry = _diagnostic_dates(source["entry_date"], "entry dates")
            exit_ = _diagnostic_dates(source["exit_date"], "exit dates")
            if not (dates.lt(entry) & entry.lt(exit_)).all():
                raise ModelReportError("Stage diagnostic label timing is invalid")
            bounds = {
                "signal_start": dates.min(), "signal_end": dates.max(),
                "label_min_entry": entry.min(), "label_max_entry": entry.max(),
                "label_min_exit": exit_.min(), "label_max_exit": exit_.max(),
                "labeled_max_exit": exit_.loc[source[target].notna()].max(),
            }
            for name, expected in bounds.items():
                actual = pd.Timestamp(row[name]) if not pd.isna(row[name]) else pd.NaT
                if not _diagnostic_same(actual, expected):
                    raise ModelReportError("Stage diagnostic %s differs from its samples" % name)
            prefix = {"TRAIN_IN_SAMPLE": "train", "VALIDATION_SELECTION": "validation", "HISTORICAL_TEST": "test"}[stage]
            for suffix in ("start", "end"):
                key = prefix + "_" + suffix
                if key not in fold_row or pd.Timestamp(fold_row[key]) != bounds["signal_" + suffix]:
                    raise ModelReportError("Stage diagnostic signal window differs from fold audit")
            boundary_key = {"TRAIN_IN_SAMPLE": "audit_validation_first_date", "VALIDATION_SELECTION": "audit_test_first_date"}.get(stage)
            if boundary_key is None:
                if not pd.isna(row["maturity_boundary"]):
                    raise ModelReportError("Historical Test must not claim a training maturity boundary")
            elif (boundary_key not in fold_row or pd.isna(row["maturity_boundary"])
                  or pd.Timestamp(row["maturity_boundary"]) != pd.Timestamp(fold_row[boundary_key])
                  or not exit_.lt(pd.Timestamp(row["maturity_boundary"])).all()):
                raise ModelReportError("Stage diagnostic label exits violate mature fold boundaries")
            counts = {"rows": len(source), "labeled_rows": int(source[target].notna().sum()), "scored_rows": int(source["score"].notna().sum())}
            for name, expected in counts.items():
                if _diagnostic_integer(row[name], name) != expected:
                    raise ModelReportError("Stage diagnostic %s differs from its samples" % name)
            hashes = {
                "source_keys_sha256": diagnostic_frame_sha256(source[["date", "symbol"]]),
                "prediction_sha256": diagnostic_frame_sha256(source),
                "daily_sha256": diagnostic_frame_sha256(stage_daily),
                "summary_sha256": diagnostic_frame_sha256(stage_summary),
            }
            if any(row[name] != digest for name, digest in hashes.items()):
                raise ModelReportError("Stage diagnostic sample or artifact SHA-256 mismatch")
            for name in ("source_sha256", "fit_source_sha256", "fit_source_keys_sha256"):
                if not _diagnostic_sha(row[name]):
                    raise ModelReportError("Stage diagnostic source SHA-256 is invalid")
            if not pd.isna(row["saved_model_sha256"]) and not _diagnostic_sha(row["saved_model_sha256"]):
                raise ModelReportError("Stage diagnostic saved model SHA-256 is invalid")
            if pd.isna(row["model_path"]) != pd.isna(row["saved_model_sha256"]):
                raise ModelReportError("Stage diagnostic model path/hash evidence is incomplete")
            for name in ("model_name", "selected_alpha", "selected_parameters", "model_path", "selection_metric"):
                if name not in fold_row or not _diagnostic_same(row[name], fold_row[name]):
                    raise ModelReportError("Stage diagnostics identify a different selected model")
            if not source["model_name"].eq(row["model_name"]).all() or not stage_daily["model_name"].eq(row["model_name"]).all() or not stage_summary["model_name"].eq(row["model_name"]).all():
                raise ModelReportError("Stage diagnostics have mixed model identities")
            try:
                feature_names = json.loads(row["feature_names"])
                parameters = json.loads(row["model_parameters"])
                selected = json.loads(row["selected_parameters"])
                json.dumps([feature_names, parameters, selected], allow_nan=False)
                if not isinstance(parameters, dict) or not isinstance(selected, dict) or not isinstance(feature_names, list) or not feature_names or any(not isinstance(value, str) or not value for value in feature_names):
                    raise ValueError("invalid metadata")
            except (ValueError, TypeError) as exc:
                raise ModelReportError("Stage diagnostic model metadata is invalid JSON") from exc
            if set(pd.to_datetime(stage_daily["date"])) != set(dates):
                raise ModelReportError("Stage diagnostic daily dates differ from source predictions")
    return tables


def _validate_test_stage_metrics(tables: Mapping[str, pd.DataFrame], prediction: PredictionEvaluation) -> None:
    if not tables:
        return
    daily = tables["stage_prediction_daily"].loc[lambda frame: frame["stage"].eq("HISTORICAL_TEST")]
    if not set(prediction.daily.columns).issubset(daily) or "fold" not in prediction.daily:
        raise ModelReportError("Historical Test diagnostics require independent Test metrics")
    left = daily[list(prediction.daily.columns)].sort_values(["fold", "date"]).reset_index(drop=True)
    right = prediction.daily.sort_values(["fold", "date"]).reset_index(drop=True)
    try:
        # Concatenation can represent an absent regime as NaN rather than pd.NA.
        # Normalize missing representations only in comparison copies; all scores
        # and realised metric values remain exact and persisted tables unchanged.
        pd.testing.assert_frame_equal(left.astype(object).where(left.notna(), None), right.astype(object).where(right.notna(), None), check_dtype=False, check_exact=True)
        metrics = [name for name in prediction.summary if name not in {"scope", "period"}]
        for fold, values in tables["stage_prediction_summary"].loc[lambda frame: frame["stage"].eq("HISTORICAL_TEST")].groupby("fold"):
            actual = values.loc[values["scope"].eq("OVERALL"), metrics].reset_index(drop=True)
            expected = prediction.summary.loc[prediction.summary["scope"].eq("FOLD") & prediction.summary["period"].astype(str).eq(str(fold)), metrics].reset_index(drop=True)
            pd.testing.assert_frame_equal(actual, expected, check_dtype=False, check_exact=True)
    except (AssertionError, KeyError) as exc:
        raise ModelReportError("Historical Test diagnostic metrics differ from independent Test evaluation") from exc


def _plot_stage_ic_comparison(summary: pd.DataFrame, destination: Path) -> None:
    """Display each fold separately; never pool expanding training samples."""
    figure, axes = plt.subplots(1, 2, figsize=(12, 4.8))
    overall = summary.loc[summary["scope"].eq("OVERALL")]
    labels = {
        "TRAIN_IN_SAMPLE": "Train (in-sample diagnostic)",
        "VALIDATION_SELECTION": "Validation (selection)",
        "HISTORICAL_TEST": "Historical Test (evaluation)",
    }
    for axis, column, title in zip(
        axes, ("prediction_ic_mean", "prediction_rank_ic_mean"),
        ("Pearson IC by fold", "RankIC by fold"),
    ):
        plotted = False
        for stage in _STAGE_USAGE:
            values = overall.loc[overall["stage"].eq(stage)].sort_values("fold")
            numeric = pd.to_numeric(values[column], errors="raise")
            if numeric.notna().any():
                axis.plot(values["fold"], numeric, marker="o", label=labels[stage])
                plotted = True
        if plotted:
            axis.set_title(title)
            axis.set_xlabel("Walk-forward fold")
            axis.set_xticks(sorted(overall["fold"].unique()))
            axis.set_ylabel("Mean daily correlation")
            axis.grid(alpha=0.2)
            axis.legend(fontsize=8)
        else:
            _placeholder(axis, title)
            axis.text(0.5, 0.35, "Insufficient observations (NaN)", ha="center", transform=axis.transAxes)
    figure.suptitle("Train-only models; stage diagnostics are not model selection from Test")
    _save_figure(figure, destination)


def _temporary_path(destination: Path) -> Path:
    """Return a same-directory temporary path with a useful final suffix."""

    token = uuid.uuid4().hex
    return destination.with_name(
        ".%s.%s.tmp%s" % (destination.stem, token, destination.suffix)
    )


def _atomic_write(destination: Path, writer: Callable[[Path], None]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(destination)
    try:
        writer(temporary)
        os.replace(str(temporary), str(destination))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_text(text: str, destination: Path) -> None:
    def write(path: Path) -> None:
        # ``Path.write_text(newline=...)`` is unavailable on Python 3.9.
        # Report text is constructed with explicit ``\n`` separators already.
        path.write_text(text, encoding="utf-8")

    _atomic_write(destination, write)


def _atomic_csv(frame: pd.DataFrame, destination: Path) -> None:
    _atomic_write(destination, lambda path: frame.to_csv(path, index=False))


def _atomic_parquet(frame: pd.DataFrame, destination: Path) -> None:
    _atomic_write(destination, lambda path: frame.to_parquet(path, index=False))


def _write_table(frame: pd.DataFrame, name: str, destination: Path) -> Tuple[Path, Path]:
    csv_path = destination / (name + ".csv")
    parquet_path = destination / (name + ".parquet")
    _atomic_csv(frame, csv_path)
    _atomic_parquet(frame, parquet_path)
    return csv_path, parquet_path


def _save_figure(figure: plt.Figure, destination: Path) -> None:
    try:
        _atomic_write(
            destination,
            lambda path: figure.savefig(
                path,
                format="png",
                dpi=150,
                bbox_inches="tight",
            ),
        )
    finally:
        plt.close(figure)


def _placeholder(axis: plt.Axes, title: str) -> None:
    axis.set_title(title)
    axis.text(
        0.5,
        0.5,
        "No data available",
        ha="center",
        va="center",
        transform=axis.transAxes,
        color="#666666",
    )
    axis.set_xticks([])
    axis.set_yticks([])


def _x_axis(frame: pd.DataFrame) -> pd.Series:
    if "date" not in frame:
        return pd.Series(np.arange(len(frame)), index=frame.index)
    dates = pd.to_datetime(frame["date"], errors="coerce")
    if dates.notna().any():
        return dates
    return pd.Series(np.arange(len(frame)), index=frame.index)


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


def _plot_nav(nav: pd.DataFrame, destination: Path) -> None:
    figure, axis = plt.subplots(figsize=(11, 5.5))
    plotted = False
    if not nav.empty:
        x_values = _x_axis(nav)
        for column, label in (
            ("strategy_nav", "Strategy"),
            ("benchmark_nav", "CSI500"),
            ("excess_nav", "Excess NAV"),
        ):
            values = _numeric(nav, column)
            if len(values) and values.notna().any():
                axis.plot(x_values, values, label=label, linewidth=1.4)
                plotted = True
    if plotted:
        axis.set_title("Out-of-sample NAV")
        axis.set_ylabel("NAV")
        axis.grid(alpha=0.2)
        axis.legend()
    else:
        _placeholder(axis, "Out-of-sample NAV")
    _save_figure(figure, destination)


def _drawdowns(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    valid = numeric.where(np.isfinite(numeric) & (numeric > 0))
    return valid / valid.cummax().clip(lower=1.0) - 1.0


def _plot_drawdown(nav: pd.DataFrame, destination: Path) -> None:
    figure, axis = plt.subplots(figsize=(11, 4.5))
    plotted = False
    if not nav.empty:
        x_values = _x_axis(nav)
        for column, label in (
            ("strategy_nav", "Strategy"),
            ("benchmark_nav", "CSI500"),
            ("excess_nav", "Excess NAV"),
        ):
            if column in nav:
                values = _drawdowns(nav[column])
                if values.notna().any():
                    axis.plot(x_values, values, label=label, linewidth=1.3)
                    plotted = True
    if plotted:
        axis.axhline(0.0, color="black", linewidth=0.8)
        axis.set_title("Drawdown")
        axis.set_ylabel("Drawdown")
        axis.grid(alpha=0.2)
        axis.legend()
    else:
        _placeholder(axis, "Drawdown")
    _save_figure(figure, destination)


def _plot_prediction_ic(daily: pd.DataFrame, destination: Path) -> None:
    figure, axis = plt.subplots(figsize=(11, 5))
    plotted = False
    if not daily.empty:
        x_values = _x_axis(daily)
        for column, label in (
            ("prediction_ic", "Prediction IC"),
            ("prediction_rank_ic", "Prediction RankIC"),
        ):
            values = _numeric(daily, column)
            if len(values) and values.notna().any():
                axis.plot(x_values, values, label=label, alpha=0.75, linewidth=1.0)
                plotted = True
    if plotted:
        axis.axhline(0.0, color="black", linewidth=0.8)
        axis.set_title("Daily out-of-sample prediction IC")
        axis.set_ylabel("Correlation")
        axis.grid(alpha=0.2)
        axis.legend()
    else:
        _placeholder(axis, "Daily out-of-sample prediction IC")
    _save_figure(figure, destination)


def _plot_daily_bar(
    nav: pd.DataFrame,
    column: str,
    title: str,
    ylabel: str,
    destination: Path,
) -> None:
    figure, axis = plt.subplots(figsize=(11, 4.5))
    values = _numeric(nav, column)
    if not nav.empty and len(values) and values.notna().any():
        axis.bar(_x_axis(nav), values.fillna(0.0), width=1.0, color="#35618f")
        axis.set_title(title)
        axis.set_ylabel(ylabel)
        axis.grid(axis="y", alpha=0.2)
    else:
        _placeholder(axis, title)
    _save_figure(figure, destination)


def _plot_coefficients(coefficients: pd.DataFrame, destination: Path) -> None:
    figure, axis = plt.subplots(figsize=(11, 6))
    required = {"fold", "feature", "coefficient"}
    if coefficients.empty or not required.issubset(coefficients.columns):
        _placeholder(axis, "Ridge coefficient stability")
        _save_figure(figure, destination)
        return
    table = coefficients.copy()
    table["coefficient"] = pd.to_numeric(table["coefficient"], errors="coerce")
    pivot = table.pivot_table(
        index="fold",
        columns="feature",
        values="coefficient",
        aggfunc="first",
    ).sort_index()
    if pivot.empty or not pivot.notna().any().any():
        _placeholder(axis, "Ridge coefficient stability")
    else:
        for feature in pivot.columns:
            axis.plot(
                pivot.index,
                pivot[feature],
                marker="o",
                markersize=3,
                linewidth=1.0,
                label=str(feature),
            )
        axis.axhline(0.0, color="black", linewidth=0.8)
        axis.set_title("Ridge coefficient stability by walk-forward fold")
        axis.set_xlabel("Fold")
        axis.set_ylabel("Coefficient")
        axis.grid(alpha=0.2)
        axis.legend(bbox_to_anchor=(1.02, 1.0), loc="upper left", fontsize=8)
    _save_figure(figure, destination)


def _plot_feature_importance(
    importance: pd.DataFrame, destination: Path
) -> None:
    figure, axis = plt.subplots(figsize=(10, 6))
    required = {"feature", "normalized_importance"}
    if importance.empty or not required.issubset(importance.columns):
        _placeholder(axis, "Mean tree feature importance")
        _save_figure(figure, destination)
        return
    table = importance.copy()
    table["normalized_importance"] = pd.to_numeric(
        table["normalized_importance"], errors="coerce"
    )
    values = (
        table.groupby("feature", sort=True)["normalized_importance"]
        .mean()
        .dropna()
        .sort_values()
    )
    if values.empty:
        _placeholder(axis, "Mean tree feature importance")
    else:
        values.plot(kind="barh", ax=axis, color="#35618f")
        axis.set_xlabel("Mean normalized gain importance")
        axis.set_title("Mean tree feature importance across walk-forward folds")
        axis.grid(axis="x", alpha=0.2)
    _save_figure(figure, destination)


def _plot_regime_performance(summary: pd.DataFrame, destination: Path) -> None:
    figure, axis = plt.subplots(figsize=(9, 5.5))
    required = {"scope", "period", "prediction_rank_ic_mean"}
    if summary.empty or not required.issubset(summary.columns):
        _placeholder(axis, "Prediction RankIC by market regime")
        _save_figure(figure, destination)
        return
    regimes = summary.loc[summary["scope"] == "REGIME"].copy()
    regimes["prediction_rank_ic_mean"] = pd.to_numeric(
        regimes["prediction_rank_ic_mean"], errors="coerce"
    )
    regimes.dropna(subset=["prediction_rank_ic_mean"], inplace=True)
    if regimes.empty:
        _placeholder(axis, "Prediction RankIC by market regime")
    else:
        regimes.sort_values("period", kind="mergesort", inplace=True)
        axis.bar(
            regimes["period"].astype(str),
            regimes["prediction_rank_ic_mean"],
            color="#527a5a",
        )
        axis.axhline(0.0, color="black", linewidth=0.8)
        axis.set_ylabel("Mean daily RankIC")
        axis.set_title("Prediction RankIC by market regime")
        axis.grid(axis="y", alpha=0.2)
    _save_figure(figure, destination)


def _metric_frame(metrics: Mapping[str, Any]) -> pd.DataFrame:
    if not isinstance(metrics, Mapping):
        raise ModelReportError("portfolio_metrics must be a mapping")
    rows = [{"metric": str(name), "value": value} for name, value in metrics.items()]
    return pd.DataFrame(rows, columns=["metric", "value"])


def _format_value(value: Any) -> str:
    if value is None or value is pd.NA:
        return "NA"
    try:
        if pd.isna(value):
            return "NA"
    except (TypeError, ValueError):
        pass
    if isinstance(value, (float, np.floating)):
        return "%.6f" % float(value)
    return str(value).replace("|", "\\|")


def _overall_prediction(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty or "scope" not in summary:
        return pd.DataFrame()
    return summary.loc[summary["scope"].astype(str).str.upper() == "OVERALL"]


def _mapping_from_metrics(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, pd.Series):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, pd.DataFrame):
        if value.empty:
            return {}
        if {"metric", "value"}.issubset(value.columns):
            return {
                str(row.metric): row.value
                for row in value.loc[:, ["metric", "value"]].itertuples(index=False)
            }
        if len(value) == 1:
            return {str(key): item for key, item in value.iloc[0].items()}
    raise ModelReportError(
        "portfolio_metrics must be a mapping, Series, or metric/value DataFrame"
    )


def _mapping_from_prediction(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, PredictionEvaluation):
        value = value.summary
    if isinstance(value, pd.Series):
        return {
            str(key): item
            for key, item in value.items()
            if str(key) not in {"scope", "period"}
        }
    if isinstance(value, pd.DataFrame):
        overall = _overall_prediction(value)
        if overall.empty and len(value) == 1:
            overall = value
        if overall.empty:
            return {}
        row = overall.iloc[0]
        return {
            str(key): item
            for key, item in row.items()
            if str(key) not in {"scope", "period"}
        }
    raise ModelReportError(
        "prediction_summary must be a PredictionEvaluation, Series, or DataFrame"
    )


def _comparison_record(value: Mapping[str, Any], model_name: Optional[str] = None) -> dict:
    record = dict(value)
    name = record.pop("model_name", model_name)
    if not isinstance(name, str) or not name.strip():
        raise ModelReportError("each model comparison record requires model_name")
    prediction_value = record.pop("prediction", None)
    if prediction_value is None:
        prediction_value = record.pop("prediction_summary", None)
    metrics_value = record.pop("portfolio_metrics", None)
    result = {"model_name": name.strip()}
    result.update(_mapping_from_prediction(prediction_value))
    result.update(_mapping_from_metrics(metrics_value))
    result.update(record)
    return result


def _comparison_frame(records_or_frame: Any) -> pd.DataFrame:
    if isinstance(records_or_frame, pd.DataFrame):
        frame = records_or_frame.copy()
    elif isinstance(records_or_frame, Mapping):
        if "model_name" in records_or_frame:
            frame = pd.DataFrame([_comparison_record(records_or_frame)])
        else:
            rows = []
            for model_name, value in records_or_frame.items():
                if isinstance(value, PredictionEvaluation):
                    value = {"prediction": value}
                if not isinstance(value, Mapping):
                    raise ModelReportError(
                        "model comparison mapping values must be mappings or PredictionEvaluation"
                    )
                rows.append(_comparison_record(value, model_name=str(model_name)))
            frame = pd.DataFrame(rows)
    elif isinstance(records_or_frame, Iterable) and not isinstance(
        records_or_frame, (str, bytes)
    ):
        rows = []
        for value in records_or_frame:
            if not isinstance(value, Mapping):
                raise ModelReportError("model comparison records must be mappings")
            rows.append(_comparison_record(value))
        frame = pd.DataFrame(rows)
    else:
        raise ModelReportError(
            "records_or_frame must be a DataFrame, mapping, or iterable of mappings"
        )
    if frame.empty:
        if "model_name" not in frame:
            frame = pd.DataFrame(columns=["model_name"])
        return frame
    if "model_name" not in frame:
        raise ModelReportError("model comparison data requires model_name")
    if frame["model_name"].isna().any() or frame["model_name"].astype(str).str.strip().eq("").any():
        raise ModelReportError("model_name must not contain missing or empty values")
    frame["model_name"] = frame["model_name"].astype(str).str.strip()
    if frame["model_name"].duplicated().any():
        raise ModelReportError("model comparison contains duplicate model_name values")
    return frame.sort_values("model_name", kind="mergesort").reset_index(drop=True)


def _plot_model_ic(comparison: pd.DataFrame, destination: Path) -> None:
    figure, axis = plt.subplots(figsize=(10, 5.5))
    metrics = [
        ("prediction_ic_mean", "Prediction IC"),
        ("prediction_rank_ic_mean", "Prediction RankIC"),
    ]
    available = [item for item in metrics if item[0] in comparison]
    if comparison.empty or not available:
        _placeholder(axis, "Out-of-sample model IC comparison")
        _save_figure(figure, destination)
        return
    x_values = np.arange(len(comparison))
    width = 0.34 if len(available) > 1 else 0.55
    plotted = False
    for index, (column, label) in enumerate(available):
        values = pd.to_numeric(comparison[column], errors="coerce")
        if not values.notna().any():
            continue
        offset = (index - (len(available) - 1) / 2.0) * width
        axis.bar(x_values + offset, values, width=width, label=label)
        plotted = True
    if plotted:
        axis.axhline(0.0, color="black", linewidth=0.8)
        axis.set_xticks(x_values, comparison["model_name"].astype(str), rotation=20)
        axis.set_ylabel("Mean daily correlation")
        axis.set_title("Out-of-sample model IC comparison")
        axis.grid(axis="y", alpha=0.2)
        axis.legend()
    else:
        _placeholder(axis, "Out-of-sample model IC comparison")
    _save_figure(figure, destination)


def _plot_comparison_nav(nav: pd.DataFrame, destination: Path) -> None:
    figure, axis = plt.subplots(figsize=(11, 5.5))
    required = {"date", "model_name", "strategy_nav", "benchmark_nav"}
    if nav.empty or not required.issubset(nav.columns):
        _placeholder(axis, "Out-of-sample NAV comparison")
        _save_figure(figure, destination)
        return
    table = nav.copy()
    table["date"] = pd.to_datetime(table["date"], errors="coerce")
    for model_name, values in table.groupby("model_name", sort=True):
        values = values.sort_values("date", kind="mergesort")
        axis.plot(values["date"], values["strategy_nav"], label=str(model_name))
    benchmark = table.sort_values("date", kind="mergesort").drop_duplicates("date")
    axis.plot(
        benchmark["date"],
        benchmark["benchmark_nav"],
        label="CSI500",
        color="black",
        linestyle="--",
        linewidth=1.2,
    )
    axis.set_title("Out-of-sample NAV comparison")
    axis.set_ylabel("NAV")
    axis.grid(alpha=0.2)
    axis.legend()
    _save_figure(figure, destination)


def _plot_comparison_drawdown(nav: pd.DataFrame, destination: Path) -> None:
    figure, axis = plt.subplots(figsize=(11, 5.0))
    required = {"date", "model_name", "strategy_nav"}
    if nav.empty or not required.issubset(nav.columns):
        _placeholder(axis, "Out-of-sample drawdown comparison")
        _save_figure(figure, destination)
        return
    for model_name, values in nav.groupby("model_name", sort=True):
        values = values.copy()
        values["date"] = pd.to_datetime(values["date"], errors="coerce")
        values.sort_values("date", kind="mergesort", inplace=True)
        series = pd.to_numeric(values["strategy_nav"], errors="coerce")
        drawdown = series / series.cummax() - 1.0
        axis.plot(values["date"], drawdown, label=str(model_name))
    axis.set_title("Out-of-sample drawdown comparison")
    axis.set_ylabel("Drawdown")
    axis.grid(alpha=0.2)
    axis.legend()
    _save_figure(figure, destination)


def _plot_comparison_daily_metric(
    nav: pd.DataFrame,
    column: str,
    title: str,
    ylabel: str,
    destination: Path,
) -> None:
    figure, axis = plt.subplots(figsize=(11, 5.0))
    required = {"date", "model_name", column}
    if nav.empty or not required.issubset(nav.columns):
        _placeholder(axis, title)
        _save_figure(figure, destination)
        return
    for model_name, values in nav.groupby("model_name", sort=True):
        values = values.copy()
        values["date"] = pd.to_datetime(values["date"], errors="coerce")
        values.sort_values("date", kind="mergesort", inplace=True)
        axis.plot(
            values["date"],
            pd.to_numeric(values[column], errors="coerce").fillna(0.0),
            label=str(model_name),
            linewidth=0.9,
        )
    axis.set_title(title)
    axis.set_ylabel(ylabel)
    axis.grid(alpha=0.2)
    axis.legend()
    _save_figure(figure, destination)


def _plot_comparison_feature_importance(
    importance: pd.DataFrame, destination: Path
) -> None:
    figure, axis = plt.subplots(figsize=(12, 6))
    required = {"model_name", "feature", "normalized_importance"}
    if importance.empty or not required.issubset(importance.columns):
        _placeholder(axis, "Tree feature importance comparison")
        _save_figure(figure, destination)
        return
    table = importance.pivot_table(
        index="feature",
        columns="model_name",
        values="normalized_importance",
        aggfunc="mean",
    ).fillna(0.0)
    table.plot(kind="bar", ax=axis)
    axis.set_title("Mean feature importance across walk-forward folds")
    axis.set_ylabel("Normalized gain importance")
    axis.grid(axis="y", alpha=0.2)
    _save_figure(figure, destination)


def _plot_comparison_regime(
    prediction_summary: pd.DataFrame, destination: Path
) -> None:
    figure, axis = plt.subplots(figsize=(10, 5.5))
    required = {
        "model_name",
        "scope",
        "period",
        "prediction_rank_ic_mean",
    }
    if prediction_summary.empty or not required.issubset(prediction_summary.columns):
        _placeholder(axis, "Prediction RankIC by model and regime")
        _save_figure(figure, destination)
        return
    regimes = prediction_summary.loc[
        prediction_summary["scope"] == "REGIME"
    ]
    table = regimes.pivot_table(
        index="period",
        columns="model_name",
        values="prediction_rank_ic_mean",
        aggfunc="first",
    )
    if table.empty:
        _placeholder(axis, "Prediction RankIC by model and regime")
    else:
        table.plot(kind="bar", ax=axis)
        axis.axhline(0.0, color="black", linewidth=0.8)
        axis.set_title("Prediction RankIC by model and market regime")
        axis.set_ylabel("Mean daily RankIC")
        axis.grid(axis="y", alpha=0.2)
    _save_figure(figure, destination)


def _comparison_markdown(
    comparison: pd.DataFrame, disclaimer: Optional[str] = None,
    robustness: Optional[RobustnessEvaluation] = None,
) -> str:
    lines = [
        "# FactorLab Phase 3 模型比较",
        "",
    ]
    if disclaimer:
        lines.extend(["> **%s**" % disclaimer.strip(), ""])
    lines.extend(
        [
            "本表仅客观并列各模型已完成的 Walk-Forward 样本外指标，不自动评选最佳模型，"
            "也不使用最终测试结果重新选择参数。",
            "",
        ]
    )
    if comparison.empty:
        lines.append("No completed model records are available.")
        lines.append("")
        return "\n".join(lines)
    if (
        "stage_diagnostics_available" in comparison
        and comparison["stage_diagnostics_available"].eq(True).all()
    ):
        lines.extend([
            "## Train / Validation / Historical Test diagnostics",
            "",
            "stage_prediction_summary/daily/audit 按模型、fold 和 stage 并列。"
            "TRAIN_IN_SAMPLE 是训练内拟合诊断；VALIDATION_SELECTION 已用于选参，"
            "不是独立留出样本；HISTORICAL_TEST 才是历史样本外评价。"
            "扩大训练集会重复包含旧日期，禁止把所有 Train folds 拼成一条独立收益序列。",
            "",
            "这些 Top/Bottom 收益是成本前未来超额标签，不是实际成交后的 NAV。"
            "主表与两级回测仍只使用 Test；没有真实 Forward 数据时不能回答真实样本外差距。",
            "",
        ])
    else:
        lines.extend([
            "Train/Validation diagnostics unavailable：旧报告未保存完整分阶段证据，"
            "不重新预测或从 Test 指标推测训练与验证表现。",
            "",
        ])
    preferred = [
        "model_name",
        "prediction_ic_mean",
        "prediction_rank_ic_mean",
        "prediction_icir",
        "prediction_rank_icir",
        "top_bottom_spread",
        "cumulative_return",
        "annualized_return",
        "sharpe_ratio",
        "maximum_drawdown",
        "information_ratio",
        "portfolio_turnover",
        "total_transaction_cost",
        "execution_cumulative_return",
        "execution_excess_return",
        "execution_sharpe_ratio",
        "execution_maximum_drawdown",
        "execution_total_transaction_cost",
    ]
    columns = [name for name in preferred if name in comparison.columns]
    if "model_name" not in columns:
        columns.insert(0, "model_name")
    lines.append("| %s |" % " | ".join(columns))
    lines.append("|%s|" % "|".join("---" if name == "model_name" else "---:" for name in columns))
    for row in comparison.loc[:, columns].itertuples(index=False, name=None):
        lines.append("| %s |" % " | ".join(_format_value(value) for value in row))
    lines.append("")
    if robustness is None:
        lines.extend([
            "统计稳健性检验 unavailable：未提供已验证的逐日 Test 指标和 benchmark 交易日历。"
            "ICIR 不是显著性检验；本报告不据此声称机器学习显著优于 FactorScore。", "",
        ])
    else:
        options = robustness.protocol["options"]
        lines.extend([
            "## Historical Test 统计稳健性（evaluation-only）", "",
            "以市场交易日而非股票—日为统计单位；按相同日期配对 ML − FactorScore。"
            "未来%d日标签重叠：Bartlett HAC 使用%d个交易日 lag，"
            "非循环 moving-block bootstrap 使用%d日块、%d次重采样。"
            % (options["holding_days"], options["hac_lags"], options["bootstrap_block_days"], options["bootstrap_repetitions"]),
            "",
            "固定主检验范围为总体 RankIC 的三个 ML − FactorScore 差值，双侧 p-value "
            "经 Holm 校正；不可检验项不缩小三项范围。拒绝零差异不等于优势，仍须看差值方向。"
            "不选择模型、不重新评分，也不反馈选参。", "",
            "HAC normal CI 和 bootstrap percentile CI 均为未做多重比较校正的个别区间；"
            "其他指标、Year/Quarter/Fold/Regime 分组均为探索性诊断，不能用分组挑出最佳模型。"
            "缺失交易日保留原日历位置，缺失非随机、市场非平稳和 fold 重拟合会削弱推断。", "",
            "| model | mean RankIC delta | HAC CI | block CI | Holm p | status |",
            "| --- | ---: | --- | --- | ---: | --- |",
        ])
        primary = robustness.summary.loc[robustness.summary["primary_hypothesis"]]
        for row in primary.itertuples(index=False):
            lines.append("| %s | %s | [%s, %s] | [%s, %s] | %s | %s |" % (
                row.model_name, _format_value(row.mean),
                _format_value(row.hac_ci_lower), _format_value(row.hac_ci_upper),
                _format_value(row.bootstrap_ci_lower), _format_value(row.bootstrap_ci_upper),
                _format_value(row.holm_adjusted_p_value), row.status,
            ))
        lines.extend([
            "", "完整结果见 model_inference.csv/parquet、inference_daily、inference_audit "
            "及 inference_protocol.json。样本不足、覆盖不足和零方差的 p/CI 保留缺失，不报成0。",
            "这些是成本前超额标签的预测能力检验，不是净交易 Alpha 或真实 Forward 结果；"
            "统计方法不能证明 PIT 数据源真实性。", "",
        ])
    return "\n".join(lines)


def _plot_model_inference(
    result: RobustnessEvaluation, destination: Path, disclaimer: Optional[str] = None,
) -> None:
    primary = result.summary.loc[result.summary["primary_hypothesis"]].reset_index(drop=True)
    figure, axis = plt.subplots(figsize=(9.5, 4.8))
    for position, row in enumerate(primary.itertuples(index=False)):
        if pd.notna(row.mean):
            axis.scatter(row.mean, position, color="#264c86", zorder=3)
        if pd.notna(row.hac_ci_lower) and pd.notna(row.hac_ci_upper):
            axis.hlines(position, row.hac_ci_lower, row.hac_ci_upper, color="#264c86", linewidth=2)
        if pd.notna(row.bootstrap_ci_lower) and pd.notna(row.bootstrap_ci_upper):
            axis.hlines(position + .16, row.bootstrap_ci_lower, row.bootstrap_ci_upper, color="#b46820", linewidth=2)
        annotation = None
        if row.status != "OK":
            annotation = "%s (n=%d)" % (row.status, row.valid_dates)
        elif row.bootstrap_status != "OK":
            annotation = "Bootstrap CI unavailable"
        if annotation:
            axis.text(1.02, position, annotation, transform=axis.get_yaxis_transform(), ha="left", fontsize=8)
    axis.axvline(0, color="#777777", linestyle="--", linewidth=1)
    axis.set_yticks(np.arange(len(primary)))
    axis.set_yticklabels(primary["model_name"])
    axis.set_ylim(-.45, len(primary) - .45)
    axis.set_xlabel("Mean paired daily RankIC difference: ML minus FactorScore")
    axis.set_title("Historical Test only; individual CIs are not multiplicity-adjusted")
    axis.plot([], [], color="#264c86", label="HAC normal CI")
    axis.plot([], [], color="#b46820", label="Moving-block percentile CI")
    axis.legend(fontsize=8, loc="upper center", bbox_to_anchor=(.5, -.2), ncol=2)
    axis.grid(axis="x", alpha=.2)
    if disclaimer:
        figure.suptitle(disclaimer.strip(), fontsize=9)
    _save_figure(figure, destination)


def _summary_markdown(
    model_name: str,
    walk_forward: WalkForwardResult,
    prediction: PredictionEvaluation,
    backtest: FastBacktestResult,
    metrics: pd.DataFrame,
    disclaimer: Optional[str] = None,
    execution_backtest: Optional[ExecutionBacktestResult] = None,
    execution_metrics: Optional[pd.DataFrame] = None,
) -> str:
    fold_count = (
        int(walk_forward.fold_summary["fold"].nunique())
        if "fold" in walk_forward.fold_summary and not walk_forward.fold_summary.empty
        else 0
    )
    prediction_rows = len(walk_forward.predictions)
    nav_rows = len(backtest.nav)
    trade_rows = len(backtest.trades)
    lines = [
        "# FactorLab Phase 3 模型报告",
        "",
    ]
    if disclaimer:
        lines.extend(["> **%s**" % disclaimer.strip(), ""])
    lines.extend(
        [
            "- Model: `%s`" % model_name,
            "- Walk-forward folds: %d" % fold_count,
            "- Out-of-sample prediction rows: %d" % prediction_rows,
            "- Backtest trading dates: %d" % nav_rows,
            "- Recorded trades: %d" % trade_rows,
            "",
            "主 prediction_* 指标与组合回测均仅来自 Walk-Forward 的测试区间；"
            "另列的 stage_* 表是逐 fold 的训练/验证/历史测试诊断。"
            "本报告不依据最终测试收益重新选择因子、模型、Top-K 或交易成本。",
            "",
            "## Prediction quality",
            "",
        ]
    )
    overall = _overall_prediction(prediction.summary)
    if overall.empty:
        lines.append("No realised out-of-sample prediction metrics are available.")
    else:
        selected = (
            "prediction_ic_mean",
            "prediction_icir",
            "prediction_rank_ic_mean",
            "prediction_rank_icir",
            "top_k_future_return",
            "bottom_k_future_return",
            "top_bottom_spread",
            "score_coverage",
            "coverage",
            "top_label_coverage",
            "bottom_label_coverage",
            "valid_dates",
            "rank_ic_valid_dates",
        )
        row = overall.iloc[0]
        lines.extend(["| Metric | Value |", "|---|---:|"])
        for metric in selected:
            if metric in overall.columns:
                lines.append("| %s | %s |" % (metric, _format_value(row[metric])))
    lines.extend(["", "## Train / Validation / Historical Test diagnostics", ""])
    if walk_forward.diagnostic_summary.empty:
        lines.append("Stage diagnostics unavailable: legacy result has no recorded Train/Validation diagnostics; no metrics were reconstructed or invented.")
    else:
        lines.extend([
            "TRAIN_IN_SAMPLE is an in-sample diagnostic only. VALIDATION_SELECTION was used to choose "
            "configured parameters and may have selection optimism; it is not an independent final holdout. "
            "HISTORICAL_TEST is evaluation only. All three use the selected train_only model, "
            "not a train-plus-validation refit.",
            "",
            "Each fold is evaluated separately; overlapping expanding Train observations are not pooled. "
            "Stage gaps do not by themselves isolate future generalization. Top/Bottom returns are "
            "future excess-return labels before transaction costs, not realised portfolio returns. "
            "Input feature hashes remain bound to the CLI dataset provenance; hashes alone do not establish market truth.",
            "",
            "| Fold | Stage | Usage | IC | RankIC | Valid dates | Coverage |",
            "|---:|---|---|---:|---:|---:|---:|",
        ])
        overall_stages = walk_forward.diagnostic_summary.loc[walk_forward.diagnostic_summary["scope"].eq("OVERALL")]
        for _, row in overall_stages.sort_values(["fold", "stage"]).iterrows():
            values = [row.get(name, np.nan) for name in ("fold", "stage", "selection_usage", "prediction_ic_mean", "prediction_rank_ic_mean", "valid_dates", "coverage")]
            lines.append("| %s |" % " | ".join(_format_value(value) for value in values))
    lines.extend(["", "## Portfolio performance", ""])
    if metrics.empty:
        lines.append("No portfolio metrics are available.")
    else:
        lines.extend(["| Metric | Value |", "|---|---:|"])
        for row in metrics.itertuples(index=False):
            lines.append("| %s | %s |" % (row.metric, _format_value(row.value)))
    if execution_backtest is not None:
        lines.extend(
            [
                "",
                "## Rule-aware execution performance",
                "",
                "该层使用次日开盘、T+1、100股整数手、现金约束、停牌和涨跌停日线近似。"
                "日线无法重建盘口队列，拒单记录见 `execution_rejections.csv`。",
                "",
                "- Executed trades: %d" % len(execution_backtest.trades),
                "- Rejected order fragments: %d"
                % len(execution_backtest.rejections),
                "",
            ]
        )
        if execution_metrics is None or execution_metrics.empty:
            lines.append("No execution metrics are available.")
        else:
            lines.extend(["| Metric | Value |", "|---|---:|"])
            for row in execution_metrics.itertuples(index=False):
                lines.append(
                    "| %s | %s |" % (row.metric, _format_value(row.value))
                )
    lines.extend(
        [
            "",
            "## Artifacts",
            "",
            "CSV files are intended for review and course-report tables; matching Parquet files "
            "preserve data types for reproducible analysis. Daily-bar execution is an approximation "
            "and cannot reconstruct order-book liquidity or limit-order queues.",
            "",
        ]
    )
    return "\n".join(lines)


def write_model_report(
    model_name: str,
    walk_forward: WalkForwardResult,
    prediction_evaluation: PredictionEvaluation,
    backtest: FastBacktestResult,
    portfolio_metrics: Mapping[str, Any],
    output_dir: Path,
    disclaimer: Optional[str] = None,
    execution_backtest: Optional[ExecutionBacktestResult] = None,
    execution_metrics: Optional[Mapping[str, Any]] = None,
) -> Path:
    """Write one model's complete Phase-3 artifact directory.

    Returns the path to ``summary.md``.  Mandatory plots are still created for
    empty inputs, using an explicit ``No data available`` marker.  Ridge
    coefficient artifacts are emitted only when coefficients are present.
    """

    if not isinstance(model_name, str) or not model_name.strip():
        raise ModelReportError("model_name must be a non-empty string")
    if not isinstance(walk_forward, WalkForwardResult):
        raise ModelReportError("walk_forward must be a WalkForwardResult")
    if not isinstance(prediction_evaluation, PredictionEvaluation):
        raise ModelReportError(
            "prediction_evaluation must be a PredictionEvaluation"
        )
    if not isinstance(backtest, FastBacktestResult):
        raise ModelReportError("backtest must be a FastBacktestResult")
    if execution_backtest is not None and not isinstance(
        execution_backtest, ExecutionBacktestResult
    ):
        raise ModelReportError(
            "execution_backtest must be an ExecutionBacktestResult"
        )
    if execution_backtest is None and execution_metrics is not None:
        raise ModelReportError(
            "execution_metrics require execution_backtest"
        )

    stage_tables = validate_stage_diagnostics(walk_forward)
    _validate_test_stage_metrics(stage_tables, prediction_evaluation)
    destination = Path(output_dir)
    metric_table = _metric_frame(portfolio_metrics)
    destination.mkdir(parents=True, exist_ok=True)
    tables: Dict[str, pd.DataFrame] = {
        "predictions": walk_forward.predictions,
        "walk_forward_results": walk_forward.fold_summary,
        "validation_results": walk_forward.validation_metrics,
        "prediction_daily": prediction_evaluation.daily,
        "prediction_summary": prediction_evaluation.summary,
        "nav": backtest.nav,
        "trades": backtest.trades,
        "positions": backtest.positions,
        "signals": backtest.signals,
        "portfolio_metrics": metric_table,
    }
    for name in _TABLE_NAMES:
        _write_table(tables[name].copy(), name, destination)
    if stage_tables:
        for name, frame in stage_tables.items():
            _write_table(frame, name, destination)
        _plot_stage_ic_comparison(stage_tables["stage_prediction_summary"], destination / "stage_ic_comparison.png")

    execution_metric_table = pd.DataFrame(columns=["metric", "value"])
    if execution_backtest is not None:
        execution_metric_table = _metric_frame(execution_metrics or {})
        execution_tables = {
            "execution_nav": execution_backtest.nav,
            "execution_trades": execution_backtest.trades,
            "execution_positions": execution_backtest.positions,
            "execution_rejections": execution_backtest.rejections,
            "execution_signals": execution_backtest.signals,
            "execution_portfolio_metrics": execution_metric_table,
        }
        for name, table in execution_tables.items():
            _write_table(table.copy(), name, destination)
        _plot_nav(
            execution_backtest.nav,
            destination / "execution_nav_curve.png",
        )
        _plot_drawdown(
            execution_backtest.nav,
            destination / "execution_drawdown.png",
        )

    if not walk_forward.coefficients.empty:
        _write_table(
            walk_forward.coefficients.copy(),
            "ridge_coefficients",
            destination,
        )
        _plot_coefficients(
            walk_forward.coefficients,
            destination / "ridge_coefficients.png",
        )

    if not walk_forward.feature_importance.empty:
        _write_table(
            walk_forward.feature_importance.copy(),
            "feature_importance",
            destination,
        )
        _plot_feature_importance(
            walk_forward.feature_importance,
            destination / "feature_importance.png",
        )

    _plot_nav(backtest.nav, destination / "nav_curve.png")
    _plot_drawdown(backtest.nav, destination / "drawdown.png")
    _plot_prediction_ic(
        prediction_evaluation.daily,
        destination / "prediction_ic.png",
    )
    _plot_regime_performance(
        prediction_evaluation.summary,
        destination / "regime_performance.png",
    )
    _plot_daily_bar(
        backtest.nav,
        "turnover",
        "Daily portfolio turnover",
        "Turnover",
        destination / "turnover.png",
    )
    _plot_daily_bar(
        backtest.nav,
        "transaction_cost",
        "Daily transaction cost",
        "CNY",
        destination / "transaction_cost.png",
    )

    summary_path = destination / "summary.md"
    _atomic_text(
        _summary_markdown(
            model_name.strip(),
            walk_forward,
            prediction_evaluation,
            backtest,
            metric_table,
            disclaimer,
            execution_backtest,
            execution_metric_table,
        ),
        summary_path,
    )
    return summary_path


def write_model_comparison_report(
    comparison: Any,
    output_dir: Path,
    disclaimer: Optional[str] = None,
    details: Optional[Mapping[str, pd.DataFrame]] = None,
    robustness: Optional[RobustnessEvaluation] = None,
) -> Path:
    """Write an objective, non-selecting comparison of completed model runs.

    ``comparison`` may be an already-flat DataFrame, an iterable of flat
    mappings, or a mapping keyed by model name.  Structured records can supply
    ``prediction``/``prediction_summary`` and ``portfolio_metrics``; their
    overall values are merged into one row per model.  The function never
    ranks models or recommends one from final-test performance.
    """

    comparison = _comparison_frame(comparison)
    if robustness is not None:
        try:
            validate_robustness_evaluation(robustness)
            if set(comparison["model_name"]) != set(robustness.daily["model_name"]):
                raise RobustnessError("inference and comparison model identities differ")
        except RobustnessError as exc:
            raise ModelReportError(str(exc)) from exc
    reserved = {"inference_daily", "model_inference", "inference_audit"}
    if details is not None and isinstance(details, Mapping) and reserved.intersection(details):
        raise ModelReportError("Inference details require the verified robustness argument")
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    _atomic_csv(comparison, destination / "model_comparison.csv")
    _plot_model_ic(comparison, destination / "model_ic_comparison.png")
    detail_tables: Dict[str, pd.DataFrame] = {}
    if details is not None:
        if not isinstance(details, Mapping):
            raise ModelReportError("details must be a mapping of DataFrames")
        for raw_name, table in details.items():
            name = str(raw_name)
            if (
                not name
                or Path(name).name != name
                or not name.replace("_", "").isalnum()
            ):
                raise ModelReportError("detail table name is unsafe: %r" % raw_name)
            if not isinstance(table, pd.DataFrame):
                raise ModelReportError("detail table %s must be a DataFrame" % name)
            detail_tables[name] = table.copy()
            _write_table(detail_tables[name], name, destination)
    nav = detail_tables.get("nav", pd.DataFrame())
    _plot_comparison_nav(nav, destination / "nav_curve.png")
    _plot_comparison_drawdown(nav, destination / "drawdown.png")
    _plot_comparison_daily_metric(
        nav,
        "turnover",
        "Daily portfolio turnover by model",
        "Turnover",
        destination / "turnover.png",
    )
    _plot_comparison_daily_metric(
        nav,
        "transaction_cost",
        "Daily transaction cost by model",
        "CNY",
        destination / "transaction_cost.png",
    )
    _plot_comparison_feature_importance(
        detail_tables.get("feature_importance", pd.DataFrame()),
        destination / "feature_importance.png",
    )
    _plot_comparison_regime(
        detail_tables.get("prediction_summary", pd.DataFrame()),
        destination / "regime_performance.png",
    )
    if robustness is not None:
        for name, table in (
            ("inference_daily", robustness.daily), ("model_inference", robustness.summary),
            ("inference_audit", robustness.audit),
        ):
            _write_table(table, name, destination)
        _atomic_text(json.dumps(
            {"protocol": robustness.protocol, "evidence": robustness.evidence},
            ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
        ) + "\n", destination / "inference_protocol.json")
        _plot_model_inference(robustness, destination / "model_inference.png", disclaimer)
    summary_path = destination / "summary.md"
    _atomic_text(_comparison_markdown(comparison, disclaimer, robustness), summary_path)
    return summary_path


__all__ = [
    "ModelReportError",
    "validate_stage_diagnostics",
    "write_model_comparison_report",
    "write_model_report",
]
