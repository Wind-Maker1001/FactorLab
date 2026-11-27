"""Offline validation, isolated batch execution, and artifact transport."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

from factorlab.experiments import research_batch as batch
from factorlab.experiments import research_selection as selection
from factorlab.experiments import research_tasks as tasks


@pytest.fixture
def manifest():
    return dict(schema_version=1, commit='a' * 40, timeout_seconds=1800,
                budget_cu=2, tasks=[dict(
                    id='first', entry='gbm', parameters=dict(paths=16, steps=2),
                    inputs={}, device='cpu', memory_budget_mb=512,
                    gpu_memory_limit_mb=None, timeout_seconds=30,
                    outputs=['report.json', 'samples.npz'])])


@pytest.mark.parametrize('entry', tasks.ENTRIES)
def test_registered_entries(manifest, tmp_path, entry):
    task = manifest['tasks'][0]
    task['entry'] = entry
    if entry == 'static_grid':
        (tmp_path / 'grid.json').write_text('{}')
        task.update(parameters={}, inputs={'definition': 'grid.json'})
    elif entry == 'benchmark':
        task['parameters'] = {'benchmark': tasks.BENCHMARKS[0]}
    assert tasks.validate_manifest(manifest, tmp_path) is manifest


@pytest.mark.parametrize('field,value', [
    ('schema_version', 2), ('commit', 'main'), ('timeout_seconds', float('nan')),
    ('timeout_seconds', True), ('budget_cu', -1), ('budget_cu', float('inf')),
    ('tasks', []),
])
def test_invalid_manifest_fields(manifest, field, value):
    manifest[field] = value
    with pytest.raises(ValueError):
        tasks.validate_manifest(manifest)


@pytest.mark.parametrize('field,value', [
    ('id', '../escape'), ('entry', 'arbitrary_module'), ('device', 'cuda'),
    ('parameters', {'script': 'execute.py'}), ('parameters', {'seed': -1}),
    ('parameters', {'paths': True}), ('parameters', {'steps': 1.5}),
    ('memory_budget_mb', 0), ('gpu_memory_limit_mb', -1),
    ('timeout_seconds', float('inf')), ('outputs', ['../outside']),
    ('outputs', ['same', 'same']), ('inputs', {'definition': 'grid.json'}),
])
def test_invalid_task_fields(manifest, field, value):
    manifest['tasks'][0][field] = value
    with pytest.raises(ValueError):
        tasks.validate_manifest(manifest)


def test_duplicate_ids_and_unknown_fields(manifest):
    manifest['tasks'].append(deepcopy(manifest['tasks'][0]))
    with pytest.raises(ValueError, match='Unique'):
        tasks.validate_manifest(manifest)
    manifest['tasks'].pop()
    manifest['tasks'][0]['command'] = 'arbitrary'
    with pytest.raises(ValueError, match='fields'):
        tasks.validate_manifest(manifest)


@pytest.mark.parametrize('name', ['../outside.npy', '/absolute.npy', 'C:/drive.npy',
                                  'data\\windows.npy', 'a//b.npy', './data.npy',
                                  'secret.json', 'credentials.json', '.env.json', 'code.py'])
def test_input_paths_refuse_escape_and_secrets(tmp_path, name):
    with pytest.raises(ValueError):
        tasks.input_path(tmp_path, name)


def test_input_symlink_refused(tmp_path):
    source = tmp_path / 'data.npy'
    source.write_bytes(b'data')
    link = tmp_path / 'linked.npy'
    try:
        link.symlink_to(source)
    except OSError as exc:
        pytest.skip(f'Symlink creation unavailable: {exc}')
    with pytest.raises(ValueError, match='Symbolic'):
        tasks.input_path(tmp_path, 'linked.npy')


def test_bundle_round_trip_preserves_nested_binary_and_outcomes(tmp_path):
    source = tmp_path / 'source'
    nested = source / 'first' / 'nested'
    nested.mkdir(parents=True)
    (nested / 'samples.npz').write_bytes(bytes(range(256)))
    (source / 'first' / 'worker.log').write_text('retained failure')
    report = {'state': 'PARTIAL', 'outcomes': [{'id': 'first', 'state': 'FAILED'}]}
    batch.bundle_results(source, report)
    unpacked = tmp_path / 'unpacked'
    restored = batch.unpack_results(source, unpacked)
    assert restored['outcomes'] == report['outcomes']
    assert (unpacked / 'first/nested/samples.npz').read_bytes() == bytes(range(256))
    assert (unpacked / 'first/worker.log').read_text() == 'retained failure'


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/drive', 'a\\b', 'a/../b', './file'])
def test_unpack_refuses_malicious_names_before_creating_destination(tmp_path, name):
    (tmp_path / 'report.json').write_text(json.dumps({'artifact_manifest': {'artifact_0': name}}))
    np.savez(tmp_path / 'samples.npz', artifact_0=np.array([1], dtype=np.uint8))
    target = tmp_path / 'unpacked'
    with pytest.raises(ValueError):
        batch.unpack_results(tmp_path, target)
    assert not target.exists()


@pytest.mark.parametrize('kind', ['duplicate', 'index', 'dtype', 'shape'])
def test_unpack_refuses_invalid_bundle(tmp_path, kind):
    index = {'a': 'first/file'}
    arrays = {'a': np.array([1], dtype=np.uint8)}
    if kind == 'duplicate':
        index['b'] = 'first/file'
        arrays['b'] = arrays['a']
    elif kind == 'index':
        arrays['unexpected'] = arrays.pop('a')
    elif kind == 'dtype':
        arrays['a'] = np.array([1], dtype=np.int64)
    else:
        arrays['a'] = np.array([[1]], dtype=np.uint8)
    (tmp_path / 'report.json').write_text(json.dumps({'artifact_manifest': index}))
    np.savez(tmp_path / 'samples.npz', **arrays)
    with pytest.raises(ValueError):
        batch.unpack_results(tmp_path, tmp_path / 'unpacked')


@pytest.fixture
def local_batch(manifest, tmp_path, monkeypatch):
    manifest['tasks'] = [dict(deepcopy(manifest['tasks'][0]), id=name)
                         for name in ('first', 'second', 'third')]
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(batch, 'installed_commit', lambda commit: {'commit': commit})
    monkeypatch.setattr(selection, 'probe_resources', lambda **kw: {})
    monkeypatch.setattr(selection, 'select_task', lambda task, resources:
                        {'device': 'cpu', 'compute_device': 'cpu'})
    return path, tmp_path / 'result'


def test_batch_continues_after_failure_and_timeout_in_fresh_isolated_workers(local_batch, monkeypatch):
    manifest_path, output = local_batch
    calls = []
    def run(argv, *, timeout, stdout, stderr, cwd=None, env=None):
        if argv[4] == 'bundle':
            assert argv[5:] == ['--source', str(output)]
            assert 0 < timeout <= 1800
            batch.bundle_results(output, json.loads((output / 'state.json').read_text()))
            return 0
        assert env['OMP_NUM_THREADS'] == '1'
        assert argv[:5] == [sys.executable, '-I', '-m',
                            'factorlab.experiments.research_batch', 'worker']
        assert argv[5] == '--spec'
        spec = json.loads(Path(argv[6]).read_text())
        directory = Path(spec['output'])
        assert cwd == directory / 'work'
        assert cwd.is_dir() and not list(cwd.iterdir())
        assert spec['input_root'] == str(manifest_path.parent)
        assert spec['compute_device'] == 'cpu'
        assert 0 < timeout <= 30
        assert stderr == subprocess.STDOUT
        stdout.write('retained worker log\n')
        calls.append(directory)
        if len(calls) == 1:
            return 7
        if len(calls) == 2:
            raise subprocess.TimeoutExpired(argv, timeout)
        for name in spec['task']['outputs']:
            (directory / name).write_bytes(b'produced')
        return 0
    monkeypatch.setattr(batch, 'run_process', run)
    report = batch.run_batch(manifest_path, output)
    assert report['state'] == 'PARTIAL'
    assert [o['state'] for o in report['outcomes']] == ['FAILED', 'TIMED_OUT', 'SUCCEEDED']
    assert report['outcomes'][0]['exit_code'] == 7
    assert len(set(calls)) == 3
    saved = json.loads((output / 'report.json').read_text())
    assert saved['outcomes'] == report['outcomes']
    unpacked = output.parent / 'unpacked'
    batch.unpack_results(output, unpacked)
    assert (unpacked / 'first/worker.log').read_text() == 'retained worker log\n'
    assert json.loads((unpacked / 'second/status.json').read_text())['state'] == 'TIMED_OUT'


def test_deadline_preserves_completed_outcome_and_starts_no_new_work(local_batch, monkeypatch):
    manifest_path, output = local_batch
    clock = [0.]
    monkeypatch.setattr(batch, 'monotonic', lambda: clock[0])
    calls = []
    def run(argv, **kwargs):
        if argv[4] == 'bundle':
            assert kwargs['timeout'] == 5
            batch.bundle_results(output, json.loads((output / 'state.json').read_text()))
            return 0
        calls.append(argv)
        clock[0] = 15
        return 0
    monkeypatch.setattr(batch, 'run_process', run)
    report = batch.run_batch(manifest_path, output, timeout=20)
    assert len(calls) == 1
    assert [o['state'] for o in report['outcomes']] == ['SUCCEEDED', 'NOT_STARTED', 'NOT_STARTED']
    assert all(o['reason'] == 'BATCH_TIME_OR_CU_BUDGET' for o in report['outcomes'][1:])
    assert not (output / 'second').exists()
    assert not (output / 'third').exists()
    assert report['state'] == 'PARTIAL'


def test_probe_consuming_deadline_prevents_worker_launch(local_batch, monkeypatch):
    manifest_path, output = local_batch
    clock = [0.]
    monkeypatch.setattr(batch, 'monotonic', lambda: clock[0])
    def probe(**kwargs):
        clock[0] = 15
        return {}
    monkeypatch.setattr(selection, 'probe_resources', probe)
    commands = []
    def run(argv, **kwargs):
        commands.append(argv)
        assert argv[4] == 'bundle'
        assert kwargs['timeout'] == 5
        batch.bundle_results(output, json.loads((output / 'state.json').read_text()))
        return 0
    monkeypatch.setattr(batch, 'run_process', run)
    report = batch.run_batch(manifest_path, output, timeout=20)
    assert len(commands) == 1
    assert all(o['state'] == 'NOT_STARTED' for o in report['outcomes'])
    assert not (output / 'first/worker.json').exists()


@pytest.mark.parametrize('status', ['COMPLETED', 'PARTIAL', 'FAILED'])
def test_benchmark_caps_configuration_and_retains_partial_report(tmp_path, manifest, monkeypatch, status):
    from factorlab.experiments import mc_colab_benchmark as benchmark
    task = dict(manifest['tasks'][0], entry='benchmark',
                parameters={'benchmark': tasks.BENCHMARKS[0]},
                memory_budget_mb=384, gpu_memory_limit_mb=256, timeout_seconds=12)
    resources = tmp_path / 'resources'
    resources.mkdir()
    (resources / (tasks.BENCHMARKS[0] + '.json')).write_text(json.dumps(dict(
        cells=[dict(gpu_memory_limit_mb=128), dict(gpu_memory_limit_mb=1024)])))
    monkeypatch.setattr(batch, 'files', lambda package: tmp_path)
    seen = []
    def run_study(config, output, **kwargs):
        seen.append(config)
        assert config['host_memory_budget_mb'] == 384
        assert config['budget_seconds'] == 12
        assert [cell['gpu_memory_limit_mb'] for cell in config['cells']] == [128, 256]
        return dict(status=status, outcomes=[dict(state='TIMED_OUT')])
    monkeypatch.setattr(benchmark, 'run_study', run_study)
    if status == 'COMPLETED':
        batch.execute_task(task, 'cpu', tmp_path, tmp_path, manifest['commit'])
    else:
        with pytest.raises(RuntimeError, match='incomplete'):
            batch.execute_task(task, 'cpu', tmp_path, tmp_path, manifest['commit'])
    assert len(seen) == 1
    report = json.loads((tmp_path / 'report.json').read_text())
    assert report['status'] == status
    assert report['outcomes'] == [dict(state='TIMED_OUT')]


def test_worker_verifies_declared_outputs(tmp_path, manifest, monkeypatch):
    output = tmp_path / 'output'
    output.mkdir()
    spec = tmp_path / 'worker.json'
    spec.write_text(json.dumps(dict(output=str(output), commit=manifest['commit'],
        compute_device='cpu', input_root=str(tmp_path), task=manifest['tasks'][0])))
    monkeypatch.setattr(batch, 'installed_commit', lambda commit: {})
    monkeypatch.setattr(batch, 'environment', lambda device: {'device': device})
    monkeypatch.setattr(batch, 'execute_task', lambda *args: (output / 'report.json').write_text('{}'))
    with pytest.raises(RuntimeError, match='samples.npz'):
        batch.worker(spec)
    assert json.loads((output / 'environment.json').read_text()) == {'device': 'cpu'}
