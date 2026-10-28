"""Independent constructor oracles and isolated synthetic compatibility checks."""

from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
import inspect
import io
import pickle
import random
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from factorlab.models import (
    FactorScoreModel, LightGBMResearchModel, XGBoostResearchModel, RidgeResearchModel,
    LightGBMDependencyError, XGBoostDependencyError, ModelInputError, ModelNotFittedError,
)
from factorlab.models.factory import (
    ALIASES, DEFAULT_CATALOG, PROFILES, FeatureRef, InvalidModelSpec,
    ModelCatalogSnapshot, ModelRegistration, ModelRegistry, ModelSpec,
    UnknownModelVersion, ValidatedModelSpec, create_model, validate_spec,
)


CLASSES = {"factor_score": FactorScoreModel, "ridge": RidgeResearchModel,
           "lightgbm": LightGBMResearchModel, "xgboost": XGBoostResearchModel}
NAMES = ("MOM20", "REV1")
REFS = tuple(FeatureRef.legacy(name) for name in NAMES)
WEIGHTS = {"REV1": -1.0, "MOM20": 2.0}


def spec_for(model_id, parameters=None, **changes):
    canonical = ALIASES.get(model_id, model_id)
    spec = ModelSpec(model_id, "legacy-v1",
                     ({"weights": WEIGHTS} if canonical == "factor_score" else {})
                     if parameters is None else parameters,
                     REFS, None if canonical == "factor_score" else 42, 1, "research_v2")
    return replace(spec, **changes)


def direct(spec):
    """Independent oracle: public original constructors, no descriptor defaults."""
    model_id = ALIASES.get(spec.model_id, spec.model_id)
    parameters = dict(spec.parameters)
    if model_id == "factor_score":
        parameters["weights"] = {r.column: parameters["weights"][r.column] for r in spec.features}
    else:
        parameters["feature_names"] = tuple(r.column for r in spec.features)
        parameters["random_seed"] = spec.seed
        if model_id in ("lightgbm", "xgboost"):
            parameters["n_jobs"] = spec.threads
    return CLASSES[model_id](**parameters)


def build(spec):
    return create_model(validate_spec(spec))


def panel():
    x = pd.DataFrame({"REV1": np.sin(np.arange(48) / 5),
                      "MOM20": np.linspace(-2., 3., 48), "audit": "synthetic"})
    x.loc[[1, 6], "REV1"] = np.nan
    y = pd.Series(0.2*x.MOM20 - 0.1*x.REV1.fillna(0), index=x.index)
    return x, y


@pytest.mark.parametrize("model_id", CLASSES)
@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_default_construction_matches_original_class_and_entire_initial_state(model_id, profile):
    spec = spec_for(model_id, profile_id=profile)
    actual, expected = build(spec), direct(spec)
    assert type(actual) is CLASSES[model_id]
    assert vars(actual) == vars(expected)
    assert pickle.dumps(actual) == pickle.dumps(expected)
    assert not actual.is_fitted
    assert not any(hasattr(actual, key) for key in ("estimator_", "imputer_", "scaler_"))
    with pytest.raises(ModelNotFittedError):
        actual.predict(panel()[0])
    if model_id == "factor_score":
        assert actual.feature_names == NAMES
        assert list(actual.weights) == list(NAMES)
        assert actual.normalize_by_absolute_weight is False
    else:
        assert actual.requested_feature_names == NAMES
        assert actual.random_seed == 42
    if model_id in ("lightgbm", "xgboost"):
        assert actual.n_estimators == 200
        assert actual.n_jobs == 1


@pytest.mark.parametrize("model_id", CLASSES)
def test_descriptor_defaults_match_original_constructor_signature(model_id):
    registration = DEFAULT_CATALOG.resolve(model_id, "legacy-v1")
    signature = inspect.signature(CLASSES[model_id])
    for name, value in registration.parameter_defaults.items():
        assert signature.parameters[name].default == value
    expected_keys = set(signature.parameters) - {"feature_names", "random_seed", "n_jobs", "weights"}
    assert set(registration.parameter_defaults) == expected_keys


EXPLICIT = {
    "factor_score": {"weights": {"REV1": -3., "MOM20": 0.}, "normalize_by_absolute_weight": True},
    "ridge": {"alpha": 0., "fit_intercept": False, "max_iter": 7, "tolerance": 0.002},
    "lightgbm": {"n_estimators": 7, "learning_rate": 0.2, "num_leaves": 3, "max_depth": -1,
                 "feature_fraction": 1., "bagging_fraction": 1., "bagging_freq": 0,
                 "reg_alpha": 0.1, "reg_lambda": 0., "min_child_samples": 1},
    "xgboost": {"n_estimators": 7, "learning_rate": 0.2, "max_depth": 1,
                "min_child_weight": 0., "subsample": 1., "colsample_bytree": 1.,
                "reg_alpha": 0.1, "reg_lambda": 0., "gamma": 0.2},
}


@pytest.mark.parametrize("model_id", CLASSES)
def test_explicit_parameters_seed_and_threads_match_direct_construction(model_id):
    spec = spec_for(model_id, EXPLICIT[model_id], seed=None if model_id == "factor_score" else 0,
                    threads=2 if model_id in ("lightgbm", "xgboost") else 1)
    actual, expected = build(spec), direct(spec)
    assert vars(actual) == vars(expected)
    descriptor = DEFAULT_CATALOG.resolve(model_id, "legacy-v1")
    effective = descriptor.effective_parameters(actual)
    assert dict(effective) == dict(descriptor.effective_parameters(expected))
    with pytest.raises(TypeError):
        effective["injected"] = 1


@pytest.mark.parametrize("model_id", CLASSES)
@pytest.mark.parametrize("explicit", [False, True])
def test_synthetic_training_prediction_and_original_serialization_are_compatible(tmp_path, model_id, explicit):
    spec = spec_for(model_id, EXPLICIT[model_id] if explicit else None)
    actual, expected = build(spec), direct(spec)
    x, y = panel()
    actual.fit(x, y)
    expected.fit(x, y)
    pd.testing.assert_series_equal(actual.predict(x), expected.predict(x), check_exact=True)
    assert actual.feature_names == expected.feature_names == NAMES
    if model_id != "factor_score":
        np.testing.assert_array_equal(actual.imputer_.statistics_, expected.imputer_.statistics_)
        assert actual.estimator_.get_params() == expected.estimator_.get_params()
        if model_id == "ridge":
            np.testing.assert_array_equal(actual.scaler_.mean_, expected.scaler_.mean_)
            np.testing.assert_array_equal(actual.estimator_.coef_, expected.estimator_.coef_)
        else:
            assert not hasattr(actual, "scaler_")
            pd.testing.assert_series_equal(actual.feature_importance, expected.feature_importance, check_exact=True)
            native = actual.estimator_.get_params()
            assert native["random_state"] == spec.seed
            assert native["n_jobs"] == spec.threads
            if model_id == "lightgbm":
                assert all(native[key] == spec.seed for key in
                           ("bagging_seed", "feature_fraction_seed", "data_random_seed"))
                assert native["deterministic"] is native["force_col_wise"] is True
                assert native["objective"] == "regression"
            else:
                assert native["seed"] == spec.seed
                assert native["tree_method"] == "hist"
                assert native["objective"] == "reg:squarederror"
    else:
        assert dict(actual.weights) == {name: spec.parameters["weights"][name] for name in NAMES}
    # Exercise the unchanged trusted save/load API for both construction paths.
    for name, model in (("factory", actual), ("direct", expected)):
        destination = tmp_path / (name + ".pkl")
        model.save(destination)
        restored = CLASSES[model_id].load(destination)
        assert type(restored) is CLASSES[model_id]
        pd.testing.assert_series_equal(restored.predict(x), expected.predict(x), check_exact=True)
    # Equal original serialized bytes in the same environment, including native models.
    assert (tmp_path/"factory.pkl").read_bytes() == (tmp_path/"direct.pkl").read_bytes()


@pytest.mark.parametrize("alias,canonical", list(ALIASES.items()))
def test_only_explicit_aliases_resolve(alias, canonical):
    validated = validate_spec(spec_for(alias))
    assert validated.spec.model_id == canonical
    assert type(create_model(validated)) is CLASSES[canonical]


@pytest.mark.parametrize("identity", ["RIDGE", "RidgeResearchModel", "FactorScoreModel", "factorScore",
                                       "LIGHTGBM", "XGBOOST", "elastic_net", "os.system", "ridge "])
def test_unknown_identity_is_rejected(identity):
    with pytest.raises(UnknownModelVersion):
        build(spec_for(identity))


@pytest.mark.parametrize("version", ["latest", "1", "legacy-v2", "LEGACY-V1", "legacy-v1 "])
def test_unknown_exact_version_is_rejected(version):
    with pytest.raises(UnknownModelVersion):
        build(spec_for("ridge", implementation_version=version))


@pytest.mark.parametrize("model_id", CLASSES)
@pytest.mark.parametrize("key", ["typo", "feature_names", "random_seed", "n_jobs", "objective", "solver"])
def test_unknown_or_conflicting_parameter_names_are_rejected(model_id, key):
    parameters = {"weights": WEIGHTS} if model_id == "factor_score" else {}
    parameters[key] = 1
    with pytest.raises(InvalidModelSpec, match="unknown parameters"):
        build(spec_for(model_id, parameters))


@pytest.mark.parametrize("model_id", ["ridge", "lightgbm", "xgboost"])
@pytest.mark.parametrize("seed", [None, True, False, -1, 1.0, "42", np.int64(42), np.bool_(False)])
def test_illegal_supervised_seed_is_rejected(model_id, seed):
    with pytest.raises(InvalidModelSpec):
        build(spec_for(model_id, seed=seed))


@pytest.mark.parametrize("seed", [0, 42, True, -1, "none"])
def test_factor_score_has_no_learning_seed(seed):
    with pytest.raises(InvalidModelSpec, match="NOT_APPLICABLE"):
        build(spec_for("factor_score", seed=seed))


@pytest.mark.parametrize("model_id", CLASSES)
@pytest.mark.parametrize("threads", [True, False, 0, -1, 1.0, "1", None])
def test_invalid_thread_config_is_rejected(model_id, threads):
    with pytest.raises(InvalidModelSpec):
        build(spec_for(model_id, threads=threads))


@pytest.mark.parametrize("model_id", ["factor_score", "ridge"])
def test_models_without_thread_option_reject_multiple_threads(model_id):
    with pytest.raises(InvalidModelSpec, match="no n_jobs"):
        build(spec_for(model_id, threads=2))


@pytest.mark.parametrize("weights", [None, {}, {"MOM20": 1.}, {"MOM20": 1., "REV1": 1., "extra": 1.},
    {"MOM20": 0., "REV1": 0.}, {"MOM20": True, "REV1": 1.},
    {"MOM20": np.nan, "REV1": 1.}, {"MOM20": np.inf, "REV1": 1.},
    {"MOM20": "1", "REV1": 1.}, {"MOM20": 1j, "REV1": 1.}])
def test_illegal_weights_are_rejected(weights):
    with pytest.raises(InvalidModelSpec):
        build(spec_for("factor_score", {"weights": weights}))


BAD_PARAMETERS = [
    ("ridge", "alpha", -1), ("ridge", "max_iter", 0), ("ridge", "max_iter", 1.5),
    ("ridge", "tolerance", 0), ("ridge", "fit_intercept", 1),
    ("factor_score", "normalize_by_absolute_weight", 1),
    ("lightgbm", "n_estimators", 0), ("lightgbm", "num_leaves", 1),
    ("lightgbm", "max_depth", 0), ("lightgbm", "max_depth", -2),
    ("lightgbm", "feature_fraction", 0), ("lightgbm", "feature_fraction", 1.01),
    ("lightgbm", "bagging_fraction", 0), ("lightgbm", "bagging_freq", -1),
    ("lightgbm", "min_child_samples", 0), ("lightgbm", "learning_rate", 0),
    ("lightgbm", "reg_alpha", -1), ("lightgbm", "reg_lambda", -1),
    ("xgboost", "n_estimators", 0), ("xgboost", "max_depth", 0), ("xgboost", "max_depth", -1),
    ("xgboost", "min_child_weight", -1), ("xgboost", "subsample", 0),
    ("xgboost", "subsample", 1.01), ("xgboost", "colsample_bytree", 0),
    ("xgboost", "colsample_bytree", 1.01), ("xgboost", "reg_alpha", -1),
    ("xgboost", "reg_lambda", -1), ("xgboost", "gamma", -1), ("xgboost", "learning_rate", 0),
]


@pytest.mark.parametrize("model_id,key,value", BAD_PARAMETERS)
def test_original_range_rejections_remain_model_input_errors(model_id, key, value):
    parameters = {"weights": WEIGHTS} if model_id == "factor_score" else {}
    parameters[key] = value
    spec = spec_for(model_id, parameters)
    with pytest.raises(ModelInputError):
        direct(spec)
    with pytest.raises(InvalidModelSpec) as error:
        build(spec)
    assert isinstance(error.value, ModelInputError)
    if (model_id, key) not in (("ridge", "fit_intercept"), ("factor_score", "normalize_by_absolute_weight")):
        assert isinstance(error.value.__cause__, ModelInputError)


NUMERIC_PARAMETERS = [(model_id, key) for model_id, values in EXPLICIT.items()
                      for key in values if key not in ("weights", "normalize_by_absolute_weight", "fit_intercept")]


@pytest.mark.parametrize("model_id,key", NUMERIC_PARAMETERS)
@pytest.mark.parametrize("value", [True, np.nan, np.inf, -np.inf, "1"])
def test_every_numeric_parameter_rejects_bool_nonfinite_or_string(model_id, key, value):
    with pytest.raises(InvalidModelSpec):
        build(spec_for(model_id, {key: value}))


@pytest.mark.parametrize("model_id", CLASSES)
@pytest.mark.parametrize("seed", [0, 1, 42, 2**32-1])
def test_legal_seed_boundaries_and_not_applicable_status(model_id, seed):
    spec = spec_for(model_id, seed=None if model_id == "factor_score" else seed)
    validated = validate_spec(spec)
    assert validated.seed_status == ("NOT_APPLICABLE" if model_id == "factor_score" else "EXPLICIT")
    assert vars(create_model(validated)) == vars(direct(spec))


@pytest.mark.parametrize("model_id,key,value", [
    ("ridge", "alpha", 0), ("ridge", "tolerance", np.nextafter(0., 1.)),
    ("ridge", "max_iter", 1), ("lightgbm", "n_estimators", 1),
    ("lightgbm", "num_leaves", 2), ("lightgbm", "max_depth", -1),
    ("lightgbm", "bagging_freq", 0), ("lightgbm", "min_child_samples", 1),
    ("lightgbm", "feature_fraction", np.nextafter(0., 1.)),
    ("lightgbm", "bagging_fraction", 1.), ("xgboost", "n_estimators", 1),
    ("xgboost", "max_depth", 1), ("xgboost", "min_child_weight", 0),
    ("xgboost", "subsample", np.nextafter(0., 1.)), ("xgboost", "colsample_bytree", 1.),
    ("xgboost", "gamma", 0), ("xgboost", "reg_lambda", 0),
])
def test_valid_constructor_boundaries_match_original(model_id, key, value):
    spec = spec_for(model_id, {key: value})
    assert vars(build(spec)) == vars(direct(spec))


@pytest.mark.parametrize("parameters", [None, [], (), 1, "alpha=1", {1: 1}, {"alpha": []},
                                         {"alpha": {"nested": {"too_deep": 1}}}])
def test_illegal_parameter_containers_rejected(parameters):
    with pytest.raises(InvalidModelSpec):
        ModelSpec("ridge", "legacy-v1", parameters, REFS, 42, 1, "research_v2")


@pytest.mark.parametrize("field", ["model_id", "implementation_version", "profile_id"])
@pytest.mark.parametrize("value", [None, True, 1, "", " "])
def test_invalid_identity_field_types_rejected(field, value):
    with pytest.raises(InvalidModelSpec):
        replace(spec_for("ridge"), **{field: value})


def test_extreme_numeric_overflow_and_unknown_spec_field_rejected():
    with pytest.raises(InvalidModelSpec):
        build(spec_for("ridge", {"alpha": 10**1000}))
    with pytest.raises(TypeError, match="unexpected keyword"):
        ModelSpec("ridge", "legacy-v1", {}, REFS, 42, 1, "research_v2", train_data="private.csv")


@pytest.mark.parametrize("features", [(), (REFS[0], REFS[0]), ("MOM20", "REV1"), None, "MOM20"])
def test_invalid_feature_config_rejected(features):
    with pytest.raises(InvalidModelSpec):
        spec_for("ridge", features=features)


@pytest.mark.parametrize("field", ["column", "factor_id", "version", "definition_hash"])
@pytest.mark.parametrize("value", ["", " ", None, True, 1])
def test_invalid_feature_ref_rejected(field, value):
    with pytest.raises(InvalidModelSpec):
        replace(REFS[0], **{field: value})


@pytest.mark.parametrize("profile", ["legacy", "CROSS_MARKET_FIXED_V1", "latest", "research_v2 "])
def test_unknown_profile_rejected(profile):
    with pytest.raises(InvalidModelSpec, match="profile"):
        build(spec_for("ridge", profile_id=profile))


def test_profile_never_invents_weights_signs_or_cross_market_tree_defaults():
    for profile in PROFILES:
        fixed = build(spec_for("factor_score", profile_id=profile))
        assert fixed.weights == {name: WEIGHTS[name] for name in NAMES}
        for model_id in ("lightgbm", "xgboost"):
            assert build(spec_for(model_id, profile_id=profile)).n_estimators == 200
            assert build(spec_for(model_id, {"n_estimators": 40}, profile_id=profile)).n_estimators == 40
    first = build(spec_for("factor_score", profile_id="legacy_csi500_v1"))
    second = build(spec_for("factor_score", profile_id="cross_market_fixed_v1"))
    first.weights["MOM20"] = 100
    assert second.weights["MOM20"] == 2


def test_spec_defensively_copies_nested_mappings_features_and_validated_defaults():
    weights = dict(WEIGHTS)
    parameters = {"weights": weights}
    features = list(REFS)
    spec = ModelSpec("factor_score", "legacy-v1", parameters, features, None, 1, "research_v2")
    weights["MOM20"] = 99
    parameters["injected"] = 1
    features.reverse()
    validated = validate_spec(spec)
    assert validated.spec.features == REFS
    assert build(spec).weights["MOM20"] == 2
    for mapping in (spec.parameters, spec.parameters["weights"], validated.spec.parameters):
        with pytest.raises(TypeError):
            mapping["injected"] = 1
    with pytest.raises(FrozenInstanceError):
        spec.seed = 4
    a, b = create_model(validated), create_model(validated)
    assert a is not b and a.weights is not b.weights


class DuplicateMapping(Mapping):
    def __iter__(self):
        return iter(("alpha",))

    def __len__(self):
        return 1

    def __getitem__(self, key):
        return 1.

    def items(self):
        return [("alpha", 1.), ("alpha", 2.)]


def test_duplicate_mapping_keys_are_not_silently_overwritten():
    with pytest.raises(InvalidModelSpec, match="duplicate mapping key"):
        spec_for("ridge", DuplicateMapping())


def test_registry_conflict_freeze_snapshot_and_registration_validation():
    registration = DEFAULT_CATALOG.resolve("ridge", "legacy-v1")
    registry = ModelRegistry()
    registry.register(registration)
    assert registry.resolve("Ridge", "legacy-v1") is registration
    with pytest.raises(InvalidModelSpec, match="duplicate"):
        registry.register(registration)
    snapshot = registry.freeze()
    assert registry.freeze().registrations == snapshot.registrations
    with pytest.raises(InvalidModelSpec, match="frozen"):
        registry.register(DEFAULT_CATALOG.resolve("xgboost", "legacy-v1"))
    with pytest.raises(TypeError):
        snapshot.registrations[("ridge", "legacy-v1")] = registration
    with pytest.raises(TypeError):
        registration.parameter_defaults["alpha"] = 8
    assert type(create_model(validate_spec(spec_for("ridge"), snapshot), snapshot)) is RidgeResearchModel
    with pytest.raises(UnknownModelVersion):
        validate_spec(spec_for("xgboost"), snapshot)
    with pytest.raises(InvalidModelSpec):
        ModelRegistration("Ridge", "legacy-v1", {}, registration.build)
    with pytest.raises(InvalidModelSpec):
        ModelRegistration("ridge", "legacy-v1", {}, "os.system")
    with pytest.raises(InvalidModelSpec):
        ModelRegistry().register("ridge")
    with pytest.raises(InvalidModelSpec):
        ModelCatalogSnapshot({("wrong", "legacy-v1"): registration})


def test_validated_handle_cannot_bypass_validation_or_different_catalog():
    with pytest.raises(InvalidModelSpec):
        create_model(spec_for("ridge"))
    with pytest.raises(InvalidModelSpec):
        create_model(ValidatedModelSpec(spec_for("ridge", {"alpha": -1})))
    with pytest.raises(UnknownModelVersion):
        create_model(validate_spec(spec_for("ridge")), ModelRegistry().freeze())
    with pytest.raises(InvalidModelSpec):
        validate_spec({"model_id": "ridge"})


@pytest.mark.parametrize("model_id", CLASSES)
def test_requirements_describe_existing_math_without_claiming_data_admission(model_id):
    validated = validate_spec(spec_for(model_id))
    registration = DEFAULT_CATALOG.resolve(model_id, "legacy-v1")
    requirements = registration.requirements(validated.spec)
    assert requirements.ordered_columns == NAMES
    assert requirements.dtype == "float64"
    assert requirements.nan_policy == ("neutral_zero" if model_id == "factor_score" else "train_median")
    assert requirements.target_required == (model_id != "factor_score")
    assert requirements.market is requirements.target_id is requirements.price_basis is None
    assert not requirements.supports_partial_fit
    assert registration.capabilities.same_environment_repeatable


@pytest.mark.parametrize("model_id", CLASSES)
def test_no_fit_predict_data_io_network_persistence_or_rng_side_effects(monkeypatch, tmp_path, model_id):
    spec = spec_for(model_id)
    before = (random.getstate(), pickle.dumps(np.random.get_state()))
    before_files = sorted(tmp_path.iterdir())

    def forbidden(*args, **kwargs):
        raise AssertionError("construction attempted a side effect")

    with monkeypatch.context() as patch:
        for cls in CLASSES.values():
            for method in ("fit", "predict", "save", "load"):
                patch.setattr(cls, method, forbidden)
        patch.setattr("builtins.open", forbidden)
        patch.setattr(io, "open", forbidden)
        patch.setattr("os.open", forbidden)
        patch.setattr("os.mkdir", forbidden)
        patch.setattr("socket.socket", forbidden)
        for method in ("read_csv", "read_parquet", "read_pickle", "read_sql", "read_json"):
            patch.setattr(pd, method, forbidden)
        for method in ("open", "read_bytes", "write_bytes", "read_text", "write_text", "mkdir"):
            patch.setattr(Path, method, forbidden)
        assert not build(spec).is_fitted
    assert sorted(tmp_path.iterdir()) == before_files
    assert random.getstate() == before[0]
    assert pickle.dumps(np.random.get_state()) == before[1]


@pytest.mark.parametrize("model_id,module,error_class", [
    ("lightgbm", "factorlab.models.lightgbm", LightGBMDependencyError),
    ("xgboost", "factorlab.models.xgboost", XGBoostDependencyError),
])
def test_missing_optional_dependency_defers_original_error_to_fit(monkeypatch, model_id, module, error_class):
    calls = []

    def missing(name):
        calls.append(name)
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(module + ".import_module", missing)
    actual, expected = build(spec_for(model_id)), direct(spec_for(model_id))
    assert calls == []
    x, y = panel()
    for model in (actual, expected):
        with pytest.raises(error_class) as error:
            model.fit(x, y)
        assert type(error.value.__cause__) is ModuleNotFoundError
        assert not model.is_fitted
    assert calls == [model_id, model_id]
    assert type(build(spec_for("ridge"))) is RidgeResearchModel
    assert type(build(spec_for("factor_score"))) is FactorScoreModel


def test_fresh_process_import_and_construction_do_not_import_native_tree_packages(tmp_path):
    code = '''
import importlib.abc, sys
class DenyTrees(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'lightgbm', 'xgboost'}:
            raise AssertionError('native tree import before fit: ' + fullname)
sys.meta_path.insert(0, DenyTrees())
from factorlab.models.factory import ModelSpec, FeatureRef, validate_spec, create_model
for name in ('factor_score', 'ridge', 'lightgbm', 'xgboost'):
    spec = ModelSpec(name, 'legacy-v1', {'weights': {'a': 1}} if name == 'factor_score' else {},
                     (FeatureRef.legacy('a'),), None if name == 'factor_score' else 42, 1, 'research_v2')
    assert not create_model(validate_spec(spec)).is_fitted
assert 'lightgbm' not in sys.modules and 'xgboost' not in sys.modules
print('LAZY_CONSTRUCTION_PASS')
'''
    # sys.path is explicit because this is a fresh process outside the checkout.
    source = str(Path(__file__).resolve().parents[2] / "src")
    process = subprocess.run([sys.executable, "-B", "-c", "import sys; sys.path.insert(0, " + repr(source) + ");" + code],
                             cwd=tmp_path, text=True, capture_output=True)
    assert process.returncode == 0, process.stdout + process.stderr
    assert "LAZY_CONSTRUCTION_PASS" in process.stdout


@pytest.mark.parametrize("model_id", CLASSES)
@pytest.mark.parametrize("fault", ["missing", "duplicate", "inf", "bool", "complex", "target_index", "all_missing"])
def test_original_fit_and_predict_feature_rejections_are_preserved(model_id, fault):
    x, y = panel()
    models = [build(spec_for(model_id)), direct(spec_for(model_id))]
    if fault == "missing":
        x = x.drop(columns="REV1")
    elif fault == "duplicate":
        x.columns = ["REV1", "REV1", "audit"]
    elif fault == "inf":
        x.loc[0, "REV1"] = np.inf
    elif fault == "bool":
        x["REV1"] = True
    elif fault == "complex":
        x["REV1"] = 1j
    elif fault == "target_index":
        y.index = y.index + 1
    else:
        x["REV1"] = np.nan
    if fault == "all_missing" and model_id == "factor_score":
        for model in models:
            model.fit(x, y)
        pd.testing.assert_series_equal(models[0].predict(x), models[1].predict(x), check_exact=True)
    else:
        errors = []
        for model in models:
            with pytest.raises(ModelInputError) as error:
                model.fit(x, y)
            errors.append((type(error.value), str(error.value)))
            assert not model.is_fitted
        assert errors[0] == errors[1]


@pytest.mark.parametrize("model_id", CLASSES)
def test_prediction_column_reordering_extra_audit_and_train_only_preprocessing(model_id):
    x, y = panel()
    actual, expected = build(spec_for(model_id)), direct(spec_for(model_id))
    actual.fit(x, y)
    expected.fit(x, y)
    before = pickle.dumps(vars(actual))
    score = x.loc[:, ["audit", "MOM20", "REV1"]].copy()
    score["MOM20"] += 1000
    pd.testing.assert_series_equal(actual.predict(score), expected.predict(score), check_exact=True)
    assert pickle.dumps(vars(actual)) == before
    for invalid in (score.drop(columns="REV1"), score.assign(REV1=np.inf)):
        errors = []
        for model in (actual, expected):
            with pytest.raises(ModelInputError) as error:
                model.predict(invalid)
            errors.append((type(error.value), str(error.value)))
        assert errors[0] == errors[1]
