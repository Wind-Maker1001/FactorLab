"""Opt-in native GPU acceptance: FACTORLAB_GPU_RUNTIME=1 pytest this file."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from factorlab.compute import ComputeConfig, ComputeUnavailableError
from factorlab.models import tree_compute
from factorlab.models.xgboost import XGBoostResearchModel
from factorlab.models.lightgbm import LightGBMResearchModel

pytestmark = pytest.mark.skipif(os.environ.get('FACTORLAB_GPU_RUNTIME') != '1',
    reason='Explicit native GPU hardware acceptance only')


@pytest.mark.parametrize('model_class', [XGBoostResearchModel, LightGBMResearchModel])
def test_gpu_train_predict_save_reload_and_cpu_inference(tmp_path, model_class):
    rng = np.random.default_rng(42)
    frame = pd.DataFrame(rng.normal(size=(512, 6)), columns=list('abcdef'))
    target = frame.a * .02 - frame.b * .01
    model = model_class(n_estimators=12, max_depth=3, compute=ComputeConfig(device='gpu')).fit(frame, target)
    assert model.compute_['device'] == 'gpu'
    assert model.compute_['executed']
    expected = model.predict(frame)
    fit_receipt = model.compute_.copy()
    if model_class is XGBoostResearchModel:
        assert model.compute_['execution']['native_device'].startswith('cuda')
        assert model.compute_records_[-1]['execution']['native_device'].startswith('cuda')
    else:
        assert model.compute_['backend'] in ('lightgbm-opencl', 'lightgbm-cuda')
        assert model.compute_records_[-1]['device'] == 'cpu'
        with pytest.raises(ComputeUnavailableError):
            model.predict(frame, compute=ComputeConfig(device='gpu'))
    path = tmp_path / 'gpu.pkl'
    model.save(path)
    restored = model_class.load(path)
    np.testing.assert_allclose(restored.predict(frame), expected, rtol=0, atol=0)
    with patch.object(tree_compute, '_probe', side_effect=AssertionError('GPU probed during CPU inference')):
        actual = restored.predict(frame, compute=ComputeConfig(device='cpu'))
    np.testing.assert_allclose(actual, expected, rtol=1e-5, atol=1e-7)
    assert restored.compute_ == fit_receipt
    assert restored.compute_records_[-1]['device'] == 'cpu'
    if model_class is XGBoostResearchModel:
        code = '''
import sys
sys.path.insert(0, sys.argv[1])
import numpy as np
import pandas as pd
from factorlab.compute import ComputeConfig, ComputeUnavailableError
from factorlab.models.xgboost import XGBoostResearchModel
model = XGBoostResearchModel.load(sys.argv[2])
frame = pd.DataFrame(np.random.default_rng(42).normal(size=(512, 6)), columns=list('abcdef'))
prediction = model.predict(frame, compute=ComputeConfig(device='cpu'))
assert np.isfinite(prediction).all()
assert model.compute_['device'] == 'gpu'
assert model.compute_records_[-1]['device'] == 'cpu'
try:
    model.predict(frame, compute=ComputeConfig(device='gpu'))
except ComputeUnavailableError:
    pass
else:
    raise AssertionError('CUDA unexpectedly usable with CUDA_VISIBLE_DEVICES=-1')
'''
        result = subprocess.run([sys.executable, '-I', '-c', code,
            str(Path(__file__).resolve().parents[1] / 'src'), str(path)],
            env=dict(os.environ, CUDA_VISIBLE_DEVICES='-1'), capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
