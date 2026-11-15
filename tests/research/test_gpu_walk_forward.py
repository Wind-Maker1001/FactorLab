"""Execution policy remains outside validation-selected tree parameters."""
from dataclasses import asdict

import pytest

from factorlab.compute import ComputeConfig
from factorlab.models.factory import DEFAULT_CATALOG, LegacyModelRequest, create_legacy_model, InvalidModelSpec
from factorlab.validation import WalkForwardRunner
from tests.research.test_tree_models_phase3 import _walk_forward_data, _splitter


@pytest.mark.parametrize('name', ['xgboost', 'lightgbm'])
def test_factory_compute_is_execution_configuration(name):
    config = ComputeConfig(device='cpu')
    request = LegacyModelRequest(name, 'legacy-v1', 'legacy_csi500_v1',
        dict(feature_names=('REV1', 'MOM20'), n_estimators=4))
    model = create_legacy_model(request, compute=config)
    assert 'compute' not in request.constructor_arguments
    assert 'compute' not in DEFAULT_CATALOG.resolve(name, 'legacy-v1').parameter_defaults
    assert not model.is_fitted


def test_factory_rejects_compute_for_linear_model():
    request = LegacyModelRequest('ridge', 'legacy-v1', 'legacy_csi500_v1', {})
    with pytest.raises(InvalidModelSpec, match='native tree'):
        create_legacy_model(request, compute=ComputeConfig())


@pytest.mark.parametrize('name', ['xgboost', 'lightgbm'])
def test_cpu_walk_forward_records_each_execution_without_changing_selection(name):
    dates, frame = _walk_forward_data()
    kwargs = {name + '_candidates': [dict(n_estimators=4, max_depth=2)]}
    def runner(compute=None):
        return WalkForwardRunner(_splitter(), model_name=name,
            feature_names=('REV1', 'MOM20'), minimum_cross_section=2,
            top_k=2, compute=compute, **kwargs)
    legacy = runner().run(frame, calendar=dates)
    actual = runner(ComputeConfig()).run(frame, calendar=dates)
    import pandas as pd
    pd.testing.assert_frame_equal(actual.predictions, legacy.predictions)
    assert not legacy.compute_records
    assert actual.compute_records
    assert {r['evaluation_stage'] for r in actual.compute_records} == {
        'TRAIN', 'VALIDATION_SELECTION', 'HISTORICAL_TEST', 'TRAIN_IN_SAMPLE'}
    for record in actual.compute_records:
        assert record['requested'] == asdict(ComputeConfig())
        assert record['device'] == 'cpu'
        assert not record['gpu_memory_limit_enforced']
    assert actual.validation_metrics.parameters.tolist() == legacy.validation_metrics.parameters.tolist()
