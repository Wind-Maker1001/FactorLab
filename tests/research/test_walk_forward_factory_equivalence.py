"""P2-02 wiring checks, including unresolved original invocation contracts.

The positional FactorScore checks deliberately retain Baseline B's contract.
They must pass before this migration can be accepted; do not xfail them.
"""
from pathlib import Path
import runpy

import pytest

from factorlab.models.factory import LegacyModelRequest
import factorlab.validation.walk_forward as wf

FIXTURE = runpy.run_path(str(Path(__file__).with_name('test_walk_forward_stage_diagnostics.py')))
CLASSES = {
    'factor_score': 'FactorScoreModel', 'ridge': 'RidgeResearchModel',
    'lightgbm': 'LightGBMResearchModel', 'xgboost': 'XGBoostResearchModel',
}


def runner(model, **extra):
    options = dict(FIXTURE['_options'](model))
    options.update(extra)
    return wf.WalkForwardRunner(FIXTURE['_splitter'](), model_name=model,
                                feature_names=('REV1', 'MOM20'), top_k=2,
                                minimum_cross_section=4, **options)


@pytest.mark.parametrize('model', CLASSES)
def test_requests_preserve_phase_counts_explicit_keys_and_current_class(monkeypatch, model):
    original = getattr(wf, CLASSES[model])
    requests, constructions, fits, predictions = [], [], [], []
    factory = wf.create_legacy_model

    class Spy(original):
        def __init__(self, *args, **kwargs):
            constructions.append((type(self), args, kwargs))
            super().__init__(*args, **kwargs)

        def fit(self, X, y=None):
            fits.append((self, X.copy(), y.copy()))
            return super().fit(X, y)

        def predict(self, X):
            predictions.append((self, X.copy()))
            return super().predict(X)

    def observe(request, *, constructor_type):
        assert type(request) is LegacyModelRequest
        assert request.model_id == model
        assert request.implementation_version == 'legacy-v1'
        assert request.profile_id == 'legacy_csi500_v1'
        assert constructor_type is getattr(wf, CLASSES[model])
        requests.append(request)
        return factory(request, constructor_type=constructor_type)

    monkeypatch.setattr(wf, 'create_legacy_model', observe)
    instance = runner(model)
    preflight = len(requests)
    assert preflight == (2 if model in ('lightgbm', 'xgboost') else 0)
    # Bind after preflight to catch import-time or Runner-time constructor caches.
    monkeypatch.setattr(wf, CLASSES[model], Spy)
    dates, data = FIXTURE['_dataset']()
    result = instance.run(data, calendar=dates)
    folds = len(result.fold_summary)
    candidates = len(result.validation_metrics) // folds
    assert len(requests) == preflight + folds * candidates
    assert len(constructions) == len(fits) == folds * candidates
    assert len(predictions) == folds * (candidates + 2)
    assert all(cls is Spy for cls, _, _ in constructions)
    assert result.model_paths == ()
    keys = {
        'factor_score': {'normalize_by_absolute_weight'},
        'ridge': {'alpha', 'feature_names', 'fit_intercept', 'random_seed', 'max_iter', 'tolerance'},
        'lightgbm': {'feature_names', 'random_seed', 'n_jobs', 'n_estimators', 'num_leaves', 'max_depth', 'min_child_samples'},
        'xgboost': {'feature_names', 'random_seed', 'n_jobs', 'n_estimators', 'max_depth'},
    }
    for request in requests:
        arguments = request.constructor_arguments
        assert set(arguments) == keys[model]
        if model == 'factor_score':
            assert type(request.positional_arguments) is tuple
            assert request.positional_arguments == (instance.factor_weights,)
            assert list(arguments) == ['normalize_by_absolute_weight']
            assert arguments['normalize_by_absolute_weight'] is instance.normalize_by_absolute_weight
        else:
            assert request.positional_arguments == ()
        if model != 'factor_score':
            assert type(arguments['feature_names']) is tuple
            assert type(arguments['random_seed']) is int
        if model == 'ridge':
            assert type(arguments['alpha']) is float
    for _, X, y in fits:
        assert tuple(X.columns) == ('REV1', 'MOM20')
        assert y.notna().all()
    assert (result.fold_summary.fit_scope == 'train_only').all()


@pytest.mark.parametrize('model', CLASSES)
def test_original_fit_exception_propagates_without_fallback(monkeypatch, model):
    original = getattr(wf, CLASSES[model])
    failure = RuntimeError('P202 original fit failure')
    calls = []

    class Broken(original):
        def fit(self, X, y=None):
            calls.append('fit')
            raise failure

        def predict(self, X):
            pytest.fail('prediction after failed fit')

    monkeypatch.setattr(wf, CLASSES[model], Broken)
    dates, data = FIXTURE['_dataset']()
    with pytest.raises(RuntimeError) as caught:
        runner(model).run(data, calendar=dates)
    assert caught.value is failure
    assert caught.value.__cause__ is None
    assert calls == ['fit']


@pytest.mark.parametrize('tree', ['lightgbm', 'xgboost'])
def test_invalid_tree_still_fails_before_fit(monkeypatch, tree):
    original = getattr(wf, CLASSES[tree])
    calls = []

    class Spy(original):
        def __init__(self, **kwargs):
            calls.append(kwargs)
            super().__init__(**kwargs)

        def fit(self, X, y=None):
            pytest.fail('fit during preflight')

    monkeypatch.setattr(wf, CLASSES[tree], Spy)
    with pytest.raises(ValueError, match='n_estimators'):
        runner(tree, **{tree+'_candidates': [{'n_estimators': True}]})
    assert len(calls) == 1


def test_factor_score_original_positional_call_shape(monkeypatch):
    original = wf.FactorScoreModel
    calls = []

    class Spy(original):
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(wf, 'FactorScoreModel', Spy)
    dates, data = FIXTURE['_dataset']()
    instance = runner('factor_score')
    instance.run(data, calendar=dates)
    assert calls
    assert all(args == (instance.factor_weights,) and
               set(kwargs) == {'normalize_by_absolute_weight'} for args, kwargs in calls)


def test_factor_score_original_positional_only_subclass(monkeypatch):
    original = wf.FactorScoreModel

    class PositionalOnly(original):
        def __init__(self, weights, /, *, normalize_by_absolute_weight=False):
            super().__init__(weights, normalize_by_absolute_weight=normalize_by_absolute_weight)

    monkeypatch.setattr(wf, 'FactorScoreModel', PositionalOnly)
    dates, data = FIXTURE['_dataset']()
    result = runner('factor_score').run(data, calendar=dates)
    assert not result.predictions.empty
