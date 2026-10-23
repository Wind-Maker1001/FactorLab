"""Fixed, interpretable multi-factor score baseline."""

from __future__ import annotations

from collections import OrderedDict
from typing import Mapping, Optional

import numpy as np
import pandas as pd

from .base import (
    ModelInputError,
    ModelNotFittedError,
    ResearchModel,
    validate_feature_frame,
    validate_target,
)


class FactorScoreModel(ResearchModel):
    """Compute ``sum(weight_i * factor_i)`` using fixed configured weights.

    Factor inputs are daily cross-sectional z-scores. Missing observations are
    assigned the explicit neutral value zero; weights are never learned or
    altered by ``fit``. Infinity remains an input error.
    """

    def __init__(
        self,
        weights: Mapping[str, float],
        *,
        normalize_by_absolute_weight: bool = False,
    ) -> None:
        if not isinstance(weights, Mapping) or not weights:
            raise ModelInputError("weights must be a non-empty mapping")
        ordered = OrderedDict()
        for name, weight in weights.items():
            canonical_name = str(name)
            if not canonical_name:
                raise ModelInputError("Factor weight names must not be empty")
            if canonical_name in ordered:
                raise ModelInputError("Factor weight names must be unique")
            if isinstance(weight, (bool, np.bool_)):
                raise ModelInputError("Factor weights must be finite numbers")
            try:
                numeric_weight = float(weight)
            except (TypeError, ValueError) as exc:
                raise ModelInputError("Factor weights must be finite numbers") from exc
            if not np.isfinite(numeric_weight):
                raise ModelInputError("Factor weights must be finite numbers")
            ordered[canonical_name] = numeric_weight
        if not any(weight != 0.0 for weight in ordered.values()):
            raise ModelInputError("At least one factor weight must be non-zero")
        if not isinstance(normalize_by_absolute_weight, (bool, np.bool_)):
            raise ModelInputError("normalize_by_absolute_weight must be boolean")
        self.weights = ordered
        self.normalize_by_absolute_weight = bool(normalize_by_absolute_weight)
        self.feature_names = tuple(ordered)
        self._is_fitted = False

    @property
    def is_fitted(self) -> bool:
        return self._is_fitted

    def fit(
        self, X: pd.DataFrame, y: Optional[pd.Series] = None
    ) -> "FactorScoreModel":
        validate_feature_frame(X, self.feature_names, allow_nan=True)
        if y is not None:
            validate_target(y, X)
        self._is_fitted = True
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        if not self.is_fitted:
            raise ModelNotFittedError("FactorScoreModel must be fitted before predict")
        _, values = validate_feature_frame(X, self.feature_names, allow_nan=True)
        neutral_values = np.nan_to_num(values, nan=0.0)
        weights = np.asarray(list(self.weights.values()), dtype=float)
        scores = neutral_values @ weights
        if self.normalize_by_absolute_weight:
            scores = scores / np.abs(weights).sum()
        return pd.Series(scores, index=X.index, name="score", dtype=float)
