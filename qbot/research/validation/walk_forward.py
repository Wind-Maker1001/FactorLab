"""Train-only expanding walk-forward execution for FactorLab models.

The runner deliberately keeps model selection and final evaluation separate:
every candidate is fitted on ``Train`` only, Ridge alpha is selected from
``Validation`` only, and the already-fitted winning model predicts ``Test``
exactly once.  In particular, no train-plus-validation refit is performed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_complex_dtype, is_numeric_dtype

from qbot.research.metrics.prediction import (
    PredictionEvaluation,
    PredictionMetricError,
    evaluate_predictions,
)
from qbot.research.models.factor_score import FactorScoreModel
from qbot.research.models.lightgbm import LightGBMResearchModel
from qbot.research.models.ridge import RidgeResearchModel
from qbot.research.models.xgboost import XGBoostResearchModel
from qbot.research.universe.csi500 import dataframe_sha256
from qbot.research.validation.splitter import (
    ExpandingWalkForwardSplitter,
    WalkForwardError,
    WalkForwardFold,
)


class WalkForwardRunError(ValueError):
    """Raised when a walk-forward experiment cannot be executed safely."""


@dataclass(frozen=True)
class WalkForwardResult:
    """All out-of-sample records and model-selection audit tables."""

    predictions: pd.DataFrame
    fold_summary: pd.DataFrame
    validation_metrics: pd.DataFrame
    coefficients: pd.DataFrame
    model_paths: Tuple[Path, ...]
    feature_importance: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(
            columns=[
                "fold",
                "model_name",
                "feature",
                "importance",
                "normalized_importance",
                "selected_parameters",
            ]
        )
    )
    # These diagnostics never change the Test-only public prediction table.
    # Empty defaults preserve older programmatically constructed results.
    diagnostic_predictions: pd.DataFrame = field(default_factory=pd.DataFrame)
    diagnostic_daily: pd.DataFrame = field(default_factory=pd.DataFrame)
    diagnostic_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    diagnostic_audit: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def validation_candidate_metrics(self) -> pd.DataFrame:
        return self.validation_metrics

    @property
    def ridge_coefficients(self) -> pd.DataFrame:
        return self.coefficients

    @property
    def tree_feature_importance(self) -> pd.DataFrame:
        return self.feature_importance


_SELECTION_METRICS = {
    "prediction_ic_mean",
    "prediction_rank_ic_mean",
    "top_bottom_spread",
}

DIAGNOSTIC_STAGES = (
    "TRAIN_IN_SAMPLE", "VALIDATION_SELECTION", "HISTORICAL_TEST",
)
DIAGNOSTIC_SELECTION_USAGE = {
    "TRAIN_IN_SAMPLE": "diagnostic_only",
    "VALIDATION_SELECTION": "selection_and_diagnostic",
    "HISTORICAL_TEST": "evaluation_only",
}
DIAGNOSTIC_AUDIT_COLUMNS = (
    "schema_version", "fold", "stage", "model_name", "fit_scope", "selection_usage",
    "selected_alpha", "selected_parameters", "model_parameters", "feature_names",
    "target_column", "selection_metric", "top_k", "minimum_cross_section", "random_seed",
    "model_path", "saved_model_sha256", "signal_start", "signal_end", "label_min_entry",
    "label_max_entry", "label_min_exit", "label_max_exit", "labeled_max_exit",
    "maturity_boundary", "rows", "labeled_rows", "scored_rows", "source_keys_sha256",
    "source_sha256", "fit_source_keys_sha256", "fit_source_sha256", "prediction_sha256",
    "daily_sha256", "summary_sha256",
)


def diagnostic_frame_sha256(frame: pd.DataFrame) -> str:
    """Use FactorLab's stable, row-order-neutral content digest for stage tables."""

    return dataframe_sha256(frame)

_LIGHTGBM_PARAMETER_NAMES = frozenset(
    {
        "n_estimators",
        "learning_rate",
        "num_leaves",
        "max_depth",
        "feature_fraction",
        "bagging_fraction",
        "bagging_freq",
        "reg_alpha",
        "reg_lambda",
        "min_child_samples",
    }
)
_XGBOOST_PARAMETER_NAMES = frozenset(
    {
        "n_estimators",
        "learning_rate",
        "max_depth",
        "min_child_weight",
        "subsample",
        "colsample_bytree",
        "reg_alpha",
        "reg_lambda",
        "gamma",
    }
)


def _positive_integer(name: str, value: int, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise WalkForwardRunError("%s must be an integer" % name)
    if int(value) < minimum:
        raise WalkForwardRunError("%s must be at least %d" % (name, minimum))
    return int(value)


def _feature_names(values: Iterable[str]) -> Tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise WalkForwardRunError("feature_names must be a sequence of column names")
    names = tuple(str(value) for value in values)
    if not names or any(not name for name in names):
        raise WalkForwardRunError("feature_names must contain non-empty names")
    if len(names) != len(set(names)):
        raise WalkForwardRunError("feature_names must not contain duplicates")
    return names


def _ridge_alphas(values: Iterable[float]) -> Tuple[float, ...]:
    if isinstance(values, (str, bytes)):
        raise WalkForwardRunError("ridge_alphas must be a sequence")
    result = []
    for value in values:
        if isinstance(value, (bool, np.bool_)):
            raise WalkForwardRunError("ridge_alphas must be finite and positive")
        try:
            alpha = float(value)
        except (TypeError, ValueError) as exc:
            raise WalkForwardRunError(
                "ridge_alphas must be finite and positive"
            ) from exc
        if not np.isfinite(alpha) or alpha <= 0:
            raise WalkForwardRunError("ridge_alphas must be finite and positive")
        result.append(alpha)
    if not result:
        raise WalkForwardRunError("ridge_alphas must contain at least one candidate")
    if len(result) != len(set(result)):
        raise WalkForwardRunError("ridge_alphas must not contain duplicates")
    return tuple(result)


def _tree_candidates(
    model_name: str,
    values: Optional[Sequence[Mapping[str, object]]],
) -> Tuple[Dict[str, object], ...]:
    """Return immutable-by-convention, JSON-auditable tree candidates."""

    if values is None or isinstance(values, (str, bytes, Mapping)):
        raise WalkForwardRunError(
            "%s_candidates must be a non-empty sequence of mappings" % model_name
        )
    allowed = (
        _LIGHTGBM_PARAMETER_NAMES
        if model_name == "lightgbm"
        else _XGBOOST_PARAMETER_NAMES
    )
    result: List[Dict[str, object]] = []
    fingerprints = set()
    for number, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise WalkForwardRunError(
                "%s_candidates[%d] must be a mapping" % (model_name, number)
            )
        candidate = {str(name): item for name, item in value.items()}
        unknown = sorted(set(candidate).difference(allowed))
        if unknown:
            raise WalkForwardRunError(
                "%s_candidates[%d] contains unknown parameters: %s"
                % (model_name, number, ", ".join(unknown))
            )
        try:
            fingerprint = json.dumps(
                candidate, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise WalkForwardRunError(
                "%s_candidates[%d] is not JSON-serializable"
                % (model_name, number)
            ) from exc
        if fingerprint in fingerprints:
            raise WalkForwardRunError(
                "%s_candidates must not contain duplicates" % model_name
            )
        fingerprints.add(fingerprint)
        result.append(candidate)
    if not result:
        raise WalkForwardRunError(
            "%s_candidates must contain at least one candidate" % model_name
        )
    return tuple(result)


def _normalise_dates(values: pd.Series, name: str) -> pd.Series:
    try:
        dates = pd.to_datetime(values, errors="raise")
    except (TypeError, ValueError) as exc:
        raise WalkForwardRunError("%s contains invalid dates" % name) from exc
    if dates.isna().any():
        raise WalkForwardRunError("%s must not contain missing dates" % name)
    if getattr(dates.dt, "tz", None) is not None:
        dates = dates.dt.tz_localize(None)
    return dates.dt.normalize()


def _validate_numeric_column(
    data: pd.DataFrame, column: str, *, allow_nan: bool
) -> None:
    series = data[column]
    if not is_numeric_dtype(series.dtype) or is_bool_dtype(series.dtype):
        raise WalkForwardRunError("%s must be numeric (not bool)" % column)
    if is_complex_dtype(series.dtype):
        raise WalkForwardRunError("%s must be real-valued" % column)
    values = series.to_numpy(dtype=float, copy=False)
    if np.isinf(values).any():
        raise WalkForwardRunError("%s must not contain infinity" % column)
    if not allow_nan and np.isnan(values).any():
        raise WalkForwardRunError("%s must not contain missing values" % column)


def _overall_row(evaluation: PredictionEvaluation) -> pd.Series:
    overall = evaluation.overall
    if len(overall) != 1:
        raise WalkForwardRunError("prediction evaluation did not produce one overall row")
    return overall.iloc[0]


def _date_boundaries(values: pd.Series) -> Tuple[pd.Timestamp, pd.Timestamp]:
    return pd.Timestamp(values.min()), pd.Timestamp(values.max())


class WalkForwardRunner:
    """Execute one configured model on immutable expanding folds.

    Candidate selection uses Validation only.  The selected, already-fitted
    Train model is then evaluated once on Test, identically for linear and
    tree models.
    """

    def __init__(
        self,
        splitter: ExpandingWalkForwardSplitter,
        *,
        model_name: str,
        feature_names: Sequence[str],
        factor_weights: Optional[Mapping[str, float]] = None,
        normalize_by_absolute_weight: bool = False,
        ridge_alphas: Sequence[float] = (0.1, 1.0, 10.0),
        ridge_fit_intercept: bool = True,
        ridge_max_iter: int = 10000,
        ridge_tolerance: float = 0.0001,
        lightgbm_candidates: Optional[Sequence[Mapping[str, object]]] = None,
        lightgbm_n_jobs: int = 1,
        xgboost_candidates: Optional[Sequence[Mapping[str, object]]] = None,
        xgboost_n_jobs: int = 1,
        target_col: str = "EXCESS_RET5",
        date_col: str = "date",
        symbol_col: str = "symbol",
        regime_col: Optional[str] = None,
        selection_metric: str = "prediction_rank_ic_mean",
        top_k: int = 20,
        minimum_cross_section: int = 2,
        random_seed: int = 42,
        model_directory: Optional[Path] = None,
    ) -> None:
        if not isinstance(splitter, ExpandingWalkForwardSplitter):
            raise WalkForwardRunError(
                "splitter must be an ExpandingWalkForwardSplitter"
            )
        canonical_model = str(model_name).strip().lower().replace("-", "_")
        if canonical_model not in {
            "factor_score",
            "ridge",
            "lightgbm",
            "xgboost",
        }:
            raise WalkForwardRunError(
                "model_name must be factor_score, ridge, lightgbm, or xgboost"
            )
        self.splitter = splitter
        self.model_name = canonical_model
        self.feature_names = _feature_names(feature_names)
        self.target_col = self._column_name("target_col", target_col)
        self.date_col = self._column_name("date_col", date_col)
        self.symbol_col = self._column_name("symbol_col", symbol_col)
        self.regime_col = (
            None if regime_col is None else self._column_name("regime_col", regime_col)
        )
        if {self.target_col, self.regime_col}.intersection({"stage", "fit_scope", "selection_usage"}):
            raise WalkForwardRunError(
                "target and regime columns must not use reserved stage diagnostic metadata names"
            )
        semantic_columns = [self.date_col, self.symbol_col, self.target_col]
        if self.regime_col is not None:
            semantic_columns.append(self.regime_col)
        if len(set(semantic_columns)) != len(semantic_columns):
            raise WalkForwardRunError(
                "date, symbol, target, and regime columns must be distinct"
            )
        reserved_features = set(semantic_columns).union({"entry_date", "exit_date"})
        leaked_features = sorted(set(self.feature_names).intersection(reserved_features))
        if leaked_features:
            raise WalkForwardRunError(
                "feature_names must not contain target or reserved audit columns: %s"
                % ", ".join(leaked_features)
            )
        if selection_metric not in _SELECTION_METRICS:
            raise WalkForwardRunError(
                "selection_metric must be one of %s"
                % ", ".join(sorted(_SELECTION_METRICS))
            )
        self.selection_metric = selection_metric
        self.top_k = _positive_integer("top_k", top_k)
        self.minimum_cross_section = _positive_integer(
            "minimum_cross_section", minimum_cross_section, minimum=2
        )
        if isinstance(random_seed, bool) or not isinstance(random_seed, int) or random_seed < 0:
            raise WalkForwardRunError("random_seed must be a non-negative integer")
        self.random_seed = random_seed
        self.model_directory = None if model_directory is None else Path(model_directory)
        if not isinstance(normalize_by_absolute_weight, (bool, np.bool_)):
            raise WalkForwardRunError(
                "normalize_by_absolute_weight must be boolean"
            )
        if not isinstance(ridge_fit_intercept, (bool, np.bool_)):
            raise WalkForwardRunError("ridge_fit_intercept must be boolean")
        if (
            isinstance(ridge_max_iter, bool)
            or not isinstance(ridge_max_iter, (int, np.integer))
            or int(ridge_max_iter) <= 0
        ):
            raise WalkForwardRunError("ridge_max_iter must be a positive integer")
        if isinstance(ridge_tolerance, (bool, np.bool_)):
            raise WalkForwardRunError("ridge_tolerance must be finite and positive")
        try:
            numeric_tolerance = float(ridge_tolerance)
        except (TypeError, ValueError) as exc:
            raise WalkForwardRunError(
                "ridge_tolerance must be finite and positive"
            ) from exc
        if not np.isfinite(numeric_tolerance) or numeric_tolerance <= 0:
            raise WalkForwardRunError("ridge_tolerance must be finite and positive")
        self.normalize_by_absolute_weight = bool(normalize_by_absolute_weight)
        self.ridge_fit_intercept = bool(ridge_fit_intercept)
        self.ridge_max_iter = int(ridge_max_iter)
        self.ridge_tolerance = numeric_tolerance
        self.lightgbm_n_jobs = _positive_integer(
            "lightgbm_n_jobs", lightgbm_n_jobs
        )
        self.xgboost_n_jobs = _positive_integer(
            "xgboost_n_jobs", xgboost_n_jobs
        )

        if canonical_model == "factor_score":
            if not isinstance(factor_weights, Mapping) or not factor_weights:
                raise WalkForwardRunError(
                    "factor_weights are required for factor_score"
                )
            missing_weights = set(self.feature_names).difference(factor_weights)
            extra_weights = set(factor_weights).difference(self.feature_names)
            if missing_weights or extra_weights:
                raise WalkForwardRunError(
                    "factor_weights must exactly match feature_names"
                )
            # Pin ordering to feature_names even if the incoming mapping has a
            # different insertion order.
            self.factor_weights = {
                name: factor_weights[name] for name in self.feature_names
            }
        else:
            if factor_weights is not None:
                raise WalkForwardRunError(
                    "factor_weights are only valid for factor_score"
                )
            self.factor_weights = None
        self.ridge_alphas = _ridge_alphas(ridge_alphas)
        self.lightgbm_candidates = (
            _tree_candidates("lightgbm", lightgbm_candidates)
            if canonical_model == "lightgbm"
            else tuple()
        )
        self.xgboost_candidates = (
            _tree_candidates("xgboost", xgboost_candidates)
            if canonical_model == "xgboost"
            else tuple()
        )

        # Validate tree parameter types before any fold starts.  Construction
        # does not import the optional native packages; those remain lazy.
        for candidate in self.lightgbm_candidates:
            LightGBMResearchModel(
                feature_names=self.feature_names,
                random_seed=self.random_seed,
                n_jobs=self.lightgbm_n_jobs,
                **candidate
            )
        for candidate in self.xgboost_candidates:
            XGBoostResearchModel(
                feature_names=self.feature_names,
                random_seed=self.random_seed,
                n_jobs=self.xgboost_n_jobs,
                **candidate
            )

    @staticmethod
    def _column_name(name: str, value: str) -> str:
        if not isinstance(value, str) or not value:
            raise WalkForwardRunError("%s must be a non-empty string" % name)
        return value

    def run(
        self,
        data: pd.DataFrame,
        *,
        calendar: Optional[Iterable[object]] = None,
        entry_date_col: str = "entry_date",
        exit_date_col: str = "exit_date",
    ) -> WalkForwardResult:
        """Run all folds and return test-only predictions plus audit tables."""

        entry_date_col = self._column_name("entry_date_col", entry_date_col)
        exit_date_col = self._column_name("exit_date_col", exit_date_col)
        runtime_semantic_columns = {
            self.date_col,
            self.symbol_col,
            self.target_col,
            entry_date_col,
            exit_date_col,
        }
        if self.regime_col is not None:
            runtime_semantic_columns.add(self.regime_col)
        expected_semantic_count = 5 + (1 if self.regime_col is not None else 0)
        if len(runtime_semantic_columns) != expected_semantic_count:
            raise WalkForwardRunError(
                "date, symbol, target, regime, entry, and exit columns must be distinct"
            )
        leaked_features = sorted(
            set(self.feature_names).intersection(runtime_semantic_columns)
        )
        if leaked_features:
            raise WalkForwardRunError(
                "feature_names must not contain target or reserved audit columns: %s"
                % ", ".join(leaked_features)
            )
        prepared = self._validate_data(data)
        missing_audit_columns = [
            name
            for name in (entry_date_col, exit_date_col)
            if name not in prepared.columns
        ]
        if missing_audit_columns:
            raise WalkForwardRunError(
                "label-window audit columns are required: %s"
                % ", ".join(missing_audit_columns)
            )
        try:
            folds = list(
                self.splitter.split(
                    prepared,
                    date_col=self.date_col,
                    entry_date_col=entry_date_col,
                    exit_date_col=exit_date_col,
                    calendar=calendar,
                )
            )
        except WalkForwardError:
            raise
        self._assert_unique_test_observations(prepared, folds)

        prediction_parts: List[pd.DataFrame] = []
        fold_rows: List[dict] = []
        candidate_rows: List[dict] = []
        coefficient_rows: List[dict] = []
        importance_rows: List[dict] = []
        saved_paths: List[Path] = []
        diagnostic_prediction_parts: List[pd.DataFrame] = []
        diagnostic_daily_parts: List[pd.DataFrame] = []
        diagnostic_summary_parts: List[pd.DataFrame] = []
        diagnostic_audit_rows: List[dict] = []

        for fold in folds:
            train = prepared.iloc[fold.train_indices]
            validation = prepared.iloc[fold.validation_indices]
            test = prepared.iloc[fold.test_indices]
            train_labeled = train.loc[train[self.target_col].notna()]
            if train_labeled.empty:
                raise WalkForwardRunError(
                    "fold %d has no finite training labels" % fold.fold
                )
            if not validation[self.target_col].notna().any():
                raise WalkForwardRunError(
                    "fold %d has no finite validation labels" % fold.fold
                )

            (
                selected_model,
                selected_alpha,
                selected_parameters,
                selected_validation,
                selected_validation_frame,
                selected_validation_evaluation,
                rows,
            ) = (
                self._fit_and_select(
                    fold.fold,
                    train_labeled,
                    validation,
                    entry_date_col=entry_date_col,
                    exit_date_col=exit_date_col,
                )
            )
            candidate_rows.extend(rows)

            # This is the sole Test predict call for the selected fold model.
            test_scores = selected_model.predict(test.loc[:, self.feature_names])
            prediction_frame = self._prediction_frame(
                test,
                test_scores,
                fold.fold,
                selected_alpha,
                selected_parameters,
                entry_date_col=entry_date_col,
                exit_date_col=exit_date_col,
            )
            test_evaluation = self._evaluate_prediction_frame(prediction_frame)
            test_overall = _overall_row(test_evaluation)
            prediction_parts.append(prediction_frame)

            # Only the already-selected model receives this extra diagnostic
            # call. Test has already been predicted exactly once; Validation
            # reuses the candidate-selection snapshot, never a second predict.
            train_scores = selected_model.predict(train.loc[:, self.feature_names])
            train_prediction_frame = self._prediction_frame(
                train, train_scores, fold.fold, selected_alpha, selected_parameters,
                entry_date_col=entry_date_col, exit_date_col=exit_date_col,
            )
            train_evaluation = self._evaluate_prediction_frame(train_prediction_frame)

            model_path: Optional[Path] = None
            saved_model_sha256: Optional[str] = None
            if self.model_directory is not None:
                self.model_directory.mkdir(parents=True, exist_ok=True)
                model_path = self.model_directory / (
                    "fold_%03d_%s.pkl" % (fold.fold, self.model_name)
                )
                selected_model.save(model_path)
                # Bind the actual saved artifact, not a parameters-derived
                # surrogate. A failed/missing artifact prevents diagnostics.
                saved_model_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
                saved_paths.append(model_path)

            if isinstance(selected_model, RidgeResearchModel):
                for feature, coefficient in selected_model.coefficients.items():
                    coefficient_rows.append(
                        {
                            "fold": fold.fold,
                            "feature": feature,
                            "coefficient": float(coefficient),
                            "alpha": float(selected_alpha),
                        }
                    )
            if isinstance(
                selected_model, (LightGBMResearchModel, XGBoostResearchModel)
            ):
                importance = selected_model.feature_importance
                total_importance = float(importance.sum())
                for feature, value in importance.items():
                    importance_rows.append(
                        {
                            "fold": fold.fold,
                            "model_name": self.model_name,
                            "feature": feature,
                            "importance": float(value),
                            "normalized_importance": (
                                float(value) / total_importance
                                if total_importance > 0.0
                                else 0.0
                            ),
                            "selected_parameters": selected_parameters,
                        }
                    )

            train_start, train_end = _date_boundaries(train[self.date_col])
            validation_start, validation_end = _date_boundaries(validation[self.date_col])
            test_start, test_end = _date_boundaries(test[self.date_col])
            audit = fold.audit
            if audit is None:
                raise WalkForwardRunError(
                    "fold %d is missing mandatory label-window audit evidence"
                    % fold.fold
                )
            fit_source = self._diagnostic_source_frame(
                train_labeled, entry_date_col=entry_date_col, exit_date_col=exit_date_col,
            )
            fit_source_keys_sha256 = diagnostic_frame_sha256(fit_source[["date", "symbol"]])
            fit_source_sha256 = diagnostic_frame_sha256(fit_source)
            model_parameters = self._model_parameters(selected_model)
            stages = (
                ("TRAIN_IN_SAMPLE", train, train_prediction_frame, train_evaluation, audit.validation_first_date),
                ("VALIDATION_SELECTION", validation, selected_validation_frame, selected_validation_evaluation, audit.test_first_date),
                ("HISTORICAL_TEST", test, prediction_frame, test_evaluation, None),
            )
            for stage, stage_source, stage_predictions, stage_evaluation, maturity_boundary in stages:
                stage_frame = self._stage_metadata(stage_predictions, fold.fold, stage)
                daily = self._stage_metadata(stage_evaluation.daily, fold.fold, stage)
                summary = self._stage_metadata(stage_evaluation.summary, fold.fold, stage)
                # Evaluate one fold and one stage at a time. Expanding Train
                # observations may repeat across folds but are never pooled.
                diagnostic_daily_parts.append(daily)
                diagnostic_summary_parts.append(summary)
                if stage != "HISTORICAL_TEST":
                    diagnostic_prediction_parts.append(stage_frame)
                source_frame = self._diagnostic_source_frame(
                    stage_source, entry_date_col=entry_date_col, exit_date_col=exit_date_col,
                )
                entry = pd.to_datetime(stage_frame["entry_date"], errors="raise")
                exit_ = pd.to_datetime(stage_frame["exit_date"], errors="raise")
                labeled = stage_frame[self.target_col].notna()
                diagnostic_audit_rows.append({
                    "schema_version": 1, "fold": int(fold.fold), "stage": stage,
                    "model_name": self.model_name, "fit_scope": "train_only",
                    "selection_usage": DIAGNOSTIC_SELECTION_USAGE[stage],
                    "selected_alpha": selected_alpha, "selected_parameters": selected_parameters,
                    "model_parameters": model_parameters,
                    "feature_names": json.dumps(list(self.feature_names), separators=(",", ":")),
                    "target_column": self.target_col, "selection_metric": self.selection_metric,
                    "top_k": self.top_k, "minimum_cross_section": self.minimum_cross_section,
                    "random_seed": self.random_seed,
                    "model_path": str(model_path) if model_path is not None else None,
                    "saved_model_sha256": saved_model_sha256,
                    "signal_start": pd.Timestamp(stage_frame["date"].min()),
                    "signal_end": pd.Timestamp(stage_frame["date"].max()),
                    "label_min_entry": entry.min(), "label_max_entry": entry.max(),
                    "label_min_exit": exit_.min(), "label_max_exit": exit_.max(),
                    "labeled_max_exit": exit_.loc[labeled].max(),
                    "maturity_boundary": maturity_boundary,
                    "rows": int(len(stage_frame)), "labeled_rows": int(labeled.sum()),
                    "scored_rows": int(stage_frame["score"].notna().sum()),
                    "source_keys_sha256": diagnostic_frame_sha256(source_frame[["date", "symbol"]]),
                    "source_sha256": diagnostic_frame_sha256(source_frame),
                    "fit_source_keys_sha256": fit_source_keys_sha256,
                    "fit_source_sha256": fit_source_sha256,
                    "prediction_sha256": diagnostic_frame_sha256(stage_frame),
                    "daily_sha256": diagnostic_frame_sha256(daily),
                    "summary_sha256": diagnostic_frame_sha256(summary),
                })
            fold_rows.append(
                {
                    "fold": fold.fold,
                    "model_name": self.model_name,
                    "fit_scope": "train_only",
                    "selected_alpha": selected_alpha,
                    "selected_parameters": selected_parameters,
                    "selection_metric": self.selection_metric,
                    "selection_value": selected_validation[self.selection_metric],
                    "train_start": train_start,
                    "train_end": train_end,
                    "validation_start": validation_start,
                    "validation_end": validation_end,
                    "test_start": test_start,
                    "test_end": test_end,
                    "train_window_start": fold.train_window[0],
                    "train_window_end_exclusive": fold.train_window[1],
                    "validation_window_start": fold.validation_window[0],
                    "validation_window_end_exclusive": fold.validation_window[1],
                    "test_window_start": fold.test_window[0],
                    "test_window_end_exclusive": fold.test_window[1],
                    "purged_date_count": int(len(fold.purged_dates)),
                    "embargoed_date_count": int(len(fold.embargoed_dates)),
                    "purged_dates": ",".join(
                        pd.Timestamp(value).date().isoformat()
                        for value in fold.purged_dates
                    ),
                    "embargoed_dates": ",".join(
                        pd.Timestamp(value).date().isoformat()
                        for value in fold.embargoed_dates
                    ),
                    "train_overlap_rows_removed": int(
                        audit.train_overlap_rows_removed
                    ),
                    "validation_overlap_rows_removed": int(
                        audit.validation_overlap_rows_removed
                    ),
                    "incomplete_label_rows_removed": int(
                        audit.incomplete_label_rows_removed
                    ),
                    "train_max_exit_date": audit.train_max_exit_date,
                    "validation_max_exit_date": audit.validation_max_exit_date,
                    "audit_validation_first_date": audit.validation_first_date,
                    "audit_test_first_date": audit.test_first_date,
                    "train_rows": int(len(train)),
                    "train_labeled_rows": int(len(train_labeled)),
                    "validation_rows": int(len(validation)),
                    "validation_labeled_rows": int(validation[self.target_col].notna().sum()),
                    "test_rows": int(len(test)),
                    "test_labeled_rows": int(test[self.target_col].notna().sum()),
                    "validation_prediction_ic": selected_validation[
                        "prediction_ic_mean"
                    ],
                    "validation_prediction_rank_ic": selected_validation[
                        "prediction_rank_ic_mean"
                    ],
                    "validation_top_bottom_spread": selected_validation[
                        "top_bottom_spread"
                    ],
                    "test_prediction_ic": test_overall["prediction_ic_mean"],
                    "test_prediction_rank_ic": test_overall[
                        "prediction_rank_ic_mean"
                    ],
                    "test_top_bottom_spread": test_overall["top_bottom_spread"],
                    "model_path": str(model_path) if model_path is not None else None,
                }
            )

        predictions = pd.concat(prediction_parts, ignore_index=True)
        # The pre-flight check should make this impossible; retain a final
        # assertion so future runner changes cannot silently double-count OOS.
        if predictions.duplicated(["date", "symbol"]).any():
            raise WalkForwardRunError(
                "walk-forward output contains duplicate test observations"
            )
        predictions.sort_values(
            ["date", "rank", "symbol"], kind="mergesort", inplace=True
        )
        predictions.reset_index(drop=True, inplace=True)
        return WalkForwardResult(
            predictions=predictions,
            fold_summary=pd.DataFrame(fold_rows),
            validation_metrics=pd.DataFrame(candidate_rows),
            coefficients=pd.DataFrame(
                coefficient_rows,
                columns=["fold", "feature", "coefficient", "alpha"],
            ),
            model_paths=tuple(saved_paths),
            feature_importance=pd.DataFrame(
                importance_rows,
                columns=[
                    "fold",
                    "model_name",
                    "feature",
                    "importance",
                    "normalized_importance",
                    "selected_parameters",
                ],
            ),
            diagnostic_predictions=pd.concat(diagnostic_prediction_parts, ignore_index=True),
            diagnostic_daily=pd.concat(diagnostic_daily_parts, ignore_index=True),
            diagnostic_summary=pd.concat(diagnostic_summary_parts, ignore_index=True),
            diagnostic_audit=pd.DataFrame(diagnostic_audit_rows, columns=DIAGNOSTIC_AUDIT_COLUMNS),
        )

    def _validate_data(self, data: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(data, pd.DataFrame):
            raise WalkForwardRunError("data must be a pandas DataFrame")
        if data.empty:
            raise WalkForwardRunError("data must not be empty")
        if data.columns.has_duplicates:
            raise WalkForwardRunError("data must not contain duplicate column names")
        required = {
            self.date_col,
            self.symbol_col,
            self.target_col,
        }.union(self.feature_names)
        if self.regime_col is not None:
            required.add(self.regime_col)
        missing = sorted(required.difference(data.columns))
        if missing:
            raise WalkForwardRunError(
                "data is missing required columns: %s" % ", ".join(missing)
            )
        prepared = data.copy().reset_index(drop=True)
        prepared[self.date_col] = _normalise_dates(
            prepared[self.date_col], self.date_col
        )
        if not prepared[self.date_col].is_monotonic_increasing:
            raise WalkForwardRunError(
                "data must be sorted chronologically by %s" % self.date_col
            )
        if prepared[self.symbol_col].isna().any() or prepared[
            self.symbol_col
        ].astype(str).str.strip().eq("").any():
            raise WalkForwardRunError("symbol must not contain missing or empty values")
        prepared[self.symbol_col] = prepared[self.symbol_col].astype(str)
        if prepared.duplicated([self.date_col, self.symbol_col]).any():
            raise WalkForwardRunError(
                "data contains duplicate (date, symbol) observations"
            )
        for feature in self.feature_names:
            _validate_numeric_column(prepared, feature, allow_nan=True)
        _validate_numeric_column(prepared, self.target_col, allow_nan=True)
        return prepared

    def _assert_unique_test_observations(
        self, data: pd.DataFrame, folds: Sequence[WalkForwardFold]
    ) -> None:
        seen = set()
        for fold in folds:
            keys = data.iloc[fold.test_indices][[self.date_col, self.symbol_col]]
            current = set(map(tuple, keys.itertuples(index=False, name=None)))
            overlap = seen.intersection(current)
            if overlap:
                sample = sorted(overlap, key=lambda value: (value[0], value[1]))[0]
                raise WalkForwardRunError(
                    "test windows overlap; duplicate test observation %s/%s"
                    % (sample[0].date(), sample[1])
                )
            seen.update(current)

    def _fit_and_select(
        self,
        fold_number: int,
        train: pd.DataFrame,
        validation: pd.DataFrame,
        *,
        entry_date_col: str,
        exit_date_col: str,
    ) -> Tuple[object, Optional[float], str, pd.Series, pd.DataFrame, PredictionEvaluation, List[dict]]:
        candidates: List[
            Tuple[int, Optional[float], str, object, pd.Series, pd.DataFrame, PredictionEvaluation]
        ] = []
        rows: List[dict] = []
        specifications: List[Tuple[str, Optional[float], Dict[str, object]]] = []
        if self.model_name == "ridge":
            specifications = [
                (
                    "alpha=%s" % format(float(alpha), "g"),
                    float(alpha),
                    {"alpha": float(alpha)},
                )
                for alpha in self.ridge_alphas
            ]
        elif self.model_name == "factor_score":
            specifications = [("fixed_weights", None, {})]
        elif self.model_name == "lightgbm":
            specifications = [
                ("candidate_%d" % number, None, dict(candidate))
                for number, candidate in enumerate(self.lightgbm_candidates)
            ]
        else:
            specifications = [
                ("candidate_%d" % number, None, dict(candidate))
                for number, candidate in enumerate(self.xgboost_candidates)
            ]

        for order, (candidate_name, alpha, parameters) in enumerate(specifications):
            if self.model_name == "ridge":
                model = RidgeResearchModel(
                    alpha=float(alpha),
                    feature_names=self.feature_names,
                    fit_intercept=self.ridge_fit_intercept,
                    random_seed=self.random_seed,
                    max_iter=self.ridge_max_iter,
                    tolerance=self.ridge_tolerance,
                )
            elif self.model_name == "factor_score":
                model = FactorScoreModel(
                    self.factor_weights,
                    normalize_by_absolute_weight=self.normalize_by_absolute_weight,
                )
            elif self.model_name == "lightgbm":
                model = LightGBMResearchModel(
                    feature_names=self.feature_names,
                    random_seed=self.random_seed,
                    n_jobs=self.lightgbm_n_jobs,
                    **parameters
                )
            else:
                model = XGBoostResearchModel(
                    feature_names=self.feature_names,
                    random_seed=self.random_seed,
                    n_jobs=self.xgboost_n_jobs,
                    **parameters
                )
            model.fit(
                train.loc[:, self.feature_names],
                train[self.target_col],
            )
            validation_scores = model.predict(validation.loc[:, self.feature_names])
            validation_frame = self._prediction_frame(
                validation,
                validation_scores,
                fold_number,
                alpha,
                json.dumps(
                    parameters,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                entry_date_col=entry_date_col,
                exit_date_col=exit_date_col,
            )
            validation_evaluation = self._evaluate_prediction_frame(validation_frame)
            overall = _overall_row(validation_evaluation)
            row = {
                "fold": fold_number,
                "model_name": self.model_name,
                "candidate": candidate_name,
                "alpha": alpha,
                "parameters": json.dumps(
                    parameters,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                "fit_scope": "train_only",
                "train_rows": int(len(train)),
                "validation_rows": int(len(validation)),
                "validation_labeled_rows": int(
                    validation[self.target_col].notna().sum()
                ),
                "prediction_ic_mean": overall["prediction_ic_mean"],
                "prediction_ic_std": overall["prediction_ic_std"],
                "prediction_icir": overall["prediction_icir"],
                "prediction_rank_ic_mean": overall["prediction_rank_ic_mean"],
                "prediction_rank_ic_std": overall["prediction_rank_ic_std"],
                "prediction_rank_icir": overall["prediction_rank_icir"],
                "top_k_future_return": overall["top_k_future_return"],
                "bottom_k_future_return": overall["bottom_k_future_return"],
                "top_bottom_spread": overall["top_bottom_spread"],
                "valid_dates": overall["valid_dates"],
                "coverage": overall["coverage"],
            }
            rows.append(row)
            candidates.append(
                (order, alpha, row["parameters"], model, pd.Series(row), validation_frame, validation_evaluation)
            )

        finite = [
            candidate
            for candidate in candidates
            if pd.notna(candidate[4][self.selection_metric])
        ]
        if not finite:
            raise WalkForwardRunError(
                "fold %d has no valid validation %s"
                % (fold_number, self.selection_metric)
            )
        # Earlier configured candidates win exact ties; Test is never involved.
        selected = max(
            finite,
            key=lambda candidate: (
                float(candidate[4][self.selection_metric]),
                -candidate[0],
            ),
        )
        _, alpha, parameters, model, validation_metrics, validation_frame, validation_evaluation = selected
        return model, alpha, parameters, validation_metrics, validation_frame, validation_evaluation, rows

    def _stage_metadata(self, frame: pd.DataFrame, fold_number: int, stage: str) -> pd.DataFrame:
        """Add role declarations without mutating cached predictions/metrics."""

        result = frame.copy(deep=True)
        result["fold"] = int(fold_number)
        result["model_name"] = self.model_name
        result["stage"] = stage
        result["fit_scope"] = "train_only"
        result["selection_usage"] = DIAGNOSTIC_SELECTION_USAGE[stage]
        return result

    def _diagnostic_source_frame(
        self, source: pd.DataFrame, *, entry_date_col: str, exit_date_col: str,
    ) -> pd.DataFrame:
        """Bind canonical keys and precisely the audited model/label inputs."""

        columns = [self.date_col, self.symbol_col, *self.feature_names,
                   self.target_col, entry_date_col, exit_date_col]
        if self.regime_col is not None:
            columns.append(self.regime_col)
        result = source.loc[:, columns].copy(deep=True)
        result.rename(columns={self.date_col: "date", self.symbol_col: "symbol",
                               entry_date_col: "entry_date", exit_date_col: "exit_date"}, inplace=True)
        for name in ("entry_date", "exit_date"):
            result[name] = pd.to_datetime(result[name], errors="raise")
        return result

    def _model_parameters(self, model: object) -> str:
        """Describe effective configuration, including untuned fixed defaults."""

        if self.model_name == "factor_score":
            parameters = {
                "weights": dict(model.weights),
                "normalize_by_absolute_weight": model.normalize_by_absolute_weight,
            }
        elif self.model_name == "ridge":
            parameters = {name: getattr(model, name) for name in (
                "alpha", "fit_intercept", "random_seed", "max_iter", "tolerance",
            )}
        else:
            names = _LIGHTGBM_PARAMETER_NAMES if self.model_name == "lightgbm" else _XGBOOST_PARAMETER_NAMES
            parameters = {name: getattr(model, name) for name in sorted(names)}
            parameters.update({"random_seed": model.random_seed, "n_jobs": model.n_jobs})
        return json.dumps(parameters, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _prediction_frame(
        self,
        source: pd.DataFrame,
        scores: pd.Series,
        fold_number: int,
        alpha: Optional[float],
        selected_parameters: str,
        *,
        entry_date_col: str,
        exit_date_col: str,
    ) -> pd.DataFrame:
        columns = [self.date_col, self.symbol_col, self.target_col]
        for optional in (entry_date_col, exit_date_col, self.regime_col):
            if optional is not None and optional in source.columns and optional not in columns:
                columns.append(optional)
        result = source.loc[:, columns].copy()
        result.rename(
            columns={
                self.date_col: "date",
                self.symbol_col: "symbol",
                entry_date_col: "entry_date",
                exit_date_col: "exit_date",
            },
            inplace=True,
        )
        result["score"] = scores.to_numpy(dtype=float, copy=False)
        result["fold"] = int(fold_number)
        result["model_name"] = self.model_name
        result["selected_alpha"] = alpha
        result["selected_parameters"] = selected_parameters
        result.sort_values(
            ["date", "score", "symbol"],
            ascending=[True, False, True],
            kind="mergesort",
            inplace=True,
        )
        result["rank"] = result.groupby("date", sort=False).cumcount() + 1
        return result

    def _evaluate_prediction_frame(
        self, prediction_frame: pd.DataFrame
    ) -> PredictionEvaluation:
        regime = self.regime_col
        # If a custom source name was supplied, _prediction_frame retained it.
        try:
            return evaluate_predictions(
                prediction_frame,
                score_col="score",
                target_col=self.target_col,
                top_k=self.top_k,
                minimum_cross_section=self.minimum_cross_section,
                fold_col="fold",
                regime_col=regime,
            )
        except PredictionMetricError as exc:
            raise WalkForwardRunError(str(exc)) from exc


def run_walk_forward(
    data: pd.DataFrame,
    splitter: ExpandingWalkForwardSplitter,
    *,
    model_name: str,
    feature_names: Sequence[str],
    calendar: Optional[Iterable[object]] = None,
    **runner_options: object
) -> WalkForwardResult:
    """Functional convenience wrapper around :class:`WalkForwardRunner`."""

    runner = WalkForwardRunner(
        splitter,
        model_name=model_name,
        feature_names=feature_names,
        **runner_options
    )
    return runner.run(data, calendar=calendar)


__all__ = [
    "DIAGNOSTIC_STAGES",
    "DIAGNOSTIC_SELECTION_USAGE",
    "DIAGNOSTIC_AUDIT_COLUMNS",
    "diagnostic_frame_sha256",
    "WalkForwardResult",
    "WalkForwardRunError",
    "WalkForwardRunner",
    "run_walk_forward",
]
