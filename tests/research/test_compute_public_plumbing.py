"""Public compute requests and execution receipts remain separate from candidates."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from factorlab.compute import ComputeConfig
from factorlab.errors import ConfigurationError
from factorlab.experiments.adapters import walk_forward_metadata
from factorlab.research import real_model_study
from tools.research.pipeline_config import compute_config


@pytest.mark.parametrize('value', [{}, {'device': 'cpu'},
    {'device': 'gpu', 'allow_fallback': True, 'gpu_memory_limit_mb': 512}])
def test_compute_json_policy(value):
    assert asdict(compute_config(value)) == asdict(ComputeConfig(**value))


@pytest.mark.parametrize('value', [None, [], {'device': 'bogus'},
    {'allow_fallback': 1}, {'gpu_memory_limit_mb': 0}, {'backend': 'cuda'}])
def test_compute_json_rejects_invalid_policy(value):
    with pytest.raises((ValueError, TypeError, ConfigurationError)):
        compute_config(value)


@pytest.mark.parametrize('model', ['factor_score', 'ridge', 'xgboost', 'lightgbm'])
@pytest.mark.parametrize('explicit', [False, True])
def test_real_study_forwards_compute_only_to_trees(monkeypatch, model, explicit):
    monkeypatch.setattr(real_model_study, 'WalkForwardRunner',
        lambda splitter, **options: options)
    config = dict(seed=1, minimum_cross_section=4, top_k=2, threads=1,
        splitter=dict(initial_train_months=3, validation_months=1, test_months=1),
        parameters={'ridge': dict(alpha=1., fit_intercept=True, max_iter=100, tolerance=1e-6),
                    'xgboost': {'max_depth': 2}, 'lightgbm': {'num_leaves': 4}})
    if explicit:
        config['compute'] = {'device': 'gpu', 'allow_fallback': True}
    options = real_model_study.make_runner(config, model, ['MOM20'])
    if explicit and model in {'xgboost', 'lightgbm'}:
        assert options['compute'] == ComputeConfig(device='gpu', allow_fallback=True)
        assert 'compute' not in options[model + '_candidates'][0]
    else:
        assert 'compute' not in options


def test_actual_receipts_do_not_change_sample_identity():
    from tests.research.test_walk_forward_runner_phase2 import _dataset, _splitter
    from factorlab.validation.walk_forward import WalkForwardRunner
    dates, data = _dataset(end='2020-09-30')
    walk = WalkForwardRunner(_splitter(), model_name='ridge',
        feature_names=['REV1', 'MOM20'], ridge_alphas=[1.], top_k=2,
        minimum_cross_section=4).run(data, calendar=dates)
    result = SimpleNamespace(**vars(walk))
    result.compute_records = ()
    baseline = walk_forward_metadata(result, top_k=2, minimum_cross_section=4)
    assert 'environment' not in baseline
    records = ({'fold': 0, 'candidate': 0, 'stage': 'fit', 'actual_device': 'gpu'},)
    result.compute_records = records
    actual = walk_forward_metadata(result, top_k=2, minimum_cross_section=4)
    assert actual['environment'] == {'compute': list(records)}
    assert actual['validation'] == baseline['validation']
