"""Native probe contracts without an installed GPU runtime."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from factorlab.compute import ComputeConfig, ComputeMemoryError, ComputeUnavailableError
from factorlab.models import tree_compute


class NativeError(Exception):
    pass


def xgb_runtime(device):
    booster = Mock()
    booster.save_config.return_value = json.dumps({"learner": {"generic_param": {"device": device}}})
    booster.predict.return_value = np.ones(128)
    estimator = Mock()
    estimator.get_booster.return_value = booster
    runtime = SimpleNamespace(XGBRegressor=Mock(return_value=estimator),
        DMatrix=Mock(side_effect=lambda frame: frame), core=SimpleNamespace(XGBoostError=NativeError))
    return runtime, estimator, booster


def test_xgb_probe_performs_native_train_and_dmatrix_predict():
    runtime, estimator, booster = xgb_runtime("cuda:0")
    assert tree_compute._probe(runtime, "xgboost", "xgboost-cuda").available
    estimator.fit.assert_called_once()
    runtime.DMatrix.assert_called_once()
    booster.predict.assert_called_once()


def test_xgb_probe_rejects_silent_cpu():
    runtime, _, booster = xgb_runtime("cpu")
    status = tree_compute._probe(runtime, "xgboost", "xgboost-cuda")
    assert not status.available
    assert "cpu" in status.reason
    booster.predict.assert_not_called()


def test_native_fit_does_not_retry_after_silent_downgrade(monkeypatch):
    runtime, estimator, _ = xgb_runtime("cpu")
    monkeypatch.setattr(tree_compute, "import_module", lambda _: runtime)
    from factorlab.compute import BackendStatus, Device, ResolvedCompute
    resolved = ResolvedCompute(ComputeConfig(device="gpu", allow_fallback=True), Device.GPU,
        "xgboost-cuda", BackendStatus(True, runtime_available=True), False, None)
    with pytest.raises(ComputeUnavailableError):
        tree_compute.fit_native("xgboost", estimator, None, None, resolved)
    estimator.fit.assert_called_once()


@pytest.mark.parametrize('family', ['xgboost', 'lightgbm'])
@pytest.mark.parametrize('device,allow_fallback,expected', [
    ('cpu', False, 'cpu'), ('auto', False, 'cpu'), ('gpu', True, 'cpu'),
    ('gpu', False, None),
])
def test_model_resolution_unavailability_policy(monkeypatch, family, device, allow_fallback, expected):
    from factorlab.compute import BackendStatus
    seen = []
    def probe(module, check):
        seen.append(module)
        return check(SimpleNamespace())
    monkeypatch.setattr(tree_compute, '_probe', lambda *args: BackendStatus(False, 'native backend unavailable'))
    monkeypatch.setattr(tree_compute, 'probe_runtime', probe)
    config = ComputeConfig(device=device, allow_fallback=allow_fallback)
    if expected is None:
        with pytest.raises(ComputeUnavailableError, match='native backend unavailable'):
            tree_compute.resolve_tree_compute(family, config)
    else:
        resolved = tree_compute.resolve_tree_compute(family, config)
        assert resolved.device.value == expected
        assert resolved.fallback == (device == 'gpu')
        if device == 'gpu':
            assert 'native backend unavailable' in resolved.fallback_reason
    if device == 'cpu':
        assert len(seen) == 1


def test_unexpected_probe_native_error_propagates():
    runtime, estimator, _ = xgb_runtime('cuda:0')
    estimator.fit.side_effect = NativeError('invalid feature')
    with pytest.raises(NativeError, match='invalid feature'):
        tree_compute._probe(runtime, 'xgboost', 'xgboost-cuda')


def test_narrow_native_memory_translation(monkeypatch):
    runtime, _, _ = xgb_runtime("cuda:0")
    monkeypatch.setattr(tree_compute, "import_module", lambda _: runtime)
    with pytest.raises(ComputeMemoryError):
        with tree_compute.native_memory("xgboost", True):
            raise NativeError("cudaErrorMemoryAllocation")
    with pytest.raises(NativeError, match="invalid feature"):
        with tree_compute.native_memory("xgboost", True):
            raise NativeError("invalid feature")
    with pytest.raises(ValueError):
        with tree_compute.native_memory("xgboost", True):
            raise ValueError("cudaErrorMemoryAllocation")
