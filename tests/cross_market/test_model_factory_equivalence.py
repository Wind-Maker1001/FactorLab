"""P2-03 comparisons against the direct caller frozen in Baseline B.

The two Spy contract tests deliberately remain ordinary acceptance tests: a
known adapter limitation must not be hidden with xfail, skip, or weaker asserts.
"""
import inspect
import pickle

import numpy as np
import pandas as pd
import pytest

from factorlab.cross_market import research as cm
from factorlab.cross_market.contracts import ContractError
from factorlab.models import (
    FactorScoreModel, RidgeResearchModel, LightGBMResearchModel, XGBoostResearchModel,
)


# Exact make_model body from ef91f3d26801dc660fbef8f3826bfa37732dff33.
# Independent of the candidate request builder and the factory implementation.
FROZEN_CALLER = '''def make_model(name, names, estimators=40):
    if name == 'FactorScore':
        return FactorScoreModel({n: (-1.0 if n.startswith(('REV', 'VOL', 'AMIHU')) else 1.0) for n in names})
    if name == 'Ridge':
        return RidgeResearchModel(feature_names=names)
    if name == 'LightGBM':
        return LightGBMResearchModel(feature_names=names, n_estimators=estimators, n_jobs=1)
    if name == 'XGBoost':
        return XGBoostResearchModel(feature_names=names, n_estimators=estimators, n_jobs=1)
    raise ContractError('unknown model')'''
CLASSES = dict(FactorScore=FactorScoreModel, Ridge=RidgeResearchModel,
               LightGBM=LightGBMResearchModel, XGBoost=XGBoostResearchModel)
NAMES = ['MOM20', 'REV1', 'VOL20', 'AMIHU20']


def direct(name, names, estimators=40):
    bindings = {cls.__name__: getattr(cm, cls.__name__) for cls in CLASSES.values()}
    bindings['ContractError'] = ContractError
    exec(compile(FROZEN_CALLER, '<Baseline-B-make_model>', 'exec'), bindings)
    return bindings['make_model'](name, names, estimators)


def outcome(func):
    try:
        obj = func()
    except Exception as exc:
        return type(exc), str(exc), type(exc.__cause__)
    return type(obj), pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)


@pytest.mark.parametrize('name', CLASSES)
@pytest.mark.parametrize('estimators', [40, 8, -7, True, '8', np.int64(8)])
def test_constructor_state_defaults_and_errors(name, estimators):
    assert outcome(lambda: cm.make_model(name, list(NAMES), estimators)) == outcome(
        lambda: direct(name, list(NAMES), estimators))


@pytest.mark.parametrize('name', CLASSES)
@pytest.mark.parametrize('names', [[], None, [''], ['a', 'a'], [1], [' '],
                                  ['rev1', 'Vol20', 'AMIHU20', 'REV'],
                                  ('b', 'a'), np.array(['b', 'a']), 'ab'])
def test_feature_acceptance_and_original_error_type_message(name, names):
    assert outcome(lambda: cm.make_model(name, names)) == outcome(lambda: direct(name, names))


@pytest.mark.parametrize('name', ['missing', 'factor_score', 'ridge', 'RIDGE', 'lightgbm',
                                  'xgboost', ' Ridge', '', None, 0])
def test_unknown_names_do_not_enter_factory(monkeypatch, name):
    def unexpected(*args, **kwargs):
        raise AssertionError('unknown names must not enter the factory')
    monkeypatch.setattr(cm, 'create_legacy_model', unexpected)
    with pytest.raises(ContractError, match='^unknown model$'):
        cm.make_model(name, NAMES)


def test_fixed_sign_case_order_duplicates_and_omitted_normalization():
    names = ['rev1', 'VOL20', 'MOM20', 'REV1', 'AMIHU20', 'vol20', 'REV1']
    model = cm.make_model('FactorScore', names, object())
    assert list(model.weights.items()) == [
        ('rev1', 1.), ('VOL20', -1.), ('MOM20', 1.), ('REV1', -1.),
        ('AMIHU20', -1.), ('vol20', 1.)]
    assert model.normalize_by_absolute_weight is False
    assert not model.is_fitted
    assert pickle.dumps(model) == pickle.dumps(direct('FactorScore', names))


def test_public_signatures_keep_40():
    assert str(inspect.signature(cm.make_model)) == '(name, names, estimators=40)'
    for func in [cm.run_models, cm.run_research]:
        assert inspect.signature(func).parameters['estimators'].default == 40


@pytest.mark.parametrize('name', CLASSES)
def test_request_profile_explicit_keys_module_binding_and_one_construction(monkeypatch, name):
    original = CLASSES[name]
    calls, requests = [], []

    class Spy(original):
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            super().__init__(*args, **kwargs)

    factory = cm.create_legacy_model

    def record(request, **kwargs):
        requests.append((request, kwargs))
        return factory(request, **kwargs)

    monkeypatch.setattr(cm, original.__name__, Spy)
    monkeypatch.setattr(cm, 'create_legacy_model', record)
    actual = cm.make_model(name, NAMES)
    assert type(actual) is Spy and len(calls) == len(requests) == 1
    request, kwargs = requests[0]
    assert request.profile_id == 'cross_market_fixed_v1'
    assert request.implementation_version == 'legacy-v1'
    assert request.model_id == ('factor_score' if name == 'FactorScore' else name)
    assert kwargs == {'constructor_type': Spy}
    expected = set() if name == 'FactorScore' else {'feature_names'}
    if name == 'FactorScore':
        assert request.positional_arguments == ({n: (-1.0 if n.startswith(('REV', 'VOL', 'AMIHU')) else 1.0) for n in NAMES},)
        assert calls[0] == (request.positional_arguments, {})
    else:
        assert request.positional_arguments == ()
    if name in ('LightGBM', 'XGBoost'):
        expected |= {'n_estimators', 'n_jobs'}
        assert actual.n_estimators == 40 and actual.n_jobs == 1
    assert set(request.constructor_arguments) == expected
    assert not actual.is_fitted


@pytest.mark.parametrize('name', CLASSES)
def test_frozen_constructor_call_shape_with_spy(monkeypatch, name):
    original = CLASSES[name]
    calls = []

    class Spy(original):
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(cm, original.__name__, Spy)
    direct(name, NAMES)
    expected = calls.pop()
    cm.make_model(name, NAMES)
    assert len(calls) == 1
    # FS retains B's single positional dict through the B2-compatible factory.
    assert calls[0] == expected


def test_factor_score_positional_only_replacement_preserves_acceptance(monkeypatch):
    class PositionalSpy(FactorScoreModel):
        def __init__(self, weights, /):
            super().__init__(weights)

    monkeypatch.setattr(cm, 'FactorScoreModel', PositionalSpy)
    expected = direct('FactorScore', NAMES)  # Original succeeds.
    actual = cm.make_model('FactorScore', NAMES)  # Must succeed under equivalence.
    assert type(actual) is type(expected) is PositionalSpy
    assert vars(actual) == vars(expected)


@pytest.mark.parametrize('name', CLASSES)
def test_bound_constructor_failure_propagates_once(monkeypatch, name):
    original = CLASSES[name]
    calls = []

    class Failure(RuntimeError):
        pass

    class Spy(original):
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise Failure('original constructor failure')

    monkeypatch.setattr(cm, original.__name__, Spy)
    with pytest.raises(Failure, match='^original constructor failure$'):
        cm.make_model(name, NAMES)
    assert len(calls) == 1


@pytest.mark.parametrize('name', CLASSES)
@pytest.mark.parametrize('estimators', [40, 8])
def test_fit_predict_state_serialization_and_trusted_load(tmp_path, name, estimators):
    rng = np.random.default_rng(42)
    X = pd.DataFrame(rng.normal(size=(80, 4)), columns=NAMES)
    X.iloc[2, 0] = np.nan
    y = pd.Series(rng.normal(size=len(X)), index=X.index)
    expected, actual = direct(name, NAMES, estimators), cm.make_model(name, NAMES, estimators)
    assert type(actual) is type(expected) is CLASSES[name]
    assert not expected.is_fitted and not actual.is_fitted
    expected.fit(X, y)
    actual.fit(X, y)
    assert expected.is_fitted and actual.is_fitted
    pd.testing.assert_series_equal(expected.predict(X), actual.predict(X), check_exact=True)
    assert pickle.dumps(actual, protocol=pickle.HIGHEST_PROTOCOL) == pickle.dumps(
        expected, protocol=pickle.HIGHEST_PROTOCOL)
    left, right = tmp_path / 'baseline.pkl', tmp_path / 'candidate.pkl'
    expected.save(left)
    actual.save(right)
    assert left.read_bytes() == right.read_bytes()
    restored = CLASSES[name].load(right)
    assert type(restored) is CLASSES[name] and restored.is_fitted
    pd.testing.assert_series_equal(expected.predict(X), restored.predict(X), check_exact=True)
    wrong = RidgeResearchModel if name == 'FactorScore' else FactorScoreModel
    with pytest.raises(TypeError):
        wrong.load(right)
