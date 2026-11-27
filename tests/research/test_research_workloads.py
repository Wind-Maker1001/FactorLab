"""Admission must describe the workload that is actually dispatched."""
import json

from factorlab.experiments.research_workloads import admission_task
from factorlab.experiments.research_batch import execute_task


def task(entry='gbm'):
    return dict(id='example', entry=entry, parameters={}, inputs={}, device='cpu',
                memory_budget_mb=1024, gpu_memory_limit_mb=None, timeout_seconds=30,
                outputs=['report.json', 'samples.npz'])


def test_manifest_control_import_needs_only_standard_library():
    import subprocess
    import sys
    from pathlib import Path
    source = str(Path(__file__).resolve().parents[2]/'src')
    code = (f'import sys; sys.path.insert(0, {source!r}); '
            'from factorlab.experiments.research_tasks import validate_manifest; '
            "assert not {'numpy','pandas','scipy'} & set(sys.modules)")
    subprocess.run([sys.executable, '-I', '-S', '-c', code], check=True)


def test_default_and_overridden_mc_dimensions(tmp_path):
    value = task()
    assert admission_task(value, tmp_path)['parameters'] == dict(
        paths=100000, steps=252, assets=1, batch_size=1024, seed=1729)
    value['parameters'] = dict(paths=200, steps=10, assets=8, batch_size=20)
    admitted = admission_task(value, tmp_path)
    assert admitted['parameters']['paths'] == 200
    assert admitted['parameters']['assets'] == 8
    assert 'seed' not in value['parameters']


def test_grid_uses_input_horizon_and_streamed_batch(tmp_path):
    (tmp_path/'grid.json').write_text(json.dumps({'experiment': {'steps': 168}}))
    value = task('static_grid')
    value['inputs'] = {'definition': 'grid.json'}
    value['parameters'] = {'paths': 1000, 'batch_size': 128}
    assert admission_task(value, tmp_path)['parameters'] == dict(
        paths=1000, steps=168, assets=1, batch_size=128, seed=1729)


def test_small_registered_cpu_tasks_produce_population_outputs(tmp_path):
    import numpy as np
    for entry in ('gbm', 'bootstrap'):
        value = task(entry)
        value['parameters'] = dict(paths=32, steps=8, batch_size=16, assets=2, seed=1729)
        output = tmp_path/entry
        output.mkdir()
        execute_task(value, 'cpu', output, tmp_path, 'a'*40)
        with np.load(output/'samples.npz', allow_pickle=False) as data:
            assert data['terminal_values'].shape == (32,)
            assert np.isfinite(data['maximum_drawdowns']).all()
        report = json.loads((output/'report.json').read_text())
        assert report['engine']['compute']['device'] == 'cpu'
