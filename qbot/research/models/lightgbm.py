"""Leakage-safe optional LightGBM regressor for cross-sectional ranking."""

from __future__ import annotations

from importlib import import_module
from numbers import Integral, Real
from pathlib import Path
from typing import Optional, Sequence, Type

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer

from .base import (
    ModelInputError,
    ModelNotFittedError,
    ResearchModel,
    validate_feature_frame,
    validate_target,
)


class LightGBMDependencyError(ImportError):
    """Raised when the optional LightGBM runtime cannot be imported."""


def _lightgbm_regressor_class() -> Type[object]:
    """Import LightGBM lazily so the FactorLab core remains dependency-light."""

    try:
        module = import_module("lightgbm")
        return module.LGBMRegressor
    except (ImportError, AttributeError) as exc:
        raise LightGBMDependencyError(
            "LightGBMResearchModel requires the optional dependency 'lightgbm'. "
            "Install a Python-compatible LightGBM build in the FactorLab "
            "research environment before fitting this model."
        ) from exc


def _positive_integer(name: str, value: object, *, minimum: int = 1) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Integral):
        raise ModelInputError("%s must be an integer >= %d" % (name, minimum))
    result = int(value)
    if result < minimum:
        raise ModelInputError("%s must be an integer >= %d" % (name, minimum))
    return result


def _finite_number(
    name: str,
    value: object,
    *,
    minimum: float,
    inclusive: bool = True,
) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        comparator = ">=" if inclusive else ">"
        raise ModelInputError("%s must be a finite number %s %s" % (name, comparator, minimum))
    result = float(value)
    valid = result >= minimum if inclusive else result > minimum
    if not np.isfinite(result) or not valid:
        comparator = ">=" if inclusive else ">"
        raise ModelInputError("%s must be a finite number %s %s" % (name, comparator, minimum))
    return result


def _fraction(name: str, value: object) -> float:
    result = _finite_number(name, value, minimum=0.0, inclusive=False)
    if result > 1.0:
        raise ModelInputError("%s must be in the interval (0, 1]" % name)
    return result


def _feature_names(value: Optional[Sequence[str]]) -> Optional[tuple]:
    if value is None:
        return None
    names = tuple(str(name) for name in value)
    if not names or any(not name for name in names):
        raise ModelInputError("feature_names must contain non-empty names")
    if len(names) != len(set(names)):
        raise ModelInputError("feature_names must not contain duplicates")
    return names


class LightGBMResearchModel(ResearchModel):
    """Small, deterministic LightGBM model with train-only median imputation.

    LightGBM is deliberately optional. Importing this module does not import
    the native LightGBM package; a clear :class:`LightGBMDependencyError` is
    raised only when a caller attempts to fit the model without it installed.
    """

    def __init__(
        self,
        *,
        feature_names: Optional[Sequence[str]] = None,
        n_estimators: int = 200,
        learning_rate: float = 0.03,
        num_leaves: int = 15,
        max_depth: int = 5,
        feature_fraction: float = 0.8,
        bagging_fraction: float = 0.8,
        bagging_freq: int = 1,
        reg_alpha: float = 0.0,
        reg_lambda: float = 1.0,
        min_child_samples: int = 20,
        random_seed: int = 42,
        n_jobs: int = 1,
    ) -> None:
        self.requested_feature_names = _feature_names(feature_names)
        self.n_estimators = _positive_integer("n_estimators", n_estimators)
        self.learning_rate = _finite_number(
            "learning_rate", learning_rate, minimum=0.0, inclusive=False
        )
        self.num_leaves = _positive_integer("num_leaves", num_leaves, minimum=2)
        if isinstance(max_depth, (bool, np.bool_)) or not isinstance(max_depth, Integral):
            raise ModelInputError("max_depth must be -1 or a positive integer")
        self.max_depth = int(max_depth)
        if self.max_depth == 0 or self.max_depth < -1:
            raise ModelInputError("max_depth must be -1 or a positive integer")
        self.feature_fraction = _fraction("feature_fraction", feature_fraction)
        self.bagging_fraction = _fraction("bagging_fraction", bagging_fraction)
        self.bagging_freq = _positive_integer(
            "bagging_freq", bagging_freq, minimum=0
        )
        self.reg_alpha = _finite_number("reg_alpha", reg_alpha, minimum=0.0)
        self.reg_lambda = _finite_number("reg_lambda", reg_lambda, minimum=0.0)
        self.min_child_samples = _positive_integer(
            "min_child_samples", min_child_samples
        )
        self.random_seed = _positive_integer(
            "random_seed", random_seed, minimum=0
        )
        self.n_jobs = _positive_integer("n_jobs", n_jobs)

    @property
    def is_fitted(self) -> bool:
        return all(
            hasattr(self, attribute)
            for attribute in ("feature_names_", "imputer_", "estimator_")
        )

    def fit(
        self, X: pd.DataFrame, y: Optional[pd.Series] = None
    ) -> "LightGBMResearchModel":
        if not isinstance(X, pd.DataFrame):
            raise ModelInputError("X must be a pandas DataFrame")
        names = self.requested_feature_names or tuple(str(column) for column in X.columns)
        selected, values = validate_feature_frame(X, names, allow_nan=True)
        target = validate_target(y, X)
        all_missing = [
            name for offset, name in enumerate(names) if np.isnan(values[:, offset]).all()
        ]
        if all_missing:
            raise ModelInputError(
                "Training features cannot be entirely missing: %s"
                % ", ".join(all_missing)
            )

        regressor_class = _lightgbm_regressor_class()
        imputer = SimpleImputer(strategy="median")
        imputed = imputer.fit_transform(values)
        training_frame = pd.DataFrame(
            imputed, index=selected.index, columns=list(names), dtype=float
        )
        estimator = regressor_class(
            objective="regression",
            n_estimators=self.n_estimators,
            learning_rate=self.learning_rate,
            num_leaves=self.num_leaves,
            max_depth=self.max_depth,
            feature_fraction=self.feature_fraction,
            bagging_fraction=self.bagging_fraction,
            bagging_freq=self.bagging_freq,
            reg_alpha=self.reg_alpha,
            reg_lambda=self.reg_lambda,
            min_child_samples=self.min_child_samples,
            importance_type="gain",
            random_state=self.random_seed,
            bagging_seed=self.random_seed,
            feature_fraction_seed=self.random_seed,
            data_random_seed=self.random_seed,
            deterministic=True,
            force_col_wise=True,
            n_jobs=self.n_jobs,
            verbosity=-1,
        )
        estimator.fit(training_frame, target)

        # Publish fitted state only after every training operation succeeds.
        self.feature_names_ = tuple(selected.columns)
        self.imputer_ = imputer
        self.estimator_ = estimator
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        self._require_fitted()
        selected, values = validate_feature_frame(
            X, self.feature_names_, allow_nan=True
        )
        imputed = self.imputer_.transform(values)
        prediction_frame = pd.DataFrame(
            imputed,
            index=selected.index,
            columns=list(self.feature_names_),
            dtype=float,
        )
        prediction = np.asarray(self.estimator_.predict(prediction_frame), dtype=float)
        if prediction.ndim != 1 or len(prediction) != len(X):
            raise RuntimeError("LightGBM returned an invalid prediction shape")
        if not np.isfinite(prediction).all():
            raise RuntimeError("LightGBM returned non-finite predictions")
        return pd.Series(prediction, index=X.index, name="score", dtype=float)

    @property
    def feature_names(self) -> tuple:
        """Fitted feature names in the exact estimator input order."""

        self._require_fitted()
        return self.feature_names_

    @property
    def feature_importance(self) -> pd.Series:
        """Named gain importance reported by the fitted LightGBM estimator."""

        self._require_fitted()
        values = np.asarray(self.estimator_.feature_importances_, dtype=float)
        if values.ndim != 1 or len(values) != len(self.feature_names_):
            raise RuntimeError("LightGBM returned invalid feature importance")
        if not np.isfinite(values).all():
            raise RuntimeError("LightGBM returned non-finite feature importance")
        return pd.Series(
            values,
            index=pd.Index(self.feature_names_, name="feature"),
            name="importance",
            dtype=float,
        )

    def export_feature_importance(self, path: Path) -> None:
        """Export named feature importance as a stable two-column CSV."""

        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.feature_importance.rename_axis("feature").reset_index().to_csv(
            destination, index=False
        )

    def _require_fitted(self) -> None:
        if not self.is_fitted:
            raise ModelNotFittedError("LightGBMResearchModel must be fitted first")


__all__ = ["LightGBMDependencyError", "LightGBMResearchModel"]
