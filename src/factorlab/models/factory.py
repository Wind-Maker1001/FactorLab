"""Explicit, immutable specifications for construction of the four legacy models.

This module constructs original model classes. It has no data, training,
selection, persistence, plugin discovery, or financial admission responsibilities.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from math import isfinite
from numbers import Real
from types import MappingProxyType

from .base import ModelInputError, ResearchModel
from .factor_score import FactorScoreModel
from .lightgbm import LightGBMResearchModel
from .ridge import RidgeResearchModel
from .xgboost import XGBoostResearchModel


class InvalidModelSpec(ModelInputError):
    """Invalid configuration at the new factory boundary."""


class UnknownModelVersion(InvalidModelSpec):
    """No explicitly registered model identity and implementation version."""


ALIASES = MappingProxyType({
    "FactorScore": "factor_score", "Ridge": "ridge",
    "LightGBM": "lightgbm", "XGBoost": "xgboost",
})
PROFILES = frozenset({"legacy_csi500_v1", "cross_market_fixed_v1", "research_v2"})


def _text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidModelSpec(name + " must be a non-empty string")


def _numeric(value: object) -> bool:
    try:
        return not isinstance(value, bool) and isinstance(value, Real) and isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _snapshot(mapping: Mapping, depth: int = 0) -> Mapping:
    if not isinstance(mapping, Mapping) or depth > 1:
        raise InvalidModelSpec("parameters must be a mapping with scalar values or weights")
    result = {}
    for key, value in mapping.items():
        _text(key, "parameter name")
        if key in result:
            raise InvalidModelSpec("duplicate mapping key: " + key)
        if isinstance(value, Mapping):
            value = _snapshot(value, depth + 1)
        elif not isinstance(value, (str, Real, bool, type(None))):
            raise InvalidModelSpec("unsupported parameter value: " + key)
        result[key] = value
    return MappingProxyType(result)


@dataclass(frozen=True)
class FeatureRef:
    column: str
    factor_id: str
    version: str
    definition_hash: str

    def __post_init__(self) -> None:
        for name in ("column", "factor_id", "version", "definition_hash"):
            _text(getattr(self, name), name)

    @classmethod
    def legacy(cls, column: str) -> FeatureRef:
        """A compatibility label only; UNVERIFIED is not a definition digest."""
        return cls(column, column, "legacy-v1", "UNVERIFIED")


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    implementation_version: str
    parameters: Mapping[str, object]
    features: tuple[FeatureRef, ...]
    seed: int | None
    threads: int
    profile_id: str

    def __post_init__(self) -> None:
        for name in ("model_id", "implementation_version", "profile_id"):
            _text(getattr(self, name), name)
        object.__setattr__(self, "parameters", _snapshot(self.parameters))
        if not isinstance(self.features, (tuple, list)):
            raise InvalidModelSpec("features must be an ordered sequence of FeatureRef")
        features = tuple(self.features)
        if not features or any(type(ref) is not FeatureRef for ref in features):
            raise InvalidModelSpec("features must contain FeatureRef values")
        if len({ref.column for ref in features}) != len(features):
            raise InvalidModelSpec("duplicate feature columns")
        object.__setattr__(self, "features", features)


@dataclass(frozen=True)
class ModelCapabilities:
    same_environment_repeatable: bool = True
    supports_partial_fit: bool = False


@dataclass(frozen=True)
class FeatureRequirement:
    ordered_columns: tuple[str, ...]
    nan_policy: str
    all_missing_training_column: str
    target_required: bool
    dtype: str = "float64"
    supports_partial_fit: bool = False
    # These are bound by future training services, never guessed by construction.
    frequency: str | None = None
    market: str | None = None
    price_basis: str | None = None
    max_lookback: int | None = None
    target_id: str | None = None


@dataclass(frozen=True)
class ModelRegistration:
    """A descriptor supplied by trusted Python code, never an import string."""

    model_id: str
    implementation_version: str
    parameter_defaults: Mapping[str, object]
    build: Callable[[ModelSpec], ResearchModel]
    capabilities: ModelCapabilities = ModelCapabilities()

    def __post_init__(self) -> None:
        _text(self.model_id, "model_id")
        _text(self.implementation_version, "implementation_version")
        if self.model_id in ALIASES or not callable(self.build):
            raise InvalidModelSpec("registration requires canonical identity and callable builder")
        object.__setattr__(self, "parameter_defaults", _snapshot(self.parameter_defaults))

    def requirements(self, spec: ModelSpec) -> FeatureRequirement:
        fixed = self.model_id == "factor_score"
        return FeatureRequirement(
            tuple(ref.column for ref in spec.features),
            "neutral_zero" if fixed else "train_median",
            "allowed" if fixed else "reject", not fixed,
        )

    def effective_parameters(self, model: ResearchModel) -> Mapping[str, object]:
        """Constructor fields only; the estimator's unchanged fit owns native options."""
        keys = tuple(self.parameter_defaults)
        values = {key: getattr(model, key) for key in keys}
        if self.model_id == "factor_score":
            values["weights"] = model.weights
        else:
            values["feature_names"] = model.requested_feature_names
            values["random_seed"] = model.random_seed
            if self.model_id in ("lightgbm", "xgboost"):
                values["n_jobs"] = model.n_jobs
        # Feature order is a tuple, outside the scalar parameter snapshot.
        names = values.pop("feature_names", None)
        frozen = dict(_snapshot(values))
        if names is not None:
            frozen["feature_names"] = names
        return MappingProxyType(frozen)


@dataclass(frozen=True)
class ModelCatalogSnapshot:
    registrations: Mapping[tuple[str, str], ModelRegistration]

    def __post_init__(self) -> None:
        copied = dict(self.registrations)
        if any(type(reg) is not ModelRegistration or key != (reg.model_id, reg.implementation_version)
               for key, reg in copied.items()):
            raise InvalidModelSpec("invalid catalog entries")
        object.__setattr__(self, "registrations", MappingProxyType(copied))

    def resolve(self, model_id: str, version: str) -> ModelRegistration:
        _text(model_id, "model_id")
        _text(version, "implementation_version")
        identity = (ALIASES.get(model_id, model_id), version)
        try:
            return self.registrations[identity]
        except KeyError as exc:
            raise UnknownModelVersion("unknown model/version: %s/%s" % identity) from exc


class ModelRegistry:
    def __init__(self) -> None:
        self._registrations = {}
        self._frozen = False

    def register(self, descriptor: ModelRegistration) -> None:
        if self._frozen:
            raise InvalidModelSpec("model registry is frozen")
        if type(descriptor) is not ModelRegistration:
            raise InvalidModelSpec("expected ModelRegistration")
        key = (descriptor.model_id, descriptor.implementation_version)
        if key in self._registrations:
            raise InvalidModelSpec("duplicate model registration: %s/%s" % key)
        self._registrations[key] = descriptor

    def resolve(self, model_id: str, version: str) -> ModelRegistration:
        return ModelCatalogSnapshot(self._registrations).resolve(model_id, version)

    def freeze(self) -> ModelCatalogSnapshot:
        self._frozen = True
        return ModelCatalogSnapshot(self._registrations)


def build_factor_score(spec: ModelSpec) -> FactorScoreModel:
    return FactorScoreModel(**spec.parameters)


def build_ridge(spec: ModelSpec) -> RidgeResearchModel:
    return RidgeResearchModel(feature_names=tuple(ref.column for ref in spec.features),
                              random_seed=spec.seed, **spec.parameters)


def build_lightgbm(spec: ModelSpec) -> LightGBMResearchModel:
    return LightGBMResearchModel(feature_names=tuple(ref.column for ref in spec.features),
                                 random_seed=spec.seed, n_jobs=spec.threads, **spec.parameters)


def build_xgboost(spec: ModelSpec) -> XGBoostResearchModel:
    return XGBoostResearchModel(feature_names=tuple(ref.column for ref in spec.features),
                                random_seed=spec.seed, n_jobs=spec.threads, **spec.parameters)


def _legacy_catalog() -> ModelCatalogSnapshot:
    registry = ModelRegistry()
    definitions = (
        ("factor_score", {"normalize_by_absolute_weight": False}, build_factor_score),
        ("ridge", {"alpha": 1.0, "fit_intercept": True, "max_iter": 10000,
                   "tolerance": 0.0001}, build_ridge),
        ("lightgbm", {"n_estimators": 200, "learning_rate": 0.03, "num_leaves": 15,
                      "max_depth": 5, "feature_fraction": 0.8, "bagging_fraction": 0.8,
                      "bagging_freq": 1, "reg_alpha": 0.0, "reg_lambda": 1.0,
                      "min_child_samples": 20}, build_lightgbm),
        ("xgboost", {"n_estimators": 200, "learning_rate": 0.03, "max_depth": 3,
                     "min_child_weight": 1.0, "subsample": 0.8, "colsample_bytree": 0.8,
                     "reg_alpha": 0.0, "reg_lambda": 1.0, "gamma": 0.0}, build_xgboost),
    )
    for model_id, defaults, builder in definitions:
        registry.register(ModelRegistration(model_id, "legacy-v1", defaults, builder))
    return registry.freeze()


DEFAULT_CATALOG = _legacy_catalog()


@dataclass(frozen=True)
class ValidatedModelSpec:
    spec: ModelSpec

    @property
    def seed_status(self) -> str:
        return "NOT_APPLICABLE" if self.spec.model_id == "factor_score" else "EXPLICIT"


def validate_spec(spec: ModelSpec, catalog: ModelCatalogSnapshot = DEFAULT_CATALOG) -> ValidatedModelSpec:
    """Expand original defaults, enforce the V2 contract, then use original validators.

    The temporary constructor is deliberately unfitted and has no I/O. No
    dependency import or training validation is attempted here.
    """
    if type(spec) is not ModelSpec or type(catalog) is not ModelCatalogSnapshot:
        raise InvalidModelSpec("expected ModelSpec and frozen ModelCatalogSnapshot")
    descriptor = catalog.resolve(spec.model_id, spec.implementation_version)
    model_id = descriptor.model_id
    if spec.profile_id not in PROFILES:
        raise InvalidModelSpec("unknown profile: " + spec.profile_id)
    fixed = model_id == "factor_score"
    if fixed:
        if spec.seed is not None:
            raise InvalidModelSpec("FactorScore seed must be None (NOT_APPLICABLE)")
    elif type(spec.seed) is not int or spec.seed < 0:
        raise InvalidModelSpec("seed must be a non-negative int, not bool")
    if type(spec.threads) is not int or spec.threads < 1:
        raise InvalidModelSpec("threads must be a positive int, not bool")
    if model_id in ("factor_score", "ridge") and spec.threads != 1:
        raise InvalidModelSpec("FactorScore/Ridge require threads=1; no n_jobs parameter")
    allowed = set(descriptor.parameter_defaults) | ({"weights"} if fixed else set())
    unknown = set(spec.parameters) - allowed
    if unknown:
        raise InvalidModelSpec("unknown parameters: " + ", ".join(sorted(unknown)))
    parameters = dict(descriptor.parameter_defaults)
    parameters.update(spec.parameters)
    for key, value in parameters.items():
        if key == "weights":
            continue
        if key in ("normalize_by_absolute_weight", "fit_intercept"):
            if type(value) is not bool:
                raise InvalidModelSpec(key + " must be boolean")
        elif not _numeric(value):
            raise InvalidModelSpec(key + " must be a finite number, not bool")
    if fixed:
        weights = parameters.get("weights")
        columns = tuple(ref.column for ref in spec.features)
        if not isinstance(weights, Mapping) or set(weights) != set(columns):
            raise InvalidModelSpec("weights must exactly match feature columns")
        if any(not _numeric(w) for w in weights.values()):
            raise InvalidModelSpec("weights must be finite numbers, not bool")
        parameters["weights"] = {name: weights[name] for name in columns}
    expanded = replace(spec, model_id=model_id, parameters=parameters)
    try:
        descriptor.build(expanded)
    except (ModelInputError, TypeError, ValueError, OverflowError) as exc:
        raise InvalidModelSpec(str(exc)) from exc
    return ValidatedModelSpec(expanded)


def create_model(spec: ValidatedModelSpec, catalog: ModelCatalogSnapshot = DEFAULT_CATALOG) -> ResearchModel:
    """Return a fresh original class; revalidate even manually assembled handles."""
    if type(spec) is not ValidatedModelSpec:
        raise InvalidModelSpec("create_model requires validate_spec output")
    validated = validate_spec(spec.spec, catalog)
    descriptor = catalog.resolve(validated.spec.model_id, validated.spec.implementation_version)
    return descriptor.build(validated.spec)
