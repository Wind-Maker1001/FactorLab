"""Leakage-safe Ridge regression for cross-sectional return prediction."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from .base import (
    ModelInputError,
    ModelNotFittedError,
    ResearchModel,
    validate_feature_frame,
    validate_target,
)


class RidgeResearchModel(ResearchModel):
    """Median-imputed, standardised Ridge model.

    The imputer and scaler are fitted exactly once from the training frame.
    ``predict`` only calls their ``transform`` methods, preventing validation
    or test observations from influencing preprocessing statistics.
    """

    def __init__(
        self,
        alpha: float = 1.0,
        *,
        feature_names: Optional[Sequence[str]] = None,
        fit_intercept: bool = True,
        random_seed: int = 42,
        max_iter: int = 10000,
        tolerance: float = 0.0001,
    ) -> None:
        if isinstance(alpha, (bool, np.bool_)):
            raise ModelInputError("alpha must be a finite non-negative number")
        try:
            numeric_alpha = float(alpha)
        except (TypeError, ValueError) as exc:
            raise ModelInputError("alpha must be a finite non-negative number") from exc
        if not np.isfinite(numeric_alpha) or numeric_alpha < 0:
            raise ModelInputError("alpha must be a finite non-negative number")
        if not isinstance(fit_intercept, (bool, np.bool_)):
            raise ModelInputError("fit_intercept must be boolean")
        if not isinstance(random_seed, int) or isinstance(random_seed, bool) or random_seed < 0:
            raise ModelInputError("random_seed must be a non-negative integer")
        if isinstance(max_iter, bool) or not isinstance(max_iter, int) or max_iter <= 0:
            raise ModelInputError("max_iter must be a positive integer")
        if isinstance(tolerance, (bool, np.bool_)):
            raise ModelInputError("tolerance must be a finite positive number")
        try:
            numeric_tolerance = float(tolerance)
        except (TypeError, ValueError) as exc:
            raise ModelInputError("tolerance must be a finite positive number") from exc
        if not np.isfinite(numeric_tolerance) or numeric_tolerance <= 0:
            raise ModelInputError("tolerance must be a finite positive number")
        if feature_names is not None:
            names = tuple(str(name) for name in feature_names)
            if not names or any(not name for name in names):
                raise ModelInputError("feature_names must contain non-empty names")
            if len(names) != len(set(names)):
                raise ModelInputError("feature_names must not contain duplicates")
        else:
            names = None
        self.alpha = numeric_alpha
        self.requested_feature_names = names
        self.fit_intercept = bool(fit_intercept)
        self.random_seed = random_seed
        self.max_iter = max_iter
        self.tolerance = numeric_tolerance

    @property
    def is_fitted(self) -> bool:
        return all(
            hasattr(self, attribute)
            for attribute in ("feature_names_", "imputer_", "scaler_", "estimator_")
        )

    def fit(self, X: pd.DataFrame, y: Optional[pd.Series] = None) -> "RidgeResearchModel":
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

        imputer = SimpleImputer(strategy="median")
        scaler = StandardScaler()
        estimator = Ridge(
            alpha=self.alpha,
            fit_intercept=self.fit_intercept,
            random_state=self.random_seed,
            max_iter=self.max_iter,
            tol=self.tolerance,
        )
        imputed = imputer.fit_transform(values)
        scaled = scaler.fit_transform(imputed)
        estimator.fit(scaled, target)

        # Assign fitted state only after every training operation succeeds.
        self.feature_names_ = tuple(selected.columns)
        self.imputer_ = imputer
        self.scaler_ = scaler
        self.estimator_ = estimator
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        self._require_fitted()
        _, values = validate_feature_frame(X, self.feature_names_, allow_nan=True)
        imputed = self.imputer_.transform(values)
        scaled = self.scaler_.transform(imputed)
        prediction = self.estimator_.predict(scaled)
        return pd.Series(prediction, index=X.index, name="score", dtype=float)

    @property
    def feature_names(self) -> tuple:
        """Fitted feature names in the exact order consumed by the model."""

        self._require_fitted()
        return self.feature_names_

    @property
    def coefficients(self) -> pd.Series:
        """Ridge coefficients in standardised feature space."""

        self._require_fitted()
        return pd.Series(
            np.asarray(self.estimator_.coef_, dtype=float),
            index=pd.Index(self.feature_names_, name="feature"),
            name="coefficient",
        )

    def export_coefficients(self, path: Path) -> None:
        """Export named coefficients as a stable two-column CSV artifact."""

        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.coefficients.rename_axis("feature").reset_index().to_csv(
            destination, index=False
        )

    def _require_fitted(self) -> None:
        if not self.is_fitted:
            raise ModelNotFittedError("RidgeResearchModel must be fitted first")
