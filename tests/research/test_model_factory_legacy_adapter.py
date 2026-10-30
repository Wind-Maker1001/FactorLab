"""Original classes are independent oracles; no old tests or expectations change."""
from collections import OrderedDict, UserDict, defaultdict
from dataclasses import FrozenInstanceError, replace
import importlib
import inspect
import io
from pathlib import Path
import pickle
import random
import socket
import subprocess
import sys
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from factorlab.models import (
    FactorScoreModel, RidgeResearchModel, LightGBMResearchModel, XGBoostResearchModel,
)
from factorlab.models.factory import (
    DEFAULT_CATALOG, FeatureRef, InvalidModelSpec, LegacyModelRequest, ModelCapabilities,
    ModelRegistration, ModelRegistry, ModelSpec, UnknownModelVersion,
    create_legacy_model, create_model, effective_spec_from_legacy, validate_spec,
)

CLASSES = dict(factor_score=FactorScoreModel, ridge=RidgeResearchModel,
               lightgbm=LightGBMResearchModel, xgboost=XGBoostResearchModel)
NAMES = ('REV1', 'MOM20')
PROFILES = ('legacy_csi500_v1', 'cross_market_fixed_v1', 'research_v2')


def args_for(name):
    return {'weights': OrderedDict((('REV1', '-1'), ('MOM20', '2')))} if name == 'factor_score' else {'feature_names': list(NAMES)}


def request_for(name, args=None, profile='research_v2'):
    return LegacyModelRequest(name, 'legacy-v1', profile, args_for(name) if args is None else args)


def describe(model, name, request=None):
    return effective_spec_from_legacy(model, model_id=name, implementation_version='legacy-v1',
                                      profile_id='research_v2', request=request)


@pytest.mark.parametrize('name', CLASSES)
@pytest.mark.parametrize('profile', PROFILES)
def test_default_state_and_raw_pickle_match_direct_constructor(name, profile):
    args = args_for(name)
    expected = CLASSES[name](**args)
    actual = create_legacy_model(request_for(name, args, profile))
    assert type(actual) is type(expected) is CLASSES[name]
    assert vars(actual) == vars(expected)
    assert pickle.dumps(actual) == pickle.dumps(expected)
    assert not actual.is_fitted


@pytest.mark.parametrize('name', CLASSES)
def test_omitted_arguments_keep_original_defaults_and_provenance(name):
    args = {'weights': {'a': 1}} if name == 'factor_score' else {}
    request = request_for(name, args)
    actual = create_legacy_model(request)
    expected = CLASSES[name](**args)
    assert vars(actual) == vars(expected)
    result = describe(actual, name, request)
    assert result.explicit_arguments == tuple(args)
    defaults = tuple(n for n, p in inspect.signature(CLASSES[name]).parameters.items()
                     if p.default is not inspect.Parameter.empty and n not in args)
    assert result.defaults_used == defaults
    unknown = describe(actual, name)
    assert unknown.explicit_arguments is unknown.defaults_used is None
    if name != 'factor_score':
        assert result.status == 'LEGACY_ONLY_UNREPRESENTABLE'
        assert result.feature_names is result.strict_spec is None
        assert actual.random_seed == 42


LOOSE = [
    ('factor_score', 'weights', OrderedDict((('REV1', '-2.5'), ('MOM20', '1')))),
    ('factor_score', 'weights', {1: 1.0, None: -1.0}),
    ('factor_score', 'normalize_by_absolute_weight', np.bool_(True)),
    ('ridge', 'alpha', '0.25'), ('ridge', 'tolerance', '0.00001'),
    ('ridge', 'fit_intercept', np.bool_(False)),
    ('lightgbm', 'n_estimators', np.int64(40)), ('xgboost', 'random_seed', np.int64(7)),
    ('lightgbm', 'n_jobs', np.int64(2)), ('xgboost', 'n_jobs', np.int64(2)),
]
LOOSE += [(n, 'feature_names', names) for n in ('ridge', 'lightgbm', 'xgboost')
          for names in [list(reversed(NAMES)), (' ', 'x'), np.array(['b', 'a']), 'ab', [1, 2]]]


@pytest.mark.parametrize('name,key,value', LOOSE)
def test_real_loose_acceptance_domain_is_unchanged(name, key, value):
    args = args_for(name)
    args[key] = value
    expected = CLASSES[name](**args)
    request = request_for(name, args)
    actual = create_legacy_model(request)
    assert vars(actual) == vars(expected)
    assert pickle.dumps(actual) == pickle.dumps(expected)
    result = describe(actual, name, request)
    assert result.original_model_id == name
    if result.strict_spec is not None:
        rebuilt = create_model(validate_spec(result.strict_spec))
        assert vars(rebuilt) == vars(actual)


ERRORS = [(n, 'unknown_parameter', 1) for n in CLASSES]
ERRORS += [('factor_score', 'weights', w) for w in [None, [], {}, {'a': True}, {'a': np.nan}, {'a': np.inf}, {'a': 0}, {'a': object()}]]
ERRORS += [('factor_score', 'normalize_by_absolute_weight', 'true')]
ERRORS += [('ridge', k, v) for k, v in [('alpha', True), ('alpha', -1), ('alpha', np.inf),
    ('random_seed', np.int64(1)), ('random_seed', True), ('max_iter', np.int64(10)),
    ('tolerance', 0), ('fit_intercept', 1)]]
ERRORS += [(n, k, v) for n in ('lightgbm', 'xgboost') for k, v in [
    ('n_estimators', '40'), ('n_estimators', True), ('n_estimators', 0),
    ('learning_rate', '0.03'), ('learning_rate', np.nan), ('n_jobs', 0), ('random_seed', -1)]]
ERRORS += [(n, 'feature_names', v) for n in ('ridge', 'lightgbm', 'xgboost') for v in [[], ['a', 'a'], [''], 1]]


@pytest.mark.parametrize('name,key,value', ERRORS)
def test_original_exception_class_message_cause_and_constructor_phase(name, key, value):
    args = args_for(name)
    args[key] = value
    request = request_for(name, args)  # Malformed values must not fail early here.
    with pytest.raises(Exception) as original:
        CLASSES[name](**args)
    with pytest.raises(type(original.value)) as adapted:
        create_legacy_model(request)
    assert type(adapted.value) is type(original.value)
    assert str(adapted.value) == str(original.value)
    assert type(adapted.value.__cause__) is type(original.value.__cause__)


@pytest.mark.parametrize('name', CLASSES)
def test_original_missing_and_non_string_keyword_errors(name):
    args = {} if name == 'factor_score' else {1: 'bad'}
    with pytest.raises(TypeError) as original:
        CLASSES[name](**args)
    with pytest.raises(TypeError) as actual:
        create_legacy_model(request_for(name, args))
    assert str(actual.value) == str(original.value)


@pytest.mark.parametrize('name', CLASSES)
def test_call_time_spy_binding_full_kwargs_once_and_original_exception(monkeypatch, name):
    original = CLASSES[name]
    calls = Mock()

    class Spy(original):
        def __init__(self, **kwargs):
            calls(kwargs)
            super().__init__(**kwargs)

    module = importlib.import_module('factorlab.validation.walk_forward')
    attribute = original.__name__
    monkeypatch.setattr(module, attribute, Spy)
    args = args_for(name)
    actual = create_legacy_model(request_for(name, args), constructor_type=getattr(module, attribute))
    assert type(actual) is Spy and isinstance(actual, original)
    calls.assert_called_once()
    observed = calls.call_args.args[0]
    assert list(observed) == list(args)
    assert all(type(observed[k]) is type(args[k]) for k in args)
    assert observed == args and not actual.is_fitted
    sentinel = RuntimeError('original injected constructor failure')

    class FailingSpy(original):
        def __init__(self, **kwargs):
            calls(kwargs)
            raise sentinel

    monkeypatch.setattr(module, attribute, FailingSpy)
    with pytest.raises(RuntimeError) as error:
        create_legacy_model(request_for(name, args), constructor_type=getattr(module, attribute))
    assert error.value is sentinel
    assert calls.call_count == 2


@pytest.mark.parametrize('constructor', ['os.system', lambda **kw: None, Mock(), FactorScoreModel])
def test_injection_rejects_import_strings_callables_and_other_model_classes(constructor):
    with pytest.raises(InvalidModelSpec, match='constructor_type'):
        create_legacy_model(request_for('ridge'), constructor_type=constructor)


@pytest.mark.parametrize('name', CLASSES)
def test_input_snapshots_public_access_and_repeated_requests_are_independent(name):
    args = args_for(name)
    request = request_for(name, args)
    if name == 'factor_score':
        args['weights']['REV1'] = '999'
        request.constructor_arguments['weights']['MOM20'] = 100
    else:
        args['feature_names'].reverse()
        request.constructor_arguments['feature_names'].reverse()
    args['random_seed'] = -1
    with pytest.raises(FrozenInstanceError):
        request.profile_id = 'other'
    with pytest.raises(TypeError):
        request.constructor_arguments['injected'] = 1
    first, second = create_legacy_model(request), create_legacy_model(request)
    assert first is not second
    expected = CLASSES[name](**args_for(name))
    assert vars(first) == vars(second) == vars(expected)
    if name == 'factor_score':
        first.weights['REV1'] = 100
        assert second.weights['REV1'] == -1


def test_numpy_sequence_snapshot_retains_type_and_order():
    names = np.array(['b', 'a'])
    request = request_for('ridge', {'feature_names': names})
    names[:] = 'x'
    assert type(request.constructor_arguments['feature_names']) is np.ndarray
    request.constructor_arguments['feature_names'][:] = 'y'
    assert create_legacy_model(request).requested_feature_names == ('b', 'a')


def test_opaque_numeric_objects_keep_original_type_conversion_without_copy_hooks():
    class Numeric:
        def __float__(self): return 0.5
        def __deepcopy__(self, memo): raise AssertionError('not a constructor protocol')
    value = Numeric()
    request = request_for('ridge', {'alpha': value})
    assert request.constructor_arguments['alpha'] is value
    assert create_legacy_model(request).alpha == RidgeResearchModel(alpha=value).alpha


@pytest.mark.parametrize('name', CLASSES)
def test_effective_spec_does_not_construct_and_keeps_explicit_fields(name, monkeypatch):
    request = request_for(name)
    model = create_legacy_model(request)
    before = pickle.dumps(model)
    def forbidden(*a, **k): raise AssertionError('inspector constructed or trained')
    with monkeypatch.context() as patch:
        patch.setattr(CLASSES[name], '__init__', forbidden)
        patch.setattr(CLASSES[name], 'fit', forbidden)
        patch.setattr(CLASSES[name], 'predict', forbidden)
        result = describe(model, name)  # No request: no signature-dependent provenance.
    result = describe(model, name, request)
    assert result.explicit_arguments == tuple(request.constructor_arguments)
    assert result.feature_names == NAMES
    assert result.seed == (None if name == 'factor_score' else 42)
    assert result.threads == 1 and result.status == 'STRICT_REPRESENTABLE'
    assert pickle.dumps(model) == before


@pytest.mark.parametrize('name', CLASSES)
def test_unrepresentable_success_keeps_model_and_returns_explicit_compatibility_status(name):
    args = {'weights': {' ': '1'}} if name == 'factor_score' else {'feature_names': [' ']}
    request = request_for(name, args)
    actual = create_legacy_model(request)
    result = describe(actual, name, request)
    assert result.status == 'LEGACY_ONLY_UNREPRESENTABLE'
    assert result.strict_spec is None and result.reason
    assert result.feature_names == (' ',)
    assert vars(actual) == vars(CLASSES[name](**args))


@pytest.mark.parametrize('name', ('lightgbm', 'xgboost'))
@pytest.mark.parametrize('trees', (8, 40, 200))
def test_tree_count_seed_threads_are_explicit_and_profiles_never_rewrite(name, trees):
    args = dict(feature_names=NAMES, n_estimators=trees, random_seed=7, n_jobs=2)
    for profile in PROFILES:
        request = request_for(name, args, profile)
        model = create_legacy_model(request)
        result = effective_spec_from_legacy(model, model_id=name, implementation_version='legacy-v1', profile_id=profile, request=request)
        assert result.parameters['n_estimators'] == trees
        assert result.seed == 7 and result.threads == 2
        assert model.n_estimators == trees and model.n_jobs == 2


def test_strict_rules_and_descriptor_freeze_are_unchanged():
    refs = (FeatureRef.legacy('a'),)
    with pytest.raises(InvalidModelSpec):
        validate_spec(ModelSpec('factor_score', 'legacy-v1', {'weights': {'a': '1'}}, refs, None, 1, 'research_v2'))
    with pytest.raises(InvalidModelSpec):
        validate_spec(ModelSpec('ridge', 'legacy-v1', {'fit_intercept': np.bool_(True)}, refs, 42, 1, 'research_v2'))
    descriptor = DEFAULT_CATALOG.resolve('ridge', 'legacy-v1')
    assert descriptor.legacy_constructor is RidgeResearchModel
    old = ModelRegistration('ridge', 'legacy-v1', descriptor.parameter_defaults, descriptor.build, ModelCapabilities())
    assert old.legacy_constructor is None
    registry = ModelRegistry()
    registry.register(old)
    catalog = registry.freeze()
    assert type(create_model(validate_spec(ModelSpec('ridge', 'legacy-v1', {}, refs, 42, 1, 'research_v2'), catalog), catalog)) is RidgeResearchModel
    with pytest.raises(InvalidModelSpec): create_legacy_model(request_for('ridge'), catalog=catalog)
    with pytest.raises(InvalidModelSpec): registry.register(descriptor)
    with pytest.raises(TypeError): catalog.registrations[('x', 'v')] = descriptor
    with pytest.raises(InvalidModelSpec): replace(descriptor, legacy_constructor='os.system')


@pytest.mark.parametrize('name', CLASSES)
def test_construction_and_inspection_have_no_io_fit_predict_or_rng_effects(name, monkeypatch):
    request = request_for(name)
    before = (random.getstate(), pickle.dumps(np.random.get_state()))
    def forbidden(*a, **k): raise AssertionError('legacy adapter performed a side effect')
    with monkeypatch.context() as patch:
        for cls in CLASSES.values():
            for method in ('fit', 'predict', 'save', 'load'): patch.setattr(cls, method, forbidden)
        for name_ in ('open',): patch.setattr('builtins.' + name_, forbidden)
        patch.setattr(io, 'open', forbidden)
        patch.setattr(socket, 'socket', forbidden)
        for method in ('read_csv', 'read_parquet', 'read_pickle', 'read_json', 'read_sql'): patch.setattr(pd, method, forbidden)
        for method in ('open', 'read_bytes', 'write_bytes', 'read_text', 'write_text', 'mkdir'): patch.setattr(Path, method, forbidden)
        model = create_legacy_model(request)
        assert not model.is_fitted
        describe(model, name, request)
    assert random.getstate() == before[0]
    assert pickle.dumps(np.random.get_state()) == before[1]


@pytest.mark.parametrize('name', CLASSES)
@pytest.mark.parametrize('normalize', (False, True))
def test_exact_training_preprocessing_predictions_pickle_and_save_load(tmp_path, monkeypatch, name, normalize):
    rng = np.random.default_rng(123)
    x = pd.DataFrame(rng.normal(size=(36, 2)), columns=NAMES)
    x.iloc[::7, 0] = np.nan
    y = pd.Series(np.arange(36, dtype=float) / 37, index=x.index)
    future = x.iloc[:9].copy() * 13
    args = args_for(name)
    if name == 'factor_score': args['normalize_by_absolute_weight'] = normalize
    if name in ('lightgbm', 'xgboost'): args.update(n_estimators=8, random_seed=7, n_jobs=1)
    direct = CLASSES[name](**args)
    adapted = create_legacy_model(request_for(name, args))
    assert pickle.dumps(direct) == pickle.dumps(adapted)
    counts = Mock()
    cls = CLASSES[name]
    original_fit, original_predict = cls.fit, cls.predict
    def fit_spy(self, *a, **k):
        counts(id(self), 'fit')
        return original_fit(self, *a, **k)
    def predict_spy(self, *a, **k):
        counts(id(self), 'predict')
        return original_predict(self, *a, **k)
    with monkeypatch.context() as patch:
        patch.setattr(cls, 'fit', fit_spy)
        patch.setattr(cls, 'predict', predict_spy)
        for model, tag in [(direct, 'direct'), (adapted, 'adapted')]:
            model.fit(x, y)
            prediction = model.predict(future)
            if tag == 'direct': expected = prediction
            else: pd.testing.assert_series_equal(prediction, expected, check_exact=True)
    assert counts.call_count == 4
    assert counts.call_args_list[0].args == (id(direct), 'fit')
    assert counts.call_args_list[1].args == (id(direct), 'predict')
    assert counts.call_args_list[2].args == (id(adapted), 'fit')
    assert counts.call_args_list[3].args == (id(adapted), 'predict')
    if name != 'factor_score':
        np.testing.assert_array_equal(direct.imputer_.statistics_, adapted.imputer_.statistics_)
        np.testing.assert_array_equal(adapted.imputer_.statistics_, np.nanmedian(x.to_numpy(), axis=0))
    if name == 'ridge':
        np.testing.assert_array_equal(direct.scaler_.mean_, adapted.scaler_.mean_)
        pd.testing.assert_series_equal(direct.coefficients, adapted.coefficients, check_exact=True)
    if name in ('lightgbm', 'xgboost'):
        np.testing.assert_array_equal(direct.estimator_.feature_importances_, adapted.estimator_.feature_importances_)
        params = adapted.estimator_.get_params()
        assert params['random_state'] == 7 and params['n_jobs'] == 1
        if name == 'xgboost': assert params['tree_method'] == 'hist'
    assert pickle.dumps(direct) == pickle.dumps(adapted)
    a, b = tmp_path/'direct.pkl', tmp_path/'adapted.pkl'
    direct.save(a); adapted.save(b)
    assert a.read_bytes() == b.read_bytes()
    loaded = CLASSES[name].load(b)
    assert type(loaded) is CLASSES[name]
    pd.testing.assert_series_equal(loaded.predict(future), expected, check_exact=True)


def test_actual_cross_market_four_constructors_remain_independent_and_match_explicit_requests():
    from factorlab.cross_market.research import make_model
    names = ['VOLUME_RATIO', 'REV1', 'AMIHU20', 'MOM20']
    for name, cls in CLASSES.items():
        public_name = {'factor_score': 'FactorScore', 'ridge': 'Ridge', 'lightgbm': 'LightGBM', 'xgboost': 'XGBoost'}[name]
        args = {'weights': {n: -1. if n.startswith(('REV', 'VOL', 'AMIHU')) else 1. for n in names}} if name == 'factor_score' else {'feature_names': names}
        if name in ('lightgbm', 'xgboost'): args.update(n_estimators=40, n_jobs=1)
        actual = create_legacy_model(request_for(name, args, 'cross_market_fixed_v1'))
        expected = make_model(public_name, names)
        assert type(actual) is type(expected) is cls
        assert vars(actual) == vars(expected)
        assert pickle.dumps(actual) == pickle.dumps(expected)


def test_fresh_process_adapter_import_and_construction_keep_native_packages_lazy(tmp_path):
    source = str(Path(__file__).resolve().parents[2]/'src')
    code = '''
import importlib.abc, sys
class Deny(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, *args):
  if fullname.split('.')[0] in {'lightgbm','xgboost'}: raise AssertionError(fullname)
sys.meta_path.insert(0,Deny())
from factorlab.models.factory import LegacyModelRequest,create_legacy_model
for name in ('factor_score','ridge','lightgbm','xgboost'):
 args={'weights':{'a':'1'}} if name=='factor_score' else {}
 assert not create_legacy_model(LegacyModelRequest(name,'legacy-v1','research_v2',args)).is_fitted
assert 'lightgbm' not in sys.modules and 'xgboost' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-B', '-c', 'import sys;sys.path.insert(0,'+repr(source)+');'+code], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('kind', (dict, OrderedDict, UserDict, defaultdict))
def test_standard_mutable_weight_mapping_snapshots_preserve_original_type(kind):
    weights = defaultdict(float, {'a':'1'}) if kind is defaultdict else kind({'a':'1'})
    request = request_for('factor_score', {'weights':weights})
    weights['a'] = '99'
    assert type(request.constructor_arguments['weights']) is kind
    assert create_legacy_model(request).weights == {'a':1.}


@pytest.mark.parametrize('name,alias', [('factor_score','FactorScore'),('ridge','Ridge'),('lightgbm','LightGBM'),('xgboost','XGBoost')])
def test_alias_identity_and_explicit_default_are_not_inferred(name, alias):
    args = args_for(name)
    if name != 'factor_score': args['random_seed'] = 42
    request = request_for(alias, args)
    model = create_legacy_model(request)
    result = effective_spec_from_legacy(model,model_id=alias,implementation_version='legacy-v1',profile_id='research_v2',request=request)
    assert result.original_model_id == alias and result.model_id == name
    if name != 'factor_score':
        assert 'random_seed' in result.explicit_arguments and 'random_seed' not in result.defaults_used


@pytest.mark.parametrize('name', ('lightgbm','xgboost'))
def test_missing_native_package_keeps_original_fit_error_and_phase(name, monkeypatch):
    module = importlib.import_module('factorlab.models.'+name)
    calls = Mock(side_effect=ModuleNotFoundError(name))
    monkeypatch.setattr(module,'import_module',calls)
    model = create_legacy_model(request_for(name))
    direct = CLASSES[name](**args_for(name))
    calls.assert_not_called()
    x = pd.DataFrame([[1.,2.],[3.,4.]],columns=NAMES)
    y = pd.Series([1.,2.])
    with pytest.raises(ImportError) as expected: direct.fit(x,y)
    with pytest.raises(type(expected.value)) as actual: model.fit(x,y)
    assert str(actual.value)==str(expected.value)
    assert calls.call_count==2 and not model.is_fitted


@pytest.mark.parametrize('field,value', [('model_id','unknown'),('implementation_version','latest'),('profile_id','unknown')])
def test_unknown_metadata_fails_without_any_constructor(field,value,monkeypatch):
    kwargs=dict(model_id='ridge',implementation_version='legacy-v1',profile_id='research_v2',constructor_arguments={})
    kwargs[field]=value
    def forbidden(*a,**k): raise AssertionError('unexpected constructor')
    monkeypatch.setattr(RidgeResearchModel,'__init__',forbidden)
    with pytest.raises(InvalidModelSpec): create_legacy_model(LegacyModelRequest(**kwargs))


def test_legacy_seed_subclass_is_retained_without_claiming_strict_representability():
    class LegacySeed(int):
        pass
    seed = LegacySeed(7)
    request = request_for('ridge', {'feature_names':NAMES,'random_seed':seed})
    model = create_legacy_model(request)
    assert model.random_seed is seed
    assert vars(model) == vars(RidgeResearchModel(feature_names=NAMES,random_seed=seed))
    result = describe(model,'ridge',request)
    assert result.seed is seed and result.status == 'LEGACY_ONLY_UNREPRESENTABLE'
    assert result.strict_spec is None and 'exact Python int' in result.reason
    with pytest.raises(InvalidModelSpec):
        validate_spec(ModelSpec('ridge','legacy-v1',{},tuple(FeatureRef.legacy(n) for n in NAMES),seed,1,'research_v2'))


@pytest.mark.parametrize('name', ('ridge','lightgbm','xgboost'))
@pytest.mark.parametrize('structured', (False,True))
def test_object_array_mutable_elements_are_snapshotted_without_type_or_value_coercion(name,structured):
    if structured:
        names = np.empty(2,dtype=[('label',object)])
        names['label'][0],names['label'][1] = ['a'],['b']
    else:
        names = np.empty(2,dtype=object)
        names[0],names[1] = ['a'],['b']
    expected = CLASSES[name](feature_names=names)
    request = request_for(name,{'feature_names':names})
    source_element = names['label'][0] if structured else names[0]
    source_element.append('source mutation')
    public = request.constructor_arguments['feature_names']
    public_element = public['label'][1] if structured else public[1]
    public_element.append('public mutation')
    assert type(public) is np.ndarray and public.dtype == names.dtype
    first,second = create_legacy_model(request),create_legacy_model(request)
    assert vars(first) == vars(second) == vars(expected)
    assert pickle.dumps(first) == pickle.dumps(expected)


def test_object_array_self_reference_is_copied_without_recursion_or_alias_leak():
    names = np.empty(1,dtype=object)
    names[0] = names
    request = request_for('ridge',{'feature_names':names})
    public = request.constructor_arguments['feature_names']
    assert public is not names and public[0] is public
    public[0] = 'changed'
    next_copy = request.constructor_arguments['feature_names']
    assert next_copy[0] is next_copy


@pytest.mark.parametrize('name,key', [('ridge','max_iter'),('lightgbm','n_estimators'),
    ('lightgbm','max_depth'),('lightgbm','bagging_freq'),('lightgbm','min_child_samples'),
    ('xgboost','n_estimators'),('xgboost','max_depth')])
def test_accepted_legacy_huge_integers_are_not_false_strict_specs(name,key):
    huge = 10**400
    args = dict(feature_names=NAMES)
    args[key] = huge
    request = request_for(name,args)
    direct = CLASSES[name](**args)
    model = create_legacy_model(request)
    assert vars(model) == vars(direct)
    result = describe(model,name,request)
    assert result.parameters[key] == huge
    assert result.status == 'LEGACY_ONLY_UNREPRESENTABLE' and result.strict_spec is None
    assert key in result.reason
    spec = ModelSpec(name,'legacy-v1',{key:huge},tuple(FeatureRef.legacy(n) for n in NAMES),42,1,'research_v2')
    with pytest.raises(InvalidModelSpec): validate_spec(spec)


def test_mapping_proxy_snapshots_preserve_distinct_mapping_contents():
    from types import MappingProxyType
    first = {'a':1}
    second = {'b':2}
    request = request_for('factor_score',{'weights':MappingProxyType(first),'unknown':MappingProxyType(second)})
    first['a'],second['b'] = 99,88
    assert dict(request.constructor_arguments['weights']) == {'a':1}
    assert dict(request.constructor_arguments['unknown']) == {'b':2}
