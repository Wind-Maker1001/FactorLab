"""Tree model compute dispatch and stage evidence."""

import pickle
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from factorlab.compute import BackendStatus, ComputeConfig, Device, ResolvedCompute
from factorlab.models import tree_compute
from factorlab.models.xgboost import XGBoostResearchModel
from factorlab.models.lightgbm import LightGBMResearchModel


@pytest.fixture
def data():
    rng = np.random.default_rng(2)
    frame = pd.DataFrame(rng.normal(size=(64, 3)), columns=["a", "b", "c"])
    return frame, pd.Series(frame.a + frame.b ** 2, index=frame.index)


@pytest.mark.parametrize("model_type,runtime", [(XGBoostResearchModel, "xgboost"), (LightGBMResearchModel, "lightgbm")])
def test_real_cpu_receipts_reload_and_atomic_refit(model_type, runtime, data):
    pytest.importorskip(runtime)
    frame, target = data
    model = model_type(n_estimators=3, compute=ComputeConfig(device="cpu"))
    model.fit(frame, target)
    fit_receipt = model.compute_.copy()
    assert fit_receipt["device"] == "cpu"
    assert fit_receipt["stage"] == "fit"
    assert fit_receipt["executed"]
    assert not fit_receipt["gpu_memory_limit_enforced"]
    restored = pickle.loads(pickle.dumps(model))
    with patch.object(tree_compute, "_probe", side_effect=AssertionError("GPU probed")):
        prediction = restored.predict(frame, compute=ComputeConfig(device="cpu"))
    assert np.isfinite(prediction).all()
    assert restored.compute_ == fit_receipt
    assert restored.compute_records_[-1]["stage"] == "predict"
    assert restored.compute_records_[-1]["device"] == "cpu"
    previous = model.estimator_
    with patch(f"factorlab.models.{runtime}.fit_native", side_effect=RuntimeError("native fit failed")):
        with pytest.raises(RuntimeError, match="native fit failed"):
            model.fit(frame, target)
    assert model.estimator_ is previous
    assert model.compute_ == fit_receipt


@pytest.mark.parametrize("model_type", [XGBoostResearchModel, LightGBMResearchModel])
def test_omitted_compute_preserves_initial_state(model_type):
    assert vars(model_type()) == vars(model_type(compute=None))
    assert "compute" not in vars(model_type())


def test_cpu_resolution_never_probes_gpu():
    with patch.object(tree_compute, "probe_runtime", return_value=BackendStatus(True, runtime_available=True)) as probe:
        resolved = tree_compute.resolve_tree_compute("xgboost", ComputeConfig(device="cpu"))
    assert resolved.backend == "xgboost-cpu"
    assert probe.call_count == 1


def test_gpu_lightgbm_parameters_omit_cpu_flags():
    resolved = ResolvedCompute(ComputeConfig(device="gpu"), Device.GPU, "lightgbm-opencl",
                               BackendStatus(True, runtime_available=True), False, None)
    assert tree_compute.tree_parameters("lightgbm", resolved) == {"device_type": "gpu"}


def test_lightgbm_prediction_is_cpu_and_not_fallback():
    model = LightGBMResearchModel(compute=ComputeConfig(device="gpu"))
    resolved = tree_compute.prediction_compute("lightgbm", model, None)
    assert resolved.device == Device.CPU
    assert not resolved.fallback


@pytest.mark.parametrize('model_type,runtime', [(XGBoostResearchModel, 'xgboost'), (LightGBMResearchModel, 'lightgbm')])
def test_missing_runtime_uses_compute_error_before_native_constructor(model_type, runtime, data):
    from factorlab.compute import ComputeUnavailableError
    frame, target = data
    with patch.object(tree_compute, 'probe_runtime', return_value=BackendStatus(False, 'runtime missing')):
        with patch(f'factorlab.models.{runtime}._{runtime}_regressor_class', side_effect=AssertionError('native import before resolution')):
            with pytest.raises(ComputeUnavailableError, match='runtime missing'):
                model_type(compute=ComputeConfig(device='gpu')).fit(frame, target)


def test_native_trainer_proof_rejects_silent_cpu():
    with pytest.raises(tree_compute.ComputeUnavailableError):
        tree_compute._trainer_proof(["CPU trainer"], "lightgbm-opencl")
    assert tree_compute._trainer_proof(["This is the GPU trainer!!"], "lightgbm-opencl")
