"""Offline mixed-placement dispatch and controller bridge tests."""

import json
from pathlib import Path
import subprocess

import pytest

from factorlab.experiments import research_dispatch as dispatch


@pytest.fixture
def submission(tmp_path, monkeypatch):
    task = dict(id='local', entry='bootstrap', parameters={}, inputs={'returns': 'data/returns.npz'},
                device='cpu', memory_budget_mb=512, gpu_memory_limit_mb=None,
                timeout_seconds=30, outputs=['report.json', 'samples.npz'])
    manifest = dict(schema_version=1, commit='a' * 40, timeout_seconds=1800, budget_cu=2,
                    tasks=[task, dict(task, id='remote', device='colab')])
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/returns.npz').write_bytes(b'declared')
    (tmp_path / 'private.json').write_text('not declared')
    source = tmp_path / 'manifest.json'
    source.write_text(json.dumps(manifest))
    monkeypatch.setattr(dispatch, 'probe_resources', lambda **kw: {})
    monkeypatch.setattr(dispatch, 'admission_task', lambda task, root: task)
    monkeypatch.setattr(dispatch, 'select_task', lambda task, resources, cloud:
                        dict(device=task['device']))
    return source, tmp_path / 'result'


def test_mixed_dispatch_one_local_one_cloud_preserves_declared_inputs(submission, monkeypatch):
    source, output = submission
    local, remote = [], []
    def run_local(path, target, **kwargs):
        document = json.loads(path.read_text())
        local.append(document)
        assert [t['id'] for t in document['tasks']] == ['local']
        assert (path.parent / 'data/returns.npz').read_bytes() == b'declared'
        assert not (path.parent / 'private.json').exists()
        return dict(state='SUCCEEDED', outcomes=[dict(id='local', state='SUCCEEDED')])
    def run_remote(command, **kwargs):
        remote.append(command)
        assert command.count('submit') == 1 and '--wait' in command
        controller = command[command.index('submit') - 1].replace('\\', '/')
        assert controller.endswith('/resources/colab_job.py')
        assert '--manifest' in command
        assert kwargs['check'] is True and 270 < kwargs['timeout'] <= 1950
        manifest = json.loads((output / 'cloud-input/manifest.json').read_text())
        assert manifest['commit'] == 'a' * 40 and manifest['budget_cu'] == 2
        assert [t['device'] for t in manifest['tasks']] == ['colab']
        assert (output / 'cloud-input/data/returns.npz').read_bytes() == b'declared'
        assert not (output / 'cloud-input/private.json').exists()
    monkeypatch.setattr(dispatch, 'run_batch', run_local)
    monkeypatch.setattr(dispatch.subprocess, 'run', run_remote)
    report = dispatch.dispatch(source, output, wheel=source.parent / 'factorlab.whl', wsl_distro='Ubuntu')
    assert report['state'] == 'SUCCEEDED'
    assert len(local) == len(remote) == 1
    assert json.loads((output / 'state.json').read_text()) == report


def test_cloud_requires_wheel_before_any_execution(submission, monkeypatch):
    source, output = submission
    monkeypatch.setattr(dispatch, 'run_batch', lambda *a, **kw: pytest.fail('local started'))
    with pytest.raises(ValueError, match='wheel'):
        dispatch.dispatch(source, output)


def test_windows_requires_explicit_distro(submission, monkeypatch):
    from types import SimpleNamespace
    source, output = submission
    monkeypatch.setattr(dispatch, 'os', SimpleNamespace(name='nt'))
    with pytest.raises(ValueError, match='explicit WSL'):
        dispatch.dispatch(source, output, wheel=source)


def test_cloud_failure_retains_local_outcomes(submission, monkeypatch):
    source, output = submission
    retained = dict(state='PARTIAL', outcomes=[dict(id='local', state='TIMED_OUT')])
    monkeypatch.setattr(dispatch, 'run_batch', lambda *a, **kw: retained)
    def fail(*a, **kw):
        raise subprocess.CalledProcessError(1, a[0])
    monkeypatch.setattr(dispatch.subprocess, 'run', fail)
    report = dispatch.dispatch(source, output, wheel=source, wsl_distro='Ubuntu')
    assert report['state'] == 'PARTIAL'
    assert report['local'] == retained
    assert report['cloud']['state'] == 'FAILED'


def test_no_cloud_start_when_local_uses_remaining_budget(submission, monkeypatch):
    source, output = submission
    clock = [0.]
    monkeypatch.setattr(dispatch, 'monotonic', lambda: clock[0])
    def local(*a, **kw):
        clock[0] = 1750
        return dict(state='SUCCEEDED')
    monkeypatch.setattr(dispatch, 'run_batch', local)
    monkeypatch.setattr(dispatch.subprocess, 'run', lambda *a, **kw: pytest.fail('cloud allocated'))
    report = dispatch.dispatch(source, output, wheel=source, wsl_distro='Ubuntu')
    assert report['state'] == 'PARTIAL'
    assert report['cloud'] == dict(state='NOT_STARTED', reason='DISPATCH_TIME_BUDGET')
    assert not (output / 'cloud-input').exists()


def test_local_only_needs_no_wheel(submission, monkeypatch):
    source, output = submission
    document = json.loads(source.read_text())
    document['tasks'].pop()
    source.write_text(json.dumps(document))
    monkeypatch.setattr(dispatch, 'run_batch', lambda *a, **kw: dict(state='SUCCEEDED'))
    monkeypatch.setattr(dispatch.subprocess, 'run', lambda *a, **kw: pytest.fail('cloud allocated'))
    assert dispatch.dispatch(source, output)['state'] == 'SUCCEEDED'


def test_auto_cloud_requires_authorized_metadata(submission, monkeypatch):
    source, output = submission
    document = json.loads(source.read_text())
    document['tasks'] = [dict(document['tasks'][0], device='auto')]
    source.write_text(json.dumps(document))
    monkeypatch.setattr(dispatch, 'select_task', lambda *a: dict(device='colab'))
    with pytest.raises(ValueError, match='authorization'):
        dispatch.dispatch(source, output, wheel=source)
    assert json.loads((output / 'state.json').read_text())['state'] == 'FAILED'


def test_cloud_selection_uses_manifest_budget(submission, monkeypatch):
    source, output = submission
    document = json.loads(source.read_text())
    document['tasks'] = [dict(document['tasks'][0], device='auto')]
    source.write_text(json.dumps(document))
    def select(task, resources, cloud):
        assert cloud['budget_cu'] == 2
        return dict(device='cpu')
    monkeypatch.setattr(dispatch, 'select_task', select)
    monkeypatch.setattr(dispatch, 'run_batch', lambda *a, **kw: dict(state='SUCCEEDED'))
    dispatch.dispatch(source, output, cloud={'authorized': True, 'budget_cu': 999})


@pytest.mark.parametrize('source,expected', [
    ('D:/Code/project/input.json', '/mnt/d/Code/project/input.json'),
    ('C:\\Users\\person\\space dir\\wheel.whl', '/mnt/c/Users/person/space dir/wheel.whl')])
def test_wsl_absolute_drive_mapping(source, expected):
    assert dispatch.wsl_path(source) == expected


@pytest.mark.parametrize('path', ['relative/path', '//server/share/file'])
def test_wsl_refuses_non_drive_paths(path):
    with pytest.raises(ValueError):
        dispatch.wsl_path(path)


def test_windows_bridge_uses_exec_argv_not_shell(submission, monkeypatch):
    source, output = submission
    monkeypatch.setattr(dispatch, 'run_batch', lambda *a, **kw: dict(state='SUCCEEDED'))
    # Patch the module's os reference, not process-global os.name used by pathlib.
    from types import SimpleNamespace
    monkeypatch.setattr(dispatch, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(dispatch, 'wsl_path', lambda path: '/mapped/' + Path(path).name)
    commands = []
    monkeypatch.setattr(dispatch.subprocess, 'run', lambda command, **kw: commands.append(command))
    dispatch.dispatch(source, output, wheel=source, wsl_distro='Chosen distro',
                      controller_python='/opt/venv/bin/python', colab_executable='/opt/bin/colab')
    command = commands[0]
    assert command[:6] == ['wsl.exe', '--distribution', 'Chosen distro', '--exec',
                           '/opt/venv/bin/python', '/mapped/colab_job.py']
    assert command[command.index('--colab') + 1] == '/opt/bin/colab'
    assert not any(arg in ('sh', 'bash', '-c') for arg in command)
