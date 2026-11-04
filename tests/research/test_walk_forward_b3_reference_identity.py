"""Live identity at the real two-fold runner's constructor boundary."""
import pickle
import runpy
from pathlib import Path
import pytest
import factorlab.validation.walk_forward as wf
from factorlab.models.factor_score import FactorScoreModel
from factorlab.models.factory import LegacyModelRequest

FIXTURE = runpy.run_path(str(Path(__file__).with_name('test_walk_forward_stage_diagnostics.py')))

def make_runner():
    return wf.WalkForwardRunner(FIXTURE['_splitter'](), model_name='factor_score',
        feature_names=('REV1', 'MOM20'), top_k=2, minimum_cross_section=4,
        **FIXTURE['_options']('factor_score'))

def test_real_runner_keeps_original_across_positional_only_entries(monkeypatch):
    instance = make_runner()
    received, models, raw = [], [], []
    class PositionalOnly(FactorScoreModel):
        def __init__(self, weights, /, *, normalize_by_absolute_weight=False):
            received.append(weights)
            assert weights is instance.factor_weights
            assert normalize_by_absolute_weight is instance.normalize_by_absolute_weight
            super().__init__(weights, normalize_by_absolute_weight=normalize_by_absolute_weight)
            models.append(self)
    class Entry(FactorScoreModel):
        def __new__(cls, *args, **kwargs):
            raw.append((args, kwargs))
            assert len(args) == 1 and args[0] is instance.factor_weights
            assert list(kwargs) == ['normalize_by_absolute_weight']
            return PositionalOnly(*args, **kwargs)
    monkeypatch.setattr(wf, 'FactorScoreModel', Entry)
    dates, data = FIXTURE['_dataset']()
    result = instance.run(data, calendar=dates)
    assert len(result.fold_summary) == len(raw) == len(received) == 2
    assert received[0] is received[1] is instance.factor_weights
    assert models[0] is not models[1]
    assert models[0].weights is not models[1].weights
    assert all(m.weights is not instance.factor_weights for m in models)
    assert not result.predictions.empty

def test_real_runner_observes_native_mutation_exception_once(monkeypatch):
    instance = make_runner()
    failure, cause = RuntimeError('B3 native failure'), ValueError('native cause')
    received = []
    class Broken(FactorScoreModel):
        def __init__(self, weights, /, *, normalize_by_absolute_weight=False):
            received.append(weights)
            assert weights is instance.factor_weights
            weights['REV1'] = -13.0
            raise failure from cause
    monkeypatch.setattr(wf, 'FactorScoreModel', Broken)
    dates, data = FIXTURE['_dataset']()
    with pytest.raises(RuntimeError) as caught:
        instance.run(data, calendar=dates)
    assert caught.value is failure and caught.value.__cause__ is cause
    assert len(received) == 1 and received[0] is instance.factor_weights
    assert instance.factor_weights['REV1'] == -13.0

def test_default_request_counterexample_value_pickle_equal_identity_wrong(monkeypatch):
    instance = make_runner()
    received = []
    factory = wf.create_legacy_model
    class Spy(FactorScoreModel):
        def __init__(self, weights, /, *, normalize_by_absolute_weight=False):
            received.append(weights)
            super().__init__(weights, normalize_by_absolute_weight=normalize_by_absolute_weight)
    def ordinary(request, *, constructor_type):
        request = LegacyModelRequest(request.model_id, request.implementation_version,
            request.profile_id, request.constructor_arguments, (instance.factor_weights,))
        return factory(request, constructor_type=constructor_type)
    monkeypatch.setattr(wf, 'FactorScoreModel', Spy)
    monkeypatch.setattr(wf, 'create_legacy_model', ordinary)
    dates, data = FIXTURE['_dataset']()
    instance.run(data, calendar=dates)
    assert len(received) == 2
    assert all(w == instance.factor_weights for w in received)
    assert all(pickle.dumps(w) == pickle.dumps(instance.factor_weights) for w in received)
    assert all(w is not instance.factor_weights for w in received)
    assert received[0] is not received[1]
