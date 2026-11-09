"""Conservative selection and mandatory extension failure propagation."""
import pytest
from tools.ci.pr_risk import FLAGS, affected_consumers, classify, git, select


@pytest.mark.parametrize('path', ['docs/validation/new/REPORT.md',
    'docs/validation/new/result.json', 'docs/validation/new/result.csv'])
def test_inert_report_additions_remain_fast(tmp_path, path):
    base = init_repo(tmp_path)
    write(tmp_path, path, '{}')
    result = select(tmp_path, base, commit(tmp_path))
    assert not any(result[key] for key in FLAGS)


def write(root, path, text):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding='utf-8')


def commit(root):
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-qm', 'fixture')
    return git(root, 'rev-parse', 'HEAD').decode().strip()


def init_repo(root):
    git(root, 'init', '-q')
    write(root, 'README.md', 'fixture')
    return commit(root)


@pytest.mark.parametrize('consumer,source,high', [
    ('tests/evidence/test_audit.py', 'import audit\ndef test_audit(): assert audit.VALUE == 1\n', False),
    ('src/factorlab/data/admission.py', 'from tools.data import audit\n', True),
    ('.github/workflows/audit.yml', 'run: python tools/data/audit.py\n', True),
    ('config/pipeline.yaml', 'entry: tools/data/audit.py\n', True),
])
def test_new_research_tool_requires_tests_and_no_sensitive_consumers(tmp_path, consumer, source, high):
    base = init_repo(tmp_path)
    write(tmp_path, 'tools/data/audit.py', 'VALUE = 1\n')
    write(tmp_path, 'tests/evidence/test_audit.py', 'import audit\ndef test_audit(): assert audit.VALUE == 1\n')
    write(tmp_path, consumer, source)
    result = select(tmp_path, base, commit(tmp_path))
    assert result['current'] is high
    assert 'tests/evidence/test_audit.py' in result['targeted_tests']


@pytest.mark.parametrize('mode', ['dynamic', 'untested', 'executable', 'deleted', 'symlink'])
def test_unproven_or_structural_changes_escalate(tmp_path, mode):
    init_repo(tmp_path)
    write(tmp_path, 'tools/data/audit.py', 'VALUE = 1\n')
    write(tmp_path, 'tests/evidence/test_audit.py', 'import audit\ndef test_audit(): pass\n')
    base = commit(tmp_path)
    if mode == 'deleted':
        (tmp_path / 'tools/data/audit.py').unlink()
    elif mode == 'executable':
        git(tmp_path, 'update-index', '--chmod=+x', 'tools/data/audit.py')
        git(tmp_path, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'mode')
    elif mode == 'symlink':
        oid = git(tmp_path, 'rev-parse', 'HEAD:README.md').decode().strip()
        git(tmp_path, 'update-index', '--add', '--cacheinfo', '120000,' + oid + ',tools/data/link.py')
        git(tmp_path, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'link')
    elif mode == 'dynamic':
        write(tmp_path, 'tools/data/audit.py', 'import importlib\nimportlib.import_module(input())\n')
    else:
        write(tmp_path, 'tools/data/unknown.py', 'VALUE = 1\n')
    head = 'HEAD' if mode in ('executable', 'symlink') else commit(tmp_path)
    result = select(tmp_path, base, head)
    assert result['current'] and result['locking']


def test_removed_base_consumer_and_rename_do_not_hide_risk(tmp_path):
    init_repo(tmp_path)
    write(tmp_path, 'src/factorlab/factors/a.py', 'VALUE = 1\n')
    write(tmp_path, 'src/factorlab/data/admission.py', 'from factorlab.factors.a import VALUE\n')
    base = commit(tmp_path)
    write(tmp_path, 'src/factorlab/data/admission.py', '')
    (tmp_path / 'src/factorlab/factors/a.py').rename(tmp_path / 'src/factorlab/factors/b.py')
    result = select(tmp_path, base, commit(tmp_path))
    assert result['current'] and result['locking']


@pytest.mark.parametrize('reference', [
    'import importlib\nimportlib.import_module(input())\n',
    'PATH = "tools/data/*.py"\n',
])
def test_unresolved_formal_loader_and_scoped_glob_escalate(tmp_path, reference):
    init_repo(tmp_path)
    write(tmp_path, 'src/factorlab/cli.py', reference)
    base = commit(tmp_path)
    write(tmp_path, 'tools/data/audit.py', 'VALUE = 1\n')
    write(tmp_path, 'tests/evidence/test_audit.py', 'import audit\ndef test_audit(): pass\n')
    assert select(tmp_path, base, commit(tmp_path))['current']


def test_plain_file_inventory_is_not_an_executable_consumer(tmp_path):
    init_repo(tmp_path)
    write(tmp_path, 'docs/validation/source-list.json', '["tests/evidence/verify.py"]')
    write(tmp_path, 'tools/ci/verify.py', 'INPUT = "docs/validation/source-list.json"\n')
    write(tmp_path, 'tests/evidence/verify.py', 'INPUT = "targeted-tests.xml"\n')
    base = commit(tmp_path)
    write(tmp_path, 'docs/validation/new/targeted-tests.xml', '<testsuites/>')
    write(tmp_path, 'tools/data/audit.py', 'VALUE = 1\n')
    write(tmp_path, 'tests/evidence/test_audit.py', 'import audit\ndef test_audit(): pass\n')
    result = select(tmp_path, base, commit(tmp_path))
    assert not any(result[k] for k in FLAGS)


def test_modified_factor_selects_actual_test_consumer(tmp_path):
    init_repo(tmp_path)
    write(tmp_path, 'src/factorlab/factors/momentum.py', 'VALUE = 1\n')
    write(tmp_path, 'tests/research/test_momentum.py', 'from factorlab.factors.momentum import VALUE\n')
    base = commit(tmp_path)
    write(tmp_path, 'src/factorlab/factors/momentum.py', 'VALUE = 2\n')
    result = select(tmp_path, base, commit(tmp_path))
    assert not result['current']
    assert result['targeted_tests'] == ['tests/research/test_momentum.py']


@pytest.mark.parametrize('module,extended', [('momentum', False), ('registry', True)])
def test_reviewed_demo_loader_does_not_taint_unrelated_factor(tmp_path, module, extended):
    init_repo(tmp_path)
    write(tmp_path, 'src/factorlab/demo.py', 'import importlib\nimportlib.import_module(namespace + "." + name)\n')
    source = 'src/factorlab/factors/' + module + '.py'
    write(tmp_path, source, 'VALUE = 1\n')
    write(tmp_path, 'tests/research/test_factor.py', 'from factorlab.factors.' + module + ' import VALUE\n')
    base = commit(tmp_path)
    write(tmp_path, source, 'VALUE = 2\n')
    assert select(tmp_path, base, commit(tmp_path))['current'] is extended


def test_test_plugin_registration_is_control_plane(tmp_path):
    base = init_repo(tmp_path)
    write(tmp_path, 'tests/evidence/test_audit.py', 'pytest_plugins = ["malicious"]\n')
    result = select(tmp_path, base, commit(tmp_path))
    assert all(result[k] for k in FLAGS)


@pytest.mark.parametrize('path', ['docs/validation/x:stream.md', 'tools/data/../audit.py',
                                  'tests/evidence/test_bad.py::test_one'])
def test_noncanonical_paths_cannot_lower_risk(path):
    assert classify([path])['current']


def test_dependency_changes_do_not_request_control_approval():
    result = classify(['requirements/dev.txt'])
    assert result['current'] and result['package']
    assert not result['review']


def test_trade_execution_requires_locks():
    result = classify(['src/factorlab/backtest/execution.py'])
    assert result['current'] and result['locking']


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
    'tests/security/test_independent_controls.py',
    'pyproject.toml', 'pytest.ini', 'conftest.py', 'somewhere/conftest.py',
])
def test_control_changes_cannot_approve_themselves(path):
    result = classify([path])
    assert result['current'] and result['package'] and result['review']


def test_safe_documentation_does_not_mask_sensitive_change():
    assert classify(['docs/user/guide.md', 'src/factorlab/data/admission.py'])['current']


def test_covered_core_still_runs_mandatory_incremental_suites():
    result = classify(['src/factorlab/factors/momentum.py'])
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


def test_fast_consumes_trusted_plan_and_runner():
    from pathlib import Path
    import yaml
    workflow = yaml.load((Path(__file__).resolve().parents[2] /
        '.github/workflows/factorlab-pr.yml').read_text(), Loader=yaml.BaseLoader)
    fast = workflow['jobs']['fast']
    assert fast['needs'] == 'risk'
    assert any(step.get('with', {}).get('name') == 'pr-risk' and
               step.get('uses', '').startswith('actions/download-artifact@') for step in fast['steps'])
    targeted = next(step['run'] for step in fast['steps'] if '--suite targeted' in step.get('run', ''))
    assert 'python -I trusted/tools/ci/integration_checks.py' in targeted
    assert '--risk-plan trusted-risk/risk.json' in targeted
    assert any('execution.json' in step.get('with', {}).get('path', '') for step in fast['steps'])


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
    assert any('--suite historical' in s.get('run', '') for s in full['jobs']['legacy']['steps'])
    assert any('--historical-only' in s.get('run', '') for s in full['jobs']['validation-v2-review']['steps'])
    assert full['jobs']['incremental']['strategy']['matrix'] == {
        'python': ['3.14', '3.13'], 'shard': ['0', '1', '2']}
    assert full['jobs']['incremental']['strategy']['fail-fast'] == 'false'
    assert 'current-coverage' in full['jobs']['release-gate']['needs']
    assert full['jobs']['current-coverage']['if'] == 'always()'
    assert full['jobs']['current-coverage']['needs'] == 'incremental'
    assert any('test "$RESULT" = success' in s.get('run', '')
               for s in full['jobs']['current-coverage']['steps'])


def test_full_release_gate_refuses_every_non_success(monkeypatch):
    from pathlib import Path
    import json
    import yaml
    full = yaml.load((Path(__file__).resolve().parents[2] /
        '.github/workflows/factorlab-full.yml').read_text(), Loader=yaml.BaseLoader)
    gate = full['jobs']['release-gate']
    results = {job: {'result': 'success'} for job in gate['needs']}
    for job in results:
        for status in ('failure', 'skipped', 'cancelled'):
            attack = dict(results, **{job: {'result': status}})
            monkeypatch.setenv('RESULTS', json.dumps(attack))
            with pytest.raises(SystemExit) as exc:
                exec(compile(gate['steps'][0]['run'], '<full-release-gate>', 'exec'), {})
            assert exc.value.code

