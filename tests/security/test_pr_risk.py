"""Conservative selection and mandatory extension failure propagation."""
import pytest
from tools.ci.pr_risk import affected_consumers, classify


@pytest.mark.parametrize('path', [
    'src/factorlab/data/adjustment/engine.py',
    'src/factorlab/data/admission.py',
    'src/factorlab/universe/event_evidence.py',
    'src/factorlab/forward/_windows_lock.py',
    'src/factorlab/unknown/new_module.py',
    'unclassified-input.json',
])
def test_unknown_or_sensitive_changes_require_full_software_and_locks(path):
    result = classify([path])
    assert result['current'] and result['locking']


@pytest.mark.parametrize('path', [
    '.github/workflows/factorlab-pr.yml', 'tools/ci/pr_risk.py',
    'tests/security/test_independent_controls.py', 'requirements/dev.txt',
    'pyproject.toml', 'pytest.ini', 'conftest.py', 'somewhere/conftest.py',
])
def test_control_changes_cannot_approve_themselves(path):
    result = classify([path])
    assert result['current'] and result['package'] and result['review']


def test_safe_documentation_does_not_mask_sensitive_change():
    assert classify(['docs/user/guide.md', 'src/factorlab/data/admission.py'])['current']


def test_covered_core_still_runs_mandatory_incremental_suites():
    result = classify(['src/factorlab/backtest/execution.py'])
    assert not result['current']
    assert not result['review']


def test_cross_module_consumers_force_conservative_extension(tmp_path):
    root = tmp_path / 'src/factorlab'
    (root / 'models').mkdir(parents=True)
    (root / 'data').mkdir()
    (root / 'models/base.py').write_text('class Model: pass\n')
    (root / 'data/admission.py').write_text('from ..models.base import Model\n')
    affected = affected_consumers(tmp_path, ['src/factorlab/models/base.py'])
    assert 'src/factorlab/data/admission.py' in affected
    assert classify(affected)['current']


def test_merge_gate_rejects_failures_and_missing_extensions(monkeypatch):
    import json
    from pathlib import Path
    import yaml

    workflow = yaml.load((Path(__file__).resolve().parents[2] /
        '.github/workflows/factorlab-pr.yml').read_text(), Loader=yaml.BaseLoader)
    script = next(step['run'] for step in workflow['jobs']['merge-gate']['steps'] if 'run' in step)
    from unittest.mock import MagicMock
    import urllib.request
    response = MagicMock()
    monkeypatch.setattr(urllib.request, 'urlopen', response)
    for name, value in dict(GITHUB_API_URL='https://api.github.com',
        GITHUB_SERVER_URL='https://github.com', GITHUB_REPOSITORY='owner/repo',
        GITHUB_RUN_ID='123', STATUS_SHA='a' * 40, GH_TOKEN='test').items():
        monkeypatch.setenv(name, value)
    results = {name: {'result': 'success'} for name in (
        'risk', 'fast', 'control-review', 'current', 'locking', 'package')}
    results['risk']['outputs'] = dict(current='true', locking='true', package='true', review='true')
    for job in results:
        for status in ('failure', 'skipped', 'cancelled'):
            attack = json.loads(json.dumps(results))
            attack[job]['result'] = status
            monkeypatch.setenv('RESULTS', json.dumps(attack))
            with pytest.raises(SystemExit) as exc:
                exec(compile(script, '<merge-gate>', 'exec'), {})
            assert exc.value.code
    monkeypatch.setenv('RESULTS', json.dumps(results))
    with pytest.raises(SystemExit) as exc:
        exec(compile(script, '<merge-gate>', 'exec'), {})
    assert not exc.value.code


def test_full_retains_real_legacy_failure_and_release_block():
    from pathlib import Path
    import yaml

    root = Path(__file__).resolve().parents[2]
    full = yaml.load((root / '.github/workflows/factorlab-full.yml').read_text(), Loader=yaml.BaseLoader)
    assert {'workflow_dispatch', 'workflow_call', 'schedule', 'push'} <= set(full['on'])
    assert 'legacy' in full['jobs']['release-gate']['needs']
    for job in full['jobs'].values():
        assert 'continue-on-error' not in job
        for step in job.get('steps', []):
            assert 'continue-on-error' not in step
    assert any('--suite legacy' in s.get('run', '') for s in full['jobs']['legacy']['steps'])
    assert any('validation_v2.py' in s.get('run', '') for s in full['jobs']['validation-v2-review']['steps'])
