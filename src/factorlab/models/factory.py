"""Explicit, immutable specifications for construction of the four legacy models.

This module constructs original model classes. It has no data, training,
selection, persistence, plugin discovery, or financial admission responsibilities.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from collections import OrderedDict, UserDict, defaultdict
from dataclasses import dataclass, field, replace
from inspect import Parameter, signature
from math import isfinite
from numbers import Real
from types import MappingProxyType

import numpy as np

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
    legacy_constructor: type[ResearchModel] | None = None

    def __post_init__(self) -> None:
        _text(self.model_id, "model_id")
        _text(self.implementation_version, "implementation_version")
        if self.model_id in ALIASES or not callable(self.build):
            raise InvalidModelSpec("registration requires canonical identity and callable builder")
        object.__setattr__(self, "parameter_defaults", _snapshot(self.parameter_defaults))
        if self.legacy_constructor is not None and (
            not isinstance(self.legacy_constructor, type)
            or not issubclass(self.legacy_constructor, ResearchModel)
        ):
            raise InvalidModelSpec("legacy_constructor must be a ResearchModel class")

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
        registry.register(ModelRegistration(model_id, "legacy-v1", defaults, builder,
                                            legacy_constructor=_LEGACY_CLASSES[model_id]))
    return registry.freeze()


_LEGACY_CLASSES = MappingProxyType({
    "factor_score": FactorScoreModel, "ridge": RidgeResearchModel,
    "lightgbm": LightGBMResearchModel, "xgboost": XGBoostResearchModel,
})
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


def _legacy_copy(value, memo=None):
    """Copy inert containers without coercion or invoking user copy protocols.

    Opaque Python objects (including one-shot iterators and custom numeric
    objects) retain their original semantics and identity. They are trusted
    caller-owned values, not serializable configuration or executable plugins.
    """
    if memo is None:
        memo = {}
    if id(value) in memo:
        return memo[id(value)][1]
    kind = type(value)
    if kind in (dict, OrderedDict, UserDict, defaultdict):
        copied = defaultdict(value.default_factory) if kind is defaultdict else kind()
        memo[id(value)] = (value, copied)
        for key, item in value.items():
            copied[key] = _legacy_copy(item, memo)
        return copied
    if kind is MappingProxyType:
        contents = {}
        copied = MappingProxyType(contents)
        memo[id(value)] = (value, copied)
        for key, item in value.items():
            contents[key] = _legacy_copy(item, memo)
        return copied
    if kind is list:
        copied = []
        memo[id(value)] = (value, copied)
        copied.extend(_legacy_copy(item, memo) for item in value)
        return copied
    if kind is tuple:
        return tuple(_legacy_copy(item, memo) for item in value)
    if kind is np.ndarray:
        copied = value.copy()
        memo[id(value)] = (value, copied)
        if value.dtype.names:
            for name in value.dtype.names:
                if value.dtype.fields[name][0].hasobject:
                    copied[name] = _legacy_copy(value[name], memo)
        elif value.dtype.hasobject:
            for index in np.ndindex(value.shape):
                copied[index] = _legacy_copy(value[index], memo)
        return copied
    return value


@dataclass(frozen=True, init=False, eq=False)
class LegacyModelRequest:
    """Original keyword arguments; no V2 coercion, expansion or value checks.

    feature_names, random_seed and n_jobs remain in constructor_arguments.
    Omission is distinct from explicit None. Injection is a call-time keyword
    of create_legacy_model, never serialized into this request.
    """

    model_id: str
    implementation_version: str
    profile_id: str
    _arguments: Mapping[str, object] = field(repr=False, compare=False)

    def __init__(self, model_id: str, implementation_version: str, profile_id: str,
                 constructor_arguments: Mapping[str, object]) -> None:
        for name, value in (("model_id", model_id), ("implementation_version", implementation_version),
                            ("profile_id", profile_id)):
            _text(value, name)
            object.__setattr__(self, name, value)
        if not isinstance(constructor_arguments, Mapping):
            raise InvalidModelSpec("constructor_arguments must be a mapping")
        object.__setattr__(self, "_arguments", _legacy_copy(dict(constructor_arguments)))

    @property
    def constructor_arguments(self) -> Mapping[str, object]:
        """A defensive public copy; original list/tuple/array/weights types survive."""
        return MappingProxyType(_legacy_copy(self._arguments))


def _legacy_registration(model_id, implementation_version, profile_id, catalog):
    if type(catalog) is not ModelCatalogSnapshot:
        raise InvalidModelSpec("expected frozen ModelCatalogSnapshot")
    descriptor = catalog.resolve(model_id, implementation_version)
    if profile_id not in PROFILES:
        raise InvalidModelSpec("unknown profile: " + str(profile_id))
    if (descriptor.implementation_version != "legacy-v1"
            or descriptor.model_id not in _LEGACY_CLASSES
            or descriptor.legacy_constructor is not _LEGACY_CLASSES[descriptor.model_id]):
        raise InvalidModelSpec("legacy construction requires a registered original model class")
    return descriptor


def create_legacy_model(request: LegacyModelRequest, *,
                        constructor_type: type[ResearchModel] | None = None,
                        catalog: ModelCatalogSnapshot = DEFAULT_CATALOG) -> ResearchModel:
    """One original construction, original errors; no strict validation or fit.

    A trusted Python caller may supply its current module-level original class
    or subclass (including a Spy). Arbitrary callables and import strings fail.
    """
    if type(request) is not LegacyModelRequest:
        raise InvalidModelSpec("expected LegacyModelRequest")
    descriptor = _legacy_registration(request.model_id, request.implementation_version,
                                      request.profile_id, catalog)
    original = descriptor.legacy_constructor
    constructor = original if constructor_type is None else constructor_type
    if not isinstance(constructor, type) or not issubclass(constructor, original):
        raise InvalidModelSpec("constructor_type must be the registered original class or its subclass")
    model = constructor(**request.constructor_arguments)
    if not isinstance(model, original):
        raise InvalidModelSpec("legacy constructor did not return an original model instance")
    return model


@dataclass(frozen=True)
class LegacyEffectiveSpec:
    """Observed effective state and optional caller-supplied request provenance.

    explicit_arguments/defaults_used are None when provenance was not supplied;
    model state cannot reveal which values were explicitly passed. strict_spec
    is absent when the old successful input cannot be expressed faithfully.
    """

    original_model_id: str
    model_id: str
    implementation_version: str
    profile_id: str
    parameters: Mapping[str, object]
    feature_names: tuple[str, ...] | None
    seed: int | None
    threads: int
    explicit_arguments: tuple[str, ...] | None
    defaults_used: tuple[str, ...] | None
    strict_spec: ModelSpec | None
    status: str
    reason: str | None


def effective_spec_from_legacy(model: ResearchModel, *, model_id: str,
                               implementation_version: str, profile_id: str,
                               catalog: ModelCatalogSnapshot = DEFAULT_CATALOG,
                               request: LegacyModelRequest | None = None) -> LegacyEffectiveSpec:
    """Describe an existing model without constructing, fitting or changing it.

    request, when supplied, must be the original creation request. Its explicit
    argument keys are caller provenance, not an inferred or authenticated fact.
    This inspection is optional and never a gate on legacy construction.
    """
    descriptor = _legacy_registration(model_id, implementation_version, profile_id, catalog)
    if not isinstance(model, descriptor.legacy_constructor):
        raise InvalidModelSpec("model does not match the registered legacy constructor")
    explicit = defaults = None
    if request is not None:
        if type(request) is not LegacyModelRequest:
            raise InvalidModelSpec("expected LegacyModelRequest provenance")
        requested = _legacy_registration(request.model_id, request.implementation_version,
                                          request.profile_id, catalog)
        if requested is not descriptor or request.profile_id != profile_id:
            raise InvalidModelSpec("request provenance does not match model identity/profile")
        explicit = tuple(request.constructor_arguments)
        defaults = tuple(name for name, p in signature(descriptor.legacy_constructor).parameters.items()
                         if p.default is not Parameter.empty and name not in explicit)
    parameters = {name: getattr(model, name) for name in descriptor.parameter_defaults}
    fixed = descriptor.model_id == "factor_score"
    if fixed:
        parameters["weights"] = model.weights
        names, seed, threads = tuple(model.feature_names), None, 1
    else:
        names = model.requested_feature_names
        seed = model.random_seed
        threads = model.n_jobs if descriptor.model_id in ("lightgbm", "xgboost") else 1
    parameters = MappingProxyType({name: MappingProxyType(dict(value))
                                   if isinstance(value, Mapping) else value
                                   for name, value in parameters.items()})
    strict = reason = None
    try:
        if not fixed and type(seed) is not int:
            raise InvalidModelSpec("legacy random_seed is not the exact Python int required by strict V2")
        if names is None:
            raise InvalidModelSpec("legacy feature_names=None is resolved by fit, not construction")
        for name, value in parameters.items():
            if name == "weights":
                numeric = all(_numeric(weight) for weight in value.values())
            elif name in ("normalize_by_absolute_weight", "fit_intercept"):
                numeric = type(value) is bool
            else:
                numeric = _numeric(value)
            if not numeric:
                raise InvalidModelSpec(name + " cannot be represented by strict V2 numeric checks")
        strict = ModelSpec(descriptor.model_id, implementation_version, parameters,
                           tuple(FeatureRef.legacy(name) for name in names), seed, threads, profile_id)
    except InvalidModelSpec as exc:
        reason = str(exc)
    return LegacyEffectiveSpec(model_id, descriptor.model_id, implementation_version, profile_id,
                               parameters, names, seed, threads, explicit, defaults, strict,
                               "STRICT_REPRESENTABLE" if strict is not None else "LEGACY_ONLY_UNREPRESENTABLE",
                               reason)
