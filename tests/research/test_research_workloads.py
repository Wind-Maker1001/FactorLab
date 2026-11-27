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


def test_bootstrap_admission_reads_archive_shape_without_materializing_history(tmp_path):
    import numpy as np
    from factorlab.experiments.research_selection import select_task
    value = task('bootstrap')
    value['parameters'] = {'paths': 100, 'assets': 2, 'steps': 8, 'batch_size': 16}
    value['inputs'] = {'returns': 'history.npz'}
    np.savez(tmp_path/'history.npz', returns=np.zeros((10000, 2)),
             timestamps=np.arange(10000).astype('datetime64[D]'))
    admitted = admission_task(value, tmp_path)
    assert admitted['parameters']['history_rows'] == 10000
    selected = select_task(admitted, {'host_available_bytes': 8*1024**3, 'gpus': []})
    assert selected['evidence'][0]['model_bytes'] > 10000*2*8


def test_benchmark_cells_are_sequential_and_seed_workers_bounded(tmp_path, monkeypatch):
    from pathlib import Path
    from factorlab.experiments import research_workloads
    from factorlab.experiments.research_selection import select_task
    config = json.loads((Path(__file__).parents[2]/'configs/research/mc_colab_02_scale.json').read_text())
    monkeypatch.setattr(research_workloads, 'benchmark_config', lambda _: config)
    value = task('benchmark')
    value.update(memory_budget_mb=8192)
    value['parameters'] = dict(benchmark='mc_colab_02_scale', workers=1)
    resources = dict(host_available_bytes=16*1024**3, gpus=[])
    one = select_task(admission_task(value, tmp_path), resources)
    assert one['estimated_host_bytes'] < 1024**3
    assert len(one['evidence'][0]['cells']) == len(config['cells'])
    value['device'] = 'local_gpu'
    gpu_resources = dict(host_available_bytes=8*1024**3, gpus=[dict(index=0,
        free_memory_bytes=8*1024**3, utilization_percent=0)])
    assert select_task(admission_task(value, tmp_path), gpu_resources)['device'] == 'local_gpu'
    value['device'] = 'colab'
    cloud = dict(authorized=True, available=True, balance_cu=1, budget_cu=1,
        rate_per_hour=2.33, startup_seconds=120, host_memory_bytes=8*1024**3,
        gpu_memory_bytes=8*1024**3)
    assert select_task(admission_task(value, tmp_path), resources, cloud)['device'] == 'colab'
    value['device'] = 'cpu'
    value['parameters']['workers'] = 20
    many = select_task(admission_task(value, tmp_path), resources)
    assert many['estimated_host_bytes'] == 5 * one['estimated_host_bytes']
    value['parameters']['first_seed_only'] = True
    assert select_task(admission_task(value, tmp_path), resources)['estimated_host_bytes'] == one['estimated_host_bytes']
    config['cells'][0]['n_paths'] = 200000000
    import pytest
    with pytest.raises(RuntimeError, match='insufficient'):
        select_task(admission_task(value, tmp_path), resources)


def test_benchmark_gpu_auto_batch_and_pool_limits(tmp_path, monkeypatch):
    import pytest
    from factorlab.experiments import research_workloads
    from factorlab.experiments.research_workloads import benchmark_memory
    from factorlab.experiments.research_selection import select_task
    cell = dict(name='auto', model='gbm', n_paths=100000, n_steps=8760,
                assets=1, batch_size=None, gpu_memory_limit_mb=1024)
    monkeypatch.setattr(research_workloads, 'benchmark_config',
                        lambda _: dict(cells=[cell], seeds=[1, 2, 3, 4, 5]))
    value = task('benchmark')
    value.update(device='local_gpu', memory_budget_mb=8192, gpu_memory_limit_mb=512)
    value['parameters'] = dict(benchmark='mc_colab_02_scale')
    resources = dict(host_available_bytes=8*1024**3, gpus=[dict(index=0,
        free_memory_bytes=1024**3, utilization_percent=0)])
    chosen = select_task(admission_task(value, tmp_path), resources)
    plan = chosen['evidence'][0]['cells'][0]
    assert 0 < plan['batch_size'] < 4096
    assert plan == benchmark_memory(cell, device='gpu', free_gpu_bytes=1024**3, gpu_limit_mb=512)
    pool_plan = benchmark_memory(cell, device='gpu', gpu_limit_mb=512)
    tight_plan = benchmark_memory(cell, device='gpu', free_gpu_bytes=128*1024**2, gpu_limit_mb=512)
    assert tight_plan['batch_size'] < pool_plan['batch_size']
    assert tight_plan['estimated_in_memory_bytes'] == pool_plan['estimated_in_memory_bytes']
    assert tight_plan['estimated_disk_working_bytes'] == pool_plan['estimated_disk_working_bytes']
    resources['host_available_bytes'] = pool_plan['estimated_in_memory_bytes'] - 1
    with pytest.raises(RuntimeError, match='GPU or host'):
        select_task(admission_task(value, tmp_path), resources)
    resources['host_available_bytes'] = 8*1024**3
    value['gpu_memory_limit_mb'] = 1
    with pytest.raises(RuntimeError, match='one benchmark path'):
        select_task(admission_task(value, tmp_path), resources)
    value['gpu_memory_limit_mb'] = 512
    value['parameters'].update(workers=2, first_seed_only=True)
    with pytest.raises(RuntimeError, match='GPU or host'):
        select_task(admission_task(value, tmp_path), resources)


def test_benchmark_plan_matches_prepared_persistent_arrays():
    from factorlab.experiments.research_workloads import benchmark_memory
    from factorlab.experiments.mc_colab_benchmark import make_inputs, simulation_config
    from factorlab.research.monte_carlo import _prepare
    for model in ('gbm', 'moving_block_bootstrap'):
        cell = dict(model=model, n_paths=100, n_steps=8, assets=2,
                    batch_size=None, gpu_memory_limit_mb=1024)
        arrays, *_ = _prepare(simulation_config(cell, 1729, 'cpu'), make_inputs(cell))
        persistent = sum(v.nbytes + 512 for v in arrays.values()) + 65536
        assert benchmark_memory(cell)['persistent_bytes'] == persistent


def test_benchmark_disk_admission_matches_execution_budget(tmp_path, monkeypatch):
    import pytest
    from factorlab.experiments import research_workloads
    from factorlab.experiments.research_selection import select_task
    cell = dict(model='gbm', n_paths=16000000, n_steps=8, assets=1,
                batch_size=16, gpu_memory_limit_mb=1024)
    monkeypatch.setattr(research_workloads, 'benchmark_config',
                        lambda _: dict(cells=[cell], seeds=[1]))
    value = task('benchmark')
    value['parameters'] = dict(benchmark='mc_colab_02_scale', statistics=False)
    resources = dict(host_available_bytes=1024**3, gpus=[])
    value['memory_budget_mb'] = 8192
    with pytest.raises(RuntimeError, match='insufficient'):
        select_task(admission_task(value, tmp_path), resources)
    value['memory_budget_mb'] = 1024
    assert select_task(admission_task(value, tmp_path), resources)['device'] == 'cpu'
    value['parameters']['statistics'] = True
    with pytest.raises(RuntimeError, match='insufficient'):
        select_task(admission_task(value, tmp_path), resources)


def test_benchmark_execution_uses_actual_seed_concurrency(tmp_path, monkeypatch):
    import pytest
    from pathlib import Path
    from factorlab.experiments import mc_colab_benchmark as benchmark
    config = json.loads((Path(__file__).parents[2]/'configs/research/mc_colab_02_scale.json').read_text())
    class Planned(Exception):
        pass
    seen = []
    def plan(cell, workers, device):
        seen.append(workers)
        raise Planned
    monkeypatch.setattr(benchmark, 'memory_plan', plan)
    for first, expected in ((False, 5), (True, 1)):
        with pytest.raises(Planned):
            benchmark.run_study(config, tmp_path/str(first), workers=20,
                first_seed_only=first, source_commit='a'*40)
        assert seen[-1] == expected


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
