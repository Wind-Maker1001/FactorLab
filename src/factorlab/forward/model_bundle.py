"""Leakage-safe fitting and immutable persistence for forward models.

This module deliberately has no model-selection API.  A caller must name one
model and provide one already frozen parameter mapping.  In particular, this
module never reads walk-forward validation or historical Test results.

``model.pkl`` is a pickle and therefore must only be loaded from a trusted
local artifact store.  Hash verification happens before unpickling to catch
accidental corruption or modification; hashes are not a substitute for a
cryptographic signature from a separately trusted authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pickle
import re
import shutil
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Type
import uuid

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_numeric_dtype

from factorlab.models.base import ResearchModel
from factorlab.forward.integrity import (
    ForwardIntegrityError,
    assert_no_link_components,
)


MODEL_FILENAME = "model.pkl"
METADATA_FILENAME = "metadata.json"
BUNDLE_FORMAT_VERSION = 2

_MODEL_NAMES = frozenset(("factor_score", "ridge", "lightgbm", "xgboost"))
_MODEL_TYPES = {
    "factor_score": "factorlab.models.factor_score.FactorScoreModel",
    "ridge": "factorlab.models.ridge.RidgeResearchModel",
    "lightgbm": "factorlab.models.lightgbm.LightGBMResearchModel",
    "xgboost": "factorlab.models.xgboost.XGBoostResearchModel",
}
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_CORE_METADATA_FIELDS = frozenset(
    {
        "bundle_format_version",
        "model_name",
        "model_version",
        "model_type",
        "feature_names",
        "target",
        "training_asof",
        "max_label_exit_date",
        "source_dataset_sha256",
        "source_sha256",
        "universe_version",
        "research_contract_sha256",
        "git_commit",
        "seed",
        "parameters",
        "training_rows",
        "frozen_at",
    }
)
_INTEGRITY_METADATA_FIELDS = frozenset(
    {"metadata_sha256", "model_sha256", "content_sha256"}
)
_ALL_METADATA_FIELDS = _CORE_METADATA_FIELDS | _INTEGRITY_METADATA_FIELDS

_CANONICAL_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CANONICAL_UTC_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)
_FINAL_FIT_AUTHORITY = object()


class ModelBundleError(ValueError):
    """Raised when fitting inputs or a forward bundle violate the contract."""


class BundleIntegrityError(ModelBundleError):
    """Raised when a bundle is incomplete, inconsistent, or modified."""


class BundleExistsError(FileExistsError):
    """Raised when an immutable model version already exists or is being written."""


@dataclass(frozen=True)
class FinalModelFit:
    """One explicitly specified model fitted on labels known by ``training_asof``."""

    model: ResearchModel
    model_name: str
    parameters: Mapping[str, Any]
    feature_names: Tuple[str, ...]
    target: str
    training_asof: str
    max_label_exit_date: str
    seed: int
    training_rows: int
    _authority: object = field(default=None, init=False, repr=False, compare=False)
    _identity_sha256: str = field(
        default="", init=False, repr=False, compare=False
    )

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        """Delegate prediction to the fitted research model."""

        return self.model.predict(frame)


@dataclass(frozen=True)
class FrozenModelBundle:
    """A verified fitted model and the immutable metadata that identifies it."""

    model: ResearchModel
    metadata: Mapping[str, Any]
    path: Path

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        """Score a frame with the exact feature contract recorded in metadata."""

        return self.model.predict(frame)

    @property
    def model_name(self) -> str:
        return str(self.metadata["model_name"])

    @property
    def model_version(self) -> str:
        return str(self.metadata["model_version"])

    @property
    def feature_names(self) -> Tuple[str, ...]:
        return tuple(self.metadata["feature_names"])

    def __fspath__(self) -> str:
        return os.fspath(self.path)


def _canonical_json_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ModelBundleError("bundle metadata must be finite JSON data") from exc
    return encoded.encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _content_sha256(metadata_sha256: str, model_sha256: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"qbot-forward-model-bundle-v2\0")
    digest.update(metadata_sha256.encode("ascii"))
    digest.update(b"\0")
    digest.update(model_sha256.encode("ascii"))
    return digest.hexdigest()


def _hash_value(name: str, value: object, lengths: Tuple[int, ...]) -> str:
    if not isinstance(value, str):
        raise ModelBundleError("%s must be a hexadecimal hash" % name)
    canonical = value.strip().lower()
    if len(canonical) not in lengths or any(
        character not in "0123456789abcdef" for character in canonical
    ):
        raise ModelBundleError("%s must be a hexadecimal hash" % name)
    return canonical


def _seed(value: object) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise ModelBundleError("seed must be a non-negative integer")
    result = int(value)
    if result < 0:
        raise ModelBundleError("seed must be a non-negative integer")
    return result


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ModelBundleError("%s must be a non-empty canonical string" % label)
    return value


def _feature_names(values: Sequence[str]) -> Tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ModelBundleError("feature_names must be a sequence of column names")
    try:
        names = tuple(values)
    except TypeError as exc:
        raise ModelBundleError(
            "feature_names must be a sequence of column names"
        ) from exc
    if not names or any(
        not isinstance(name, str) or not name or name != name.strip()
        for name in names
    ):
        raise ModelBundleError("feature_names must contain non-empty strings")
    if len(names) != len(set(names)):
        raise ModelBundleError("feature_names must not contain duplicates")
    return names


def _strict_date_value(
    value: object,
    name: str,
    *,
    allow_missing: bool,
) -> Optional[pd.Timestamp]:
    """Return one tz-naive midnight date without timezone conversion.

    Financial cut-offs are calendar dates in the exchange timezone.  Converting
    an aware timestamp to UTC before normalising can silently move it to the
    previous date, while parsing an integer can silently interpret nanoseconds
    since epoch.  Both behaviours are unsafe at a training boundary.
    """

    missing = (
        value is None
        or value is pd.NaT
        or value is pd.NA
        or isinstance(value, (float, np.floating))
        and np.isnan(value)
        or isinstance(value, np.datetime64)
        and np.isnat(value)
    )
    if missing:
        if allow_missing:
            return None
        raise ModelBundleError("%s must be a valid canonical date" % name)

    if isinstance(value, (bool, np.bool_, int, np.integer, float, np.floating)):
        raise ModelBundleError(
            "%s must be a tz-naive date, not a numeric timestamp" % name
        )

    if isinstance(value, str):
        if not _CANONICAL_DATE_PATTERN.fullmatch(value):
            raise ModelBundleError("%s must use canonical YYYY-MM-DD format" % name)
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ModelBundleError("%s must be a valid calendar date" % name) from exc
        return pd.Timestamp(parsed)

    if isinstance(value, datetime):
        timestamp = pd.Timestamp(value)
    elif isinstance(value, date):
        timestamp = pd.Timestamp(value)
    elif isinstance(value, (pd.Timestamp, np.datetime64)):
        try:
            timestamp = pd.Timestamp(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ModelBundleError("%s must be a valid calendar date" % name) from exc
    else:
        raise ModelBundleError(
            "%s must be a canonical date or tz-naive midnight datetime" % name
        )

    if pd.isna(timestamp):
        if allow_missing:
            return None
        raise ModelBundleError("%s must be a valid canonical date" % name)
    if timestamp.tz is not None:
        raise ModelBundleError("%s must be timezone-naive" % name)
    if timestamp != timestamp.normalize():
        raise ModelBundleError("%s must be midnight with no time component" % name)
    return timestamp.normalize()


def _asof(value: object, name: str) -> Tuple[pd.Timestamp, str]:
    timestamp = _strict_date_value(value, name, allow_missing=False)
    assert timestamp is not None
    return timestamp, timestamp.date().isoformat()


def _date_series(values: pd.Series, name: str) -> pd.Series:
    if not isinstance(values, pd.Series):
        raise ModelBundleError("%s must be a pandas Series" % name)
    if is_numeric_dtype(values.dtype) or is_bool_dtype(values.dtype):
        raise ModelBundleError(
            "%s must not contain numeric timestamps" % name
        )
    if isinstance(values.dtype, pd.DatetimeTZDtype):
        raise ModelBundleError("%s must be timezone-naive" % name)

    converted = []
    for row_number, value in enumerate(values.tolist()):
        try:
            converted.append(
                _strict_date_value(value, name, allow_missing=True)
            )
        except ModelBundleError as exc:
            raise ModelBundleError(
                "%s contains an invalid value at position %d: %s"
                % (name, row_number, exc)
            ) from exc
    return pd.Series(converted, index=values.index, dtype="datetime64[ns]")


def _utc_now() -> datetime:
    """Clock seam used only to create the non-caller-controlled freeze receipt."""

    return datetime.now(timezone.utc)


def _frozen_at_now() -> str:
    observed = _utc_now()
    if not isinstance(observed, datetime) or observed.tzinfo is None:
        raise ModelBundleError("internal freeze clock must return an aware datetime")
    return observed.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _validate_frozen_at(value: object) -> str:
    if not isinstance(value, str) or not _CANONICAL_UTC_PATTERN.fullmatch(value):
        raise ModelBundleError("frozen_at must be a canonical UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise ModelBundleError("frozen_at must be a valid UTC timestamp") from exc
    return value


def _parameter_mapping(params: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(params, Mapping):
        raise ModelBundleError("params must be one explicit parameter mapping")
    supplied = dict(params)
    if any(not isinstance(key, str) for key in supplied):
        raise ModelBundleError("parameter names must be strings")
    return supplied


def _reject_unknown_parameters(
    model_name: str, supplied: Mapping[str, Any], allowed: Sequence[str]
) -> None:
    unknown = sorted(set(supplied).difference(allowed))
    if unknown:
        raise ModelBundleError(
            "%s has unsupported frozen parameters: %s"
            % (model_name, ", ".join(unknown))
        )


def _model_class(model_name: str) -> Type[ResearchModel]:
    """Import only the selected implementation; native tree libraries stay lazy."""

    if model_name == "factor_score":
        from factorlab.models.factor_score import FactorScoreModel

        return FactorScoreModel
    if model_name == "ridge":
        from factorlab.models.ridge import RidgeResearchModel

        return RidgeResearchModel
    if model_name == "lightgbm":
        from factorlab.models.lightgbm import LightGBMResearchModel

        return LightGBMResearchModel
    if model_name == "xgboost":
        from factorlab.models.xgboost import XGBoostResearchModel

        return XGBoostResearchModel
    raise ModelBundleError(
        "model_name must be one of: factor_score, ridge, lightgbm, xgboost"
    )


def _construct_model(
    model_name: str,
    params: Mapping[str, Any],
    feature_names: Tuple[str, ...],
    seed: int,
) -> Tuple[ResearchModel, Dict[str, Any]]:
    supplied = _parameter_mapping(params)

    if model_name == "factor_score":
        allowed = ("weights", "normalize_by_absolute_weight")
        _reject_unknown_parameters(model_name, supplied, allowed)
        if "weights" not in supplied:
            raise ModelBundleError("factor_score params must explicitly contain weights")
        weights = supplied["weights"]
        if not isinstance(weights, Mapping):
            raise ModelBundleError("factor_score weights must be a mapping")
        if set(weights) != set(feature_names):
            raise ModelBundleError(
                "factor_score weight names must exactly match feature_names"
            )
        ordered_weights = {name: weights[name] for name in feature_names}
        normalize = supplied.get("normalize_by_absolute_weight", False)
        model_class = _model_class(model_name)
        model = model_class(
            ordered_weights,
            normalize_by_absolute_weight=normalize,
        )
        resolved = {
            "weights": {name: float(model.weights[name]) for name in feature_names},
            "normalize_by_absolute_weight": bool(
                model.normalize_by_absolute_weight
            ),
        }
        return model, resolved

    if model_name == "ridge":
        allowed = ("alpha", "fit_intercept", "max_iter", "tolerance")
        _reject_unknown_parameters(model_name, supplied, allowed)
        if "alpha" not in supplied:
            raise ModelBundleError("ridge params must explicitly contain alpha")
        values = {
            "alpha": supplied["alpha"],
            "fit_intercept": supplied.get("fit_intercept", True),
            "max_iter": supplied.get("max_iter", 10000),
            "tolerance": supplied.get("tolerance", 0.0001),
        }
        model_class = _model_class(model_name)
        model = model_class(
            feature_names=feature_names,
            random_seed=seed,
            **values,
        )
        resolved = {
            "alpha": float(model.alpha),
            "fit_intercept": bool(model.fit_intercept),
            "max_iter": int(model.max_iter),
            "tolerance": float(model.tolerance),
        }
        return model, resolved

    if model_name == "lightgbm":
        defaults: Dict[str, Any] = {
            "n_estimators": 200,
            "learning_rate": 0.03,
            "num_leaves": 15,
            "max_depth": 5,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 1,
            "reg_alpha": 0.0,
            "reg_lambda": 1.0,
            "min_child_samples": 20,
            "n_jobs": 1,
        }
    elif model_name == "xgboost":
        defaults = {
            "n_estimators": 200,
            "learning_rate": 0.03,
            "max_depth": 3,
            "min_child_weight": 1.0,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "reg_alpha": 0.0,
            "reg_lambda": 1.0,
            "gamma": 0.0,
            "n_jobs": 1,
        }
    else:
        # Keep this check local even though callers validate first; it prevents a
        # future branch from accidentally acquiring an automatic "best" model.
        _model_class(model_name)
        raise AssertionError("unreachable")

    _reject_unknown_parameters(model_name, supplied, tuple(defaults))
    if "n_estimators" not in supplied:
        raise ModelBundleError(
            "%s params must explicitly contain n_estimators" % model_name
        )
    values = dict(defaults)
    values.update(supplied)
    model_class = _model_class(model_name)
    model = model_class(
        feature_names=feature_names,
        random_seed=seed,
        **values,
    )
    resolved = {name: getattr(model, name) for name in defaults}
    # Constructors normalise NumPy and numeric ABC values.  Convert once more
    # so the persisted contract contains only portable JSON scalar types.
    resolved = {
        name: (
            int(value)
            if isinstance(value, (int, np.integer)) and not isinstance(value, bool)
            else float(value)
            if isinstance(value, (float, np.floating))
            else value
        )
        for name, value in resolved.items()
    }
    return model, resolved


def _pickle_model_for_identity(model: ResearchModel) -> bytes:
    try:
        return pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)
    except (pickle.PickleError, TypeError, AttributeError) as exc:
        raise ModelBundleError("could not serialise fitted model identity") from exc


def _final_fit_identity_sha256(fitted: FinalModelFit) -> str:
    """Bind every audit field to the exact in-memory fitted estimator state."""

    core = {
        "format": "qbot-final-model-fit-v1",
        "model_sha256": _sha256(_pickle_model_for_identity(fitted.model)),
        "model_name": fitted.model_name,
        "parameters": fitted.parameters,
        "feature_names": list(fitted.feature_names),
        "target": fitted.target,
        "training_asof": fitted.training_asof,
        "max_label_exit_date": fitted.max_label_exit_date,
        "seed": fitted.seed,
        "training_rows": fitted.training_rows,
    }
    return _sha256(_canonical_json_bytes(core))


def _authorise_final_fit(fitted: FinalModelFit) -> FinalModelFit:
    object.__setattr__(fitted, "_authority", _FINAL_FIT_AUTHORITY)
    object.__setattr__(fitted, "_identity_sha256", _final_fit_identity_sha256(fitted))
    return fitted


def fit_final_model(
    data: pd.DataFrame,
    *,
    model_name: str,
    params: Mapping[str, Any],
    feature_names: Sequence[str],
    target: str,
    training_asof: object,
    seed: int = 42,
    date_col: str = "date",
    exit_date_col: str = "exit_date",
) -> FinalModelFit:
    """Fit exactly one pre-specified model without consulting historical Test.

    A row is eligible only when its signal date and label exit date are both no
    later than ``training_asof`` and its target is finite.  This filtering is
    completed before any imputer, scaler, or estimator sees the data.
    """

    if not isinstance(data, pd.DataFrame):
        raise ModelBundleError("data must be a pandas DataFrame")
    if data.empty:
        raise ModelBundleError("data must contain at least one row")
    if data.columns.has_duplicates:
        raise ModelBundleError("data must not contain duplicate column names")
    canonical_model_name = _name(model_name, "model_name")
    if canonical_model_name not in _MODEL_NAMES:
        raise ModelBundleError(
            "model_name must be one of: factor_score, ridge, lightgbm, xgboost"
        )
    names = _feature_names(feature_names)
    target_name = _name(target, "target")
    signal_column = _name(date_col, "date_col")
    exit_column = _name(exit_date_col, "exit_date_col")
    leakage_columns = {target_name, signal_column, exit_column}
    overlap = [name for name in names if name in leakage_columns]
    if overlap:
        raise ModelBundleError(
            "feature_names must not contain target/date audit columns: %s"
            % ", ".join(overlap)
        )
    required = list(names) + [target_name, signal_column, exit_column]
    missing = [name for name in required if name not in data.columns]
    if missing:
        raise ModelBundleError(
            "data is missing required columns: %s" % ", ".join(dict.fromkeys(missing))
        )
    if not is_numeric_dtype(data[target_name].dtype) or is_bool_dtype(
        data[target_name].dtype
    ):
        raise ModelBundleError("target must be numeric (not bool)")

    cutoff, cutoff_text = _asof(training_asof, "training_asof")
    signal_dates = _date_series(data[signal_column], signal_column)
    exit_dates = _date_series(data[exit_column], exit_column)
    try:
        numeric_target = data[target_name].astype(float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ModelBundleError("target must be numeric (not bool)") from exc
    finite_target = pd.Series(
        np.isfinite(numeric_target.to_numpy(dtype=float, copy=False)),
        index=data.index,
    )
    eligible = (
        signal_dates.notna()
        & exit_dates.notna()
        & signal_dates.le(cutoff)
        & exit_dates.le(cutoff)
        & finite_target
    )
    training = data.loc[eligible]
    if training.empty:
        raise ModelBundleError(
            "no finite labels with date and exit_date known by training_asof"
        )
    training_exit_dates = exit_dates.loc[eligible]
    max_exit = training_exit_dates.max()
    if pd.isna(max_exit) or max_exit > cutoff:
        raise ModelBundleError("training labels extend beyond training_asof")

    canonical_seed = _seed(seed)
    model, resolved = _construct_model(
        canonical_model_name,
        params,
        names,
        canonical_seed,
    )
    model.fit(
        training.loc[:, list(names)],
        numeric_target.loc[eligible],
    )
    return _authorise_final_fit(
        FinalModelFit(
            model=model,
            model_name=canonical_model_name,
            parameters=json.loads(_canonical_json_bytes(resolved).decode("utf-8")),
            feature_names=names,
            target=target_name,
            training_asof=cutoff_text,
            max_label_exit_date=max_exit.date().isoformat(),
            seed=canonical_seed,
            training_rows=int(eligible.sum()),
        )
    )


def _resolved_model_parameters(model_name: str, model: ResearchModel) -> Dict[str, Any]:
    if model_name == "factor_score":
        return {
            "weights": {
                name: float(model.weights[name]) for name in model.feature_names
            },
            "normalize_by_absolute_weight": bool(
                model.normalize_by_absolute_weight
            ),
        }
    if model_name == "ridge":
        return {
            "alpha": float(model.alpha),
            "fit_intercept": bool(model.fit_intercept),
            "max_iter": int(model.max_iter),
            "tolerance": float(model.tolerance),
        }
    names = (
        (
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
            "n_jobs",
        )
        if model_name == "lightgbm"
        else (
            "n_estimators",
            "learning_rate",
            "max_depth",
            "min_child_weight",
            "subsample",
            "colsample_bytree",
            "reg_alpha",
            "reg_lambda",
            "gamma",
            "n_jobs",
        )
    )
    result: Dict[str, Any] = {}
    for name in names:
        value = getattr(model, name)
        if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
            result[name] = int(value)
        elif isinstance(value, (float, np.floating)):
            result[name] = float(value)
        else:
            result[name] = value
    return result


def _fitted_feature_names(model: ResearchModel) -> Tuple[str, ...]:
    try:
        return tuple(model.feature_names)
    except (AttributeError, TypeError, RuntimeError) as exc:
        raise BundleIntegrityError(
            "model does not expose fitted feature_names"
        ) from exc


def _validate_fit_identity(fitted: FinalModelFit) -> None:
    if not isinstance(fitted, FinalModelFit):
        raise ModelBundleError("fitted must be the result of fit_final_model")
    if fitted._authority is not _FINAL_FIT_AUTHORITY or not fitted._identity_sha256:
        raise ModelBundleError("fitted must be the unmodified result of fit_final_model")
    if fitted._identity_sha256 != _final_fit_identity_sha256(fitted):
        raise ModelBundleError(
            "fitted model or its training audit fields changed after fitting"
        )
    expected_class = _model_class(fitted.model_name)
    if type(fitted.model) is not expected_class:
        raise ModelBundleError("fitted model type does not match model_name")
    if not fitted.model.is_fitted:
        raise ModelBundleError("fitted model is not fitted")
    if _fitted_feature_names(fitted.model) != tuple(fitted.feature_names):
        raise ModelBundleError("fitted model feature order has changed")
    resolved = _resolved_model_parameters(fitted.model_name, fitted.model)
    if _canonical_json_bytes(resolved) != _canonical_json_bytes(fitted.parameters):
        raise ModelBundleError("fitted model parameters have changed")
    if fitted.model_name != "factor_score" and int(fitted.model.random_seed) != fitted.seed:
        raise ModelBundleError("fitted model seed has changed")


def _model_version(model_version: Optional[str], version: Optional[str]) -> str:
    if model_version is not None and version is not None:
        raise ModelBundleError("provide model_version or version, not both")
    value = model_version if model_version is not None else version
    if not isinstance(value, str) or not _VERSION_PATTERN.fullmatch(value):
        raise ModelBundleError(
            "model_version must be a safe 1-128 character version name"
        )
    return value


def _write_file(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def save_model_bundle(
    fitted: FinalModelFit,
    bundle_root: Path,
    *,
    model_version: Optional[str] = None,
    version: Optional[str] = None,
    source_dataset_sha256: str,
    source_sha256: str,
    universe_version: str,
    research_contract_sha256: str,
    git_commit: str,
) -> FrozenModelBundle:
    """Atomically publish one new, never-overwritten model version directory."""

    _validate_fit_identity(fitted)
    canonical_version = _model_version(model_version, version)
    dataset_hash = _hash_value(
        "source_dataset_sha256", source_dataset_sha256, (64,)
    )
    source_hash = _hash_value("source_sha256", source_sha256, (64,))
    universe_hash = _hash_value("universe_version", universe_version, (64,))
    contract_hash = _hash_value(
        "research_contract_sha256", research_contract_sha256, (64,)
    )
    commit_hash = _hash_value("git_commit", git_commit, (40, 64))

    core: Dict[str, Any] = {
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "model_name": fitted.model_name,
        "model_version": canonical_version,
        "model_type": _MODEL_TYPES[fitted.model_name],
        "feature_names": list(fitted.feature_names),
        "target": fitted.target,
        "training_asof": fitted.training_asof,
        "max_label_exit_date": fitted.max_label_exit_date,
        "source_dataset_sha256": dataset_hash,
        "source_sha256": source_hash,
        "universe_version": universe_hash,
        "research_contract_sha256": contract_hash,
        "git_commit": commit_hash,
        "seed": fitted.seed,
        "parameters": json.loads(
            _canonical_json_bytes(fitted.parameters).decode("utf-8")
        ),
        "training_rows": fitted.training_rows,
        "frozen_at": _frozen_at_now(),
    }
    metadata_hash = _sha256(_canonical_json_bytes(core))

    # Never mutate the authorised FinalModelFit after its identity digest was
    # created.  Bind a serialization clone to this bundle's metadata instead.
    # This also ensures a failed/existing publication attempt cannot alter the
    # caller's fitted object.
    try:
        persisted_model = pickle.loads(_pickle_model_for_identity(fitted.model))
        setattr(persisted_model, "_forward_bundle_metadata_sha256", metadata_hash)
        model_payload = pickle.dumps(
            persisted_model, protocol=pickle.HIGHEST_PROTOCOL
        )
    except (pickle.PickleError, TypeError, AttributeError) as exc:
        raise ModelBundleError("could not serialise fitted model") from exc
    model_hash = _sha256(model_payload)
    metadata = dict(core)
    metadata.update(
        {
            "metadata_sha256": metadata_hash,
            "model_sha256": model_hash,
            "content_sha256": _content_sha256(metadata_hash, model_hash),
        }
    )
    metadata_payload = json.dumps(
        metadata,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8") + b"\n"

    try:
        root = assert_no_link_components(Path(bundle_root))
    except ForwardIntegrityError as exc:
        raise ModelBundleError(str(exc)) from exc
    model_parent = root / fitted.model_name
    destination = model_parent / canonical_version
    model_parent.mkdir(parents=True, exist_ok=True)
    try:
        assert_no_link_components(model_parent)
        assert_no_link_components(destination)
    except ForwardIntegrityError as exc:
        raise ModelBundleError(str(exc)) from exc
    if destination.exists() or destination.is_symlink():
        raise BundleExistsError(
            "model version already exists and cannot be overwritten: %s"
            % destination
        )

    lock_path = model_parent / (".%s.lock" % canonical_version)
    try:
        lock_descriptor = os.open(
            str(lock_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError as exc:
        raise BundleExistsError(
            "model version is already being published: %s" % canonical_version
        ) from exc
    staging = model_parent / (
        ".%s.tmp-%s" % (canonical_version, uuid.uuid4().hex)
    )
    try:
        os.write(lock_descriptor, (str(os.getpid()) + "\n").encode("ascii"))
        os.close(lock_descriptor)
        lock_descriptor = -1
        if destination.exists() or destination.is_symlink():
            raise BundleExistsError(
                "model version already exists and cannot be overwritten: %s"
                % destination
            )
        staging.mkdir(exist_ok=False)
        _write_file(staging / MODEL_FILENAME, model_payload)
        _write_file(staging / METADATA_FILENAME, metadata_payload)
        os.rename(str(staging), str(destination))
    except Exception:
        if staging.exists():
            shutil.rmtree(str(staging))
        raise
    finally:
        if lock_descriptor != -1:
            os.close(lock_descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass

    return FrozenModelBundle(
        model=persisted_model,
        metadata=metadata,
        path=destination,
    )


def _unique_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BundleIntegrityError("metadata contains duplicate key: %s" % key)
        result[key] = value
    return result


def _read_metadata(path: Path) -> Dict[str, Any]:
    try:
        payload = path.read_text(encoding="utf-8")
        metadata = json.loads(payload, object_pairs_hook=_unique_object)
    except BundleIntegrityError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise BundleIntegrityError("metadata.json is not valid UTF-8 JSON") from exc
    if not isinstance(metadata, dict):
        raise BundleIntegrityError("metadata.json must contain one object")
    fields = set(metadata)
    missing = sorted(_ALL_METADATA_FIELDS.difference(fields))
    extra = sorted(fields.difference(_ALL_METADATA_FIELDS))
    if missing or extra:
        details = []
        if missing:
            details.append("missing: %s" % ", ".join(missing))
        if extra:
            details.append("unexpected: %s" % ", ".join(extra))
        raise BundleIntegrityError(
            "metadata fields do not match bundle schema (%s)" % "; ".join(details)
        )
    return metadata


def _validate_metadata(metadata: Mapping[str, Any], path: Path) -> Dict[str, Any]:
    if metadata["bundle_format_version"] != BUNDLE_FORMAT_VERSION:
        raise BundleIntegrityError("unsupported bundle_format_version")
    model_name = metadata["model_name"]
    if not isinstance(model_name, str) or model_name not in _MODEL_NAMES:
        raise BundleIntegrityError("metadata model_name is invalid")
    version = metadata["model_version"]
    if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
        raise BundleIntegrityError("metadata model_version is invalid")
    if path.name != version:
        raise BundleIntegrityError("version directory name does not match metadata")
    if metadata["model_type"] != _MODEL_TYPES[model_name]:
        raise BundleIntegrityError("metadata model_name and model_type do not match")
    try:
        names = _feature_names(metadata["feature_names"])
        target = _name(metadata["target"], "target")
        _, training_asof = _asof(metadata["training_asof"], "training_asof")
        _, max_exit = _asof(
            metadata["max_label_exit_date"], "max_label_exit_date"
        )
        if training_asof != metadata["training_asof"]:
            raise ModelBundleError("training_asof is not canonical")
        if max_exit != metadata["max_label_exit_date"]:
            raise ModelBundleError("max_label_exit_date is not canonical")
        if max_exit > training_asof:
            raise ModelBundleError("max_label_exit_date exceeds training_asof")
        dataset_hash = _hash_value(
            "source_dataset_sha256", metadata["source_dataset_sha256"], (64,)
        )
        source_hash = _hash_value(
            "source_sha256", metadata["source_sha256"], (64,)
        )
        universe_hash = _hash_value(
            "universe_version", metadata["universe_version"], (64,)
        )
        contract_hash = _hash_value(
            "research_contract_sha256",
            metadata["research_contract_sha256"],
            (64,),
        )
        commit_hash = _hash_value("git_commit", metadata["git_commit"], (40, 64))
        frozen_at = _validate_frozen_at(metadata["frozen_at"])
        seed = _seed(metadata["seed"])
        rows = metadata["training_rows"]
        if isinstance(rows, bool) or not isinstance(rows, int) or rows <= 0:
            raise ModelBundleError("training_rows must be a positive integer")
        if not isinstance(metadata["parameters"], dict):
            raise ModelBundleError("parameters must be an object")
        _canonical_json_bytes(metadata["parameters"])
        metadata_hash = _hash_value(
            "metadata_sha256", metadata["metadata_sha256"], (64,)
        )
        model_hash = _hash_value("model_sha256", metadata["model_sha256"], (64,))
        content_hash = _hash_value(
            "content_sha256", metadata["content_sha256"], (64,)
        )
    except ModelBundleError as exc:
        raise BundleIntegrityError(str(exc)) from exc

    canonical = dict(metadata)
    canonical.update(
        {
            "feature_names": list(names),
            "target": target,
            "training_asof": training_asof,
            "max_label_exit_date": max_exit,
            "source_dataset_sha256": dataset_hash,
            "source_sha256": source_hash,
            "universe_version": universe_hash,
            "research_contract_sha256": contract_hash,
            "git_commit": commit_hash,
            "frozen_at": frozen_at,
            "seed": seed,
            "metadata_sha256": metadata_hash,
            "model_sha256": model_hash,
            "content_sha256": content_hash,
        }
    )
    core = {name: canonical[name] for name in _CORE_METADATA_FIELDS}
    actual_metadata_hash = _sha256(_canonical_json_bytes(core))
    if actual_metadata_hash != metadata_hash:
        raise BundleIntegrityError("metadata_sha256 does not match metadata content")
    if _content_sha256(metadata_hash, model_hash) != content_hash:
        raise BundleIntegrityError("content_sha256 does not match bundle digests")
    return canonical


def load_model_bundle(
    bundle_path: Path,
    *,
    expected_model_name: Optional[str] = None,
    expected_feature_names: Optional[Sequence[str]] = None,
    expected_source_sha256: Optional[str] = None,
) -> FrozenModelBundle:
    """Load and verify a trusted local forward-model bundle.

    Hashes and the complete two-file layout are checked before unpickling.  The
    loaded class, fitted state, parameter values, seed, and exact feature order
    are then checked against metadata.
    """

    try:
        path = assert_no_link_components(Path(bundle_path))
    except ForwardIntegrityError as exc:
        raise BundleIntegrityError(str(exc)) from exc
    if path.is_symlink() or not path.is_dir():
        raise BundleIntegrityError("bundle_path must be a local directory")
    try:
        entries = list(path.iterdir())
    except OSError as exc:
        raise BundleIntegrityError("bundle directory cannot be read") from exc
    names = {entry.name for entry in entries}
    expected_files = {MODEL_FILENAME, METADATA_FILENAME}
    if names != expected_files:
        missing = sorted(expected_files.difference(names))
        extra = sorted(names.difference(expected_files))
        details = []
        if missing:
            details.append("missing files: %s" % ", ".join(missing))
        if extra:
            details.append("unexpected files: %s" % ", ".join(extra))
        raise BundleIntegrityError(
            "bundle must contain exactly model.pkl and metadata.json (%s)"
            % "; ".join(details)
        )
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            raise BundleIntegrityError("bundle files must be regular local files")

    metadata = _validate_metadata(
        _read_metadata(path / METADATA_FILENAME),
        path,
    )
    if expected_model_name is not None:
        requested_name = _name(expected_model_name, "expected_model_name")
        if requested_name != metadata["model_name"]:
            raise BundleIntegrityError("loaded model_name does not match expectation")
    if expected_feature_names is not None:
        requested_features = _feature_names(expected_feature_names)
        if requested_features != tuple(metadata["feature_names"]):
            raise BundleIntegrityError(
                "loaded feature order does not match expectation"
            )
    if expected_source_sha256 is not None:
        requested_source = _hash_value(
            "expected_source_sha256", expected_source_sha256, (64,)
        )
        if requested_source != metadata["source_sha256"]:
            raise BundleIntegrityError(
                "loaded source digest does not match the current research code"
            )

    try:
        model_payload = (path / MODEL_FILENAME).read_bytes()
    except OSError as exc:
        raise BundleIntegrityError("model.pkl cannot be read") from exc
    if _sha256(model_payload) != metadata["model_sha256"]:
        raise BundleIntegrityError("model_sha256 does not match model.pkl")

    # Do not move this before the file hash check.  Pickle is only appropriate
    # for the trusted local store documented by this module.
    try:
        model = pickle.loads(model_payload)
    except Exception as exc:
        raise BundleIntegrityError("model.pkl could not be deserialised") from exc
    expected_class = _model_class(metadata["model_name"])
    if type(model) is not expected_class:
        raise BundleIntegrityError("model.pkl type does not match model_name")
    if not model.is_fitted:
        raise BundleIntegrityError("model.pkl does not contain a fitted model")
    if getattr(model, "_forward_bundle_metadata_sha256", None) != metadata[
        "metadata_sha256"
    ]:
        raise BundleIntegrityError("model.pkl is not bound to this metadata")
    if _fitted_feature_names(model) != tuple(metadata["feature_names"]):
        raise BundleIntegrityError(
            "model feature order does not match metadata feature_names"
        )
    resolved = _resolved_model_parameters(metadata["model_name"], model)
    if _canonical_json_bytes(resolved) != _canonical_json_bytes(
        metadata["parameters"]
    ):
        raise BundleIntegrityError("model parameters do not match metadata")
    if metadata["model_name"] != "factor_score" and int(model.random_seed) != metadata[
        "seed"
    ]:
        raise BundleIntegrityError("model seed does not match metadata")

    return FrozenModelBundle(model=model, metadata=metadata, path=path)


def fit_and_save_model_bundle(
    data: pd.DataFrame,
    bundle_root: Path,
    *,
    model_name: str,
    params: Mapping[str, Any],
    feature_names: Sequence[str],
    target: str,
    training_asof: object,
    source_dataset_sha256: str,
    source_sha256: str,
    universe_version: str,
    research_contract_sha256: str,
    git_commit: str,
    model_version: Optional[str] = None,
    version: Optional[str] = None,
    seed: int = 42,
    date_col: str = "date",
    exit_date_col: str = "exit_date",
) -> FrozenModelBundle:
    """Fit and atomically publish one explicit forward model specification."""

    fitted = fit_final_model(
        data,
        model_name=model_name,
        params=params,
        feature_names=feature_names,
        target=target,
        training_asof=training_asof,
        seed=seed,
        date_col=date_col,
        exit_date_col=exit_date_col,
    )
    return save_model_bundle(
        fitted,
        bundle_root,
        model_version=model_version,
        version=version,
        source_dataset_sha256=source_dataset_sha256,
        source_sha256=source_sha256,
        universe_version=universe_version,
        research_contract_sha256=research_contract_sha256,
        git_commit=git_commit,
    )


__all__ = [
    "BUNDLE_FORMAT_VERSION",
    "BundleExistsError",
    "BundleIntegrityError",
    "FinalModelFit",
    "FrozenModelBundle",
    "METADATA_FILENAME",
    "MODEL_FILENAME",
    "ModelBundleError",
    "fit_and_save_model_bundle",
    "fit_final_model",
    "load_model_bundle",
    "save_model_bundle",
]
