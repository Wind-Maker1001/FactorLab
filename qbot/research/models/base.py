"""Common model contract and strict tabular input validation."""

from __future__ import annotations

from abc import ABC, abstractmethod
import os
from pathlib import Path
import pickle
from typing import Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_numeric_dtype


class ModelInputError(ValueError):
    """Raised when model inputs violate the research data contract."""


class ModelNotFittedError(RuntimeError):
    """Raised when prediction or export is requested before fitting."""


class ResearchModel(ABC):
    """Interface implemented by every FactorLab research model.

    ``load`` uses pickle and must only receive artifacts produced by a trusted
    FactorLab run.
    """

    @abstractmethod
    def fit(self, X: pd.DataFrame, y: Optional[pd.Series] = None) -> "ResearchModel":
        """Fit the model using training-period observations only."""

    @abstractmethod
    def predict(self, X: pd.DataFrame) -> pd.Series:
        """Return one continuous cross-sectional ranking score per row."""

    def save(self, path: Path) -> None:
        """Atomically persist a fitted model to ``path``."""

        if not self.is_fitted:
            raise ModelNotFittedError("Cannot save a model before fit")
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        try:
            with temporary.open("wb") as stream:
                pickle.dump(self, stream, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(str(temporary), str(destination))
        finally:
            if temporary.exists():
                temporary.unlink()

    @classmethod
    def load(cls, path: Path) -> "ResearchModel":
        """Load a trusted model artifact and enforce the requested class."""

        with Path(path).open("rb") as stream:
            model = pickle.load(stream)
        if not isinstance(model, cls):
            raise TypeError(
                "Model artifact contains %s, expected %s"
                % (type(model).__name__, cls.__name__)
            )
        if not model.is_fitted:
            raise ModelNotFittedError("Loaded model artifact is not fitted")
        return model

    @property
    @abstractmethod
    def is_fitted(self) -> bool:
        """Whether the model contains fitted state."""


def validate_feature_frame(
    X: pd.DataFrame,
    feature_names: Sequence[str],
    *,
    allow_nan: bool,
) -> Tuple[pd.DataFrame, np.ndarray]:
    """Select named numeric features in model order and validate finiteness.

    Extra columns are intentionally ignored so callers may pass a long-form
    dataset containing audit columns. Required features are always selected
    by name, never by their incoming position.
    """

    if not isinstance(X, pd.DataFrame):
        raise ModelInputError("X must be a pandas DataFrame")
    if X.empty:
        raise ModelInputError("X must contain at least one row")
    if X.columns.has_duplicates:
        raise ModelInputError("X must not contain duplicate column names")
    names = tuple(str(name) for name in feature_names)
    if not names:
        raise ModelInputError("At least one feature is required")
    if len(names) != len(set(names)):
        raise ModelInputError("feature_names must not contain duplicates")
    missing = [name for name in names if name not in X.columns]
    if missing:
        raise ModelInputError("X is missing required features: %s" % ", ".join(missing))
    selected = X.loc[:, list(names)]
    non_numeric = [
        name
        for name in names
        if not is_numeric_dtype(selected[name].dtype)
        or is_bool_dtype(selected[name].dtype)
    ]
    if non_numeric:
        raise ModelInputError(
            "Features must be numeric (not bool): %s" % ", ".join(non_numeric)
        )
    if any(np.iscomplexobj(selected[name].to_numpy()) for name in names):
        raise ModelInputError("Complex-valued features are not supported")
    values = selected.to_numpy(dtype=float, copy=True)
    if np.isinf(values).any():
        raise ModelInputError("X must not contain positive or negative infinity")
    if not allow_nan and np.isnan(values).any():
        raise ModelInputError("X must not contain missing feature values")
    return selected, values


def validate_target(y: object, X: pd.DataFrame) -> np.ndarray:
    """Validate a finite, one-dimensional training target aligned with X."""

    if y is None:
        raise ModelInputError("y is required for supervised model fitting")
    if isinstance(y, pd.DataFrame):
        if y.shape[1] != 1:
            raise ModelInputError("y must be one-dimensional")
        y = y.iloc[:, 0]
    if isinstance(y, pd.Series):
        if not y.index.equals(X.index):
            raise ModelInputError("X and y indexes must be identical and in the same order")
        if not is_numeric_dtype(y.dtype) or is_bool_dtype(y.dtype):
            raise ModelInputError("y must be numeric (not bool)")
        raw_values = y.to_numpy(copy=True)
        if np.iscomplexobj(raw_values):
            raise ModelInputError("Complex-valued targets are not supported")
        values = raw_values.astype(float, copy=False)
    else:
        values = np.asarray(y)
        if values.ndim != 1:
            raise ModelInputError("y must be one-dimensional")
        if values.dtype.kind not in "iuf":
            raise ModelInputError("y must be numeric")
        if np.iscomplexobj(values):
            raise ModelInputError("Complex-valued targets are not supported")
        values = values.astype(float, copy=False)
    if len(values) != len(X):
        raise ModelInputError("X and y must contain the same number of rows")
    if not np.isfinite(values).all():
        raise ModelInputError(
            "y must contain only finite labels; filter unlabeled tail rows before fit"
        )
    return values
