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



@pytest.mark.parametrize('path', [
    'tools/data/audit_p3_d05.py', 'tools/data/summarize_p3_d05_notices.py',
    'tests/evidence/test_p3_d05.py', 'docs/validation/p3-d05/REPORT.md',
    'docs/validation/p3-d05/real.json', 'docs/validation/p3-d05/targeted-tests.xml',
])
def test_research_additions_do_not_trigger_full_matrix(path):
    result = classify([path], changes=[('A', path)])
    assert (result['current'], result['locking'], result['package'], result['review']) == (
        False, False, False, False)


def test_p3_d05_mixed_additions_are_not_a_ci_control_change():
    paths = ['tools/data/audit_p3_d05.py', 'tools/data/summarize_p3_d05_notices.py',
             'tests/evidence/test_p3_d05.py', 'docs/validation/p3-d05/REPORT.md',
             'docs/validation/p3-d05/real.json']
    result = classify(paths, changes=[('A', p) for p in paths])
    assert not any(result[k] for k in ('current', 'locking', 'package', 'review'))


@pytest.mark.parametrize('path', [
    'tools/ci/pr_risk.py', 'tools/ci/pr_targeted.py', 'tools/security/fixed_validator.py',
    'tests/security/test_pr_risk.py', '.github/workflows/factorlab-pr.yml',
    'requirements/dev.txt', 'tests/evidence/conftest.py',
    'tools/data/__init__.py', 'tools/data/subdir/audit.py',
    'tools/data/candidate_preflight.py', 'tests/evidence/test_pit_archive_verifiers.py',
    'docs/validation/p3-d05/run.py', 'docs/validation/p3-d05/logic.yml',
])
def test_disguised_control_or_unclassified_changes_remain_high_risk(path):
    result = classify([path], changes=[('A', path)])
    assert result['current'] and result['locking']


@pytest.mark.parametrize('change', [
    ('D', 'tools/data/audit_p3_d05.py'),
    ('D', 'docs/validation/p3-d05/real.json'),
    ('T', 'tests/evidence/test_p3_d05.py'),
    ('A', 'src/factorlab/models/new_model.py'),
])
def test_deletions_type_changes_and_application_additions_are_extended(change):
    result = classify([change[1]], changes=[change])
    assert result['current'] and result['locking']


def test_executable_and_symlink_modes_fail_closed():
    result = classify(['tools/data/audit_p3_d05.py'],
                      changes=[('A', 'tools/data/audit_p3_d05.py')], unsafe_modes=True)
    assert all(result[k] for k in ('current', 'locking', 'package', 'review'))


def test_mixed_research_and_sensitive_changes_cannot_downgrade():
    result = classify(['tests/evidence/test_p3_d05.py', 'src/factorlab/data/admission.py'],
                      changes=[('A', 'tests/evidence/test_p3_d05.py'),
                               ('M', 'src/factorlab/data/admission.py')])
    assert result['current'] and result['locking']


def test_fast_research_changes_require_targeted_execution():
    import yaml
    from pathlib import Path

    workflow = yaml.load((Path(__file__).resolve().parents[2] /
        '.github/workflows/factorlab-pr.yml').read_text(), Loader=yaml.BaseLoader)
    fast = workflow['jobs']['fast']
    risk = workflow['jobs']['risk']
    assert 'risk' in fast['needs']
    assert 'targeted' in risk['outputs']
    assert any('tools/ci/pr_targeted.py' in step.get('run', '')
               for step in fast['steps'])
    assert 'fast' in workflow['jobs']['merge-gate']['needs']


def test_targeted_research_paths_are_checked_without_running_diagnostic_scripts(tmp_path, monkeypatch):
    import base64
    import json
    from tools.ci import pr_targeted

    monkeypatch.setattr(pr_targeted, 'ROOT', tmp_path)
    script = tmp_path / 'tools/data/audit_p3_d05.py'
    script.parent.mkdir(parents=True)
    script.write_text('raise RuntimeError("diagnostic code must not execute")\n')
    test = tmp_path / 'tests/evidence/test_p3_d05.py'
    test.parent.mkdir(parents=True)
    test.write_text('def test_example(): assert 1 + 1 == 2\n')
    paths = ['tests/evidence/test_p3_d05.py', 'tools/data/audit_p3_d05.py']
    encoded = base64.b64encode(json.dumps(paths).encode()).decode()
    assert pr_targeted.decode_paths(encoded) == paths
    # The standalone script is only parsed; its top-level RuntimeError must not execute.
    assert pr_targeted.run([paths[1]], tmp_path / 'output') == 0
    receipt = json.loads((tmp_path / 'output/receipt.json').read_text())
    assert receipt['status'] == 'PASS' and receipt['syntax_checked'] == [paths[1]]
    for malicious in ('../escape.py', 'tests/evidence/conftest.py',
                      'tools/data/nested/audit.py'):
        encoded = base64.b64encode(json.dumps([malicious]).encode()).decode()
        with pytest.raises(ValueError):
            pr_targeted.decode_paths(encoded)


def test_git_risk_selector_records_additions_and_targeted_tests(tmp_path):
    """Exercise git's actual -z name/status and new-blob-mode handling."""
    import base64
    import json
    from pathlib import Path
    import subprocess
    import sys

    repo = tmp_path / 'candidate'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.name', 'CI Test'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.email', 'ci@example.invalid'], cwd=repo, check=True)
    (repo / 'src/factorlab').mkdir(parents=True)
    (repo / 'src/factorlab/__init__.py').write_text('# baseline\n')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'baseline'], cwd=repo, check=True)
    baseline = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    for path, value in {
        'tools/data/audit_p3_d05.py': 'def audit(): return 1\n',
        'tests/evidence/test_p3_d05.py': 'def test_audit(): assert True\n',
        'docs/validation/p3-d05/REPORT.md': '# Report\n',
    }.items():
        source = repo / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(value)
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'research addition'], cwd=repo, check=True)
    output = tmp_path / 'risk.json'
    gh_output = tmp_path / 'github-output.txt'
    selector = Path(__file__).resolve().parents[2] / 'tools/ci/pr_risk.py'
    subprocess.run([sys.executable, str(selector), '--repository', str(repo),
                    '--base', baseline, '--head', 'HEAD', '--output', str(output),
                    '--github-output', str(gh_output)], check=True)
    result = json.loads(output.read_text())
    assert not any(result[k] for k in ('current', 'locking', 'package', 'review'))
    assert result['targeted_paths'] == [
        'tests/evidence/test_p3_d05.py', 'tools/data/audit_p3_d05.py']
    encoded = dict(line.split('=', 1) for line in gh_output.read_text().splitlines())
    assert json.loads(base64.b64decode(encoded['targeted'])) == result['targeted_paths']
    assert set(encoded) == {'current', 'locking', 'package', 'review', 'targeted'}
    research_head = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                            cwd=repo, text=True).strip()
    (repo / 'tools/data/audit_p3_d05.py').unlink()
    subprocess.run(['git', 'add', '-u'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'delete diagnostic'], cwd=repo, check=True)
    subprocess.run([sys.executable, str(selector), '--repository', str(repo),
                    '--base', research_head, '--head', 'HEAD',
                    '--output', str(output), '--github-output', str(gh_output)],
                   check=True)
    deleted = json.loads(output.read_text())
    assert deleted['current'] and deleted['locking']
    assert deleted['targeted_paths'] == []
