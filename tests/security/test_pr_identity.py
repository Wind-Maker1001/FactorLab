"""Event-bound immutable PR subjects, exercised through trusted CLI execution."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[2] / 'tools/ci'
spec = importlib.util.spec_from_file_location('pr_identity', TOOLS / 'pr_identity.py')
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def commit(root, message):
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', message)
    return git(root, 'rev-parse', 'HEAD')


@pytest.fixture
def subject(tmp_path):
    root = tmp_path / 'candidate'
    root.mkdir()
    git(root, 'init', '-q')
    (root / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n', encoding='utf-8')
    (root / 'base').write_text('base', encoding='utf-8')
    base = commit(root, 'base')
    (root / 'tests').mkdir()
    (root / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
    (root / 'tests/test_one.py').write_text(
        'from pathlib import Path\ndef test_ok():\n    Path("../executed").write_text("yes")\n', encoding='utf-8')
    head = commit(root, 'pr')
    tree = git(root, 'rev-parse', 'HEAD^{tree}')
    process_env = {key: value for key, value in os.environ.items() if key.upper() in {
        'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'HOME', 'USERPROFILE',
        'APPDATA', 'LOCALAPPDATA', 'COMSPEC', 'PATHEXT', 'PYTHONUTF8', 'PYTHONIOENCODING'}}
    env = {**process_env, 'GIT_AUTHOR_NAME': 'Test', 'GIT_COMMITTER_NAME': 'Test',
           'GIT_AUTHOR_EMAIL': 'test@example.com', 'GIT_COMMITTER_EMAIL': 'test@example.com'}
    merge = subprocess.check_output(['git', '-C', str(root), 'commit-tree', tree,
                                     '-p', base, '-p', head, '-m', 'merge'], env=env, text=True).strip()
    git(root, 'checkout', '-q', '--detach', merge)
    event = dict(number=44, pull_request=dict(base=dict(sha=base), head=dict(sha=head)))
    environ = {**env, 'GITHUB_EVENT_NAME': 'pull_request_target', 'GITHUB_REPOSITORY': 'test/FactorLab',
               'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': base}
    event_path = tmp_path / 'event.json'
    event_path.write_text(json.dumps(event), encoding='utf-8')
    environ['GITHUB_EVENT_PATH'] = str(event_path)
    expected = identity.event_identity(event, environ)
    plan = dict(schema='FACTORLAB_PR_RISK_V2', base=base, head=merge, targeted_tests=['tests/test_one.py'])
    risk = tmp_path / 'risk.json'
    risk.write_text(json.dumps(plan), encoding='utf-8')
    receipt = identity.issue(root, expected)
    receipt['risk_sha256'] = hashlib.sha256(risk.read_bytes()).hexdigest()
    signed = tmp_path / 'identity.json'
    signed.write_text(json.dumps(receipt), encoding='utf-8')
    environ.update(VALIDATION_SUBJECT=merge, RISK_SHA256=receipt['risk_sha256'])
    return root, expected, risk, signed, environ


def run_verified(subject, tmp_path):
    root, _, risk, signed, env = subject
    result = subprocess.run([sys.executable, '-I', str(TOOLS / 'pr_identity.py'), 'verify',
                             '--repository', str(root), '--identity', str(signed), '--risk-plan', str(risk)],
                            env=env, capture_output=True, text=True)
    if result.returncode == 0:
        result = subprocess.run([sys.executable, '-I', str(TOOLS / 'integration_checks.py'),
                                 '--repository', str(root), '--risk-plan', str(risk), '--suite', 'targeted',
                                 '--output', str(tmp_path / 'result')], env=env, capture_output=True, text=True)
    return result


def test_matching_identity_executes(subject, tmp_path):
    result = run_verified(subject, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (subject[0].parent / 'executed').read_text() == 'yes'


@pytest.mark.parametrize('change', ['schema', 'risk_head', 'checkout', 'pr_head', 'base', 'run',
                                   'attempt', 'repository', 'event_sha', 'untracked', 'symlink_mode', 'symlink'])
def test_mismatched_identity_never_executes(subject, tmp_path, change):
    root, expected, risk, signed, env = subject
    if change in ('schema', 'risk_head'):
        plan = json.loads(risk.read_text())
        plan['schema' if change == 'schema' else 'head'] = 'invalid'
        risk.write_text(json.dumps(plan), encoding='utf-8')
        # Even a newly sealed digest cannot excuse an invalid schema/subject.
        receipt = json.loads(signed.read_text())
        receipt['risk_sha256'] = env['RISK_SHA256'] = hashlib.sha256(risk.read_bytes()).hexdigest()
        signed.write_text(json.dumps(receipt), encoding='utf-8')
    elif change == 'checkout':
        git(root, 'checkout', '-q', '--detach', expected['pr_head'])
    elif change in ('pr_head', 'base'):
        event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text())
        event['pull_request']['head' if change == 'pr_head' else 'base']['sha'] = 'a' * 40
        Path(env['GITHUB_EVENT_PATH']).write_text(json.dumps(event), encoding='utf-8')
    elif change in ('run', 'attempt', 'repository', 'event_sha'):
        key = {'run': 'GITHUB_RUN_ID', 'attempt': 'GITHUB_RUN_ATTEMPT',
               'repository': 'GITHUB_REPOSITORY', 'event_sha': 'GITHUB_SHA'}[change]
        env[key] += '-other'
    elif change == 'untracked':
        git(root, 'rm', '--cached', 'tests/test_one.py')
    elif change == 'symlink_mode':
        blob = git(root, 'hash-object', 'tests/test_one.py')
        git(root, 'update-index', '--cacheinfo', f'120000,{blob},tests/test_one.py')
    else:
        target = root / 'tests/test_one.py'
        source = root / 'source.py'
        target.rename(source)
        target.symlink_to(source)
    result = run_verified(subject, tmp_path)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not (root.parent / 'executed').exists()


def test_plan_tampering_rejected(subject, tmp_path):
    root, _, risk, _, _ = subject
    risk.write_bytes(risk.read_bytes() + b' ')
    assert run_verified(subject, tmp_path).returncode != 0
    assert not (root.parent / 'executed').exists()


@pytest.mark.parametrize('field', ['base', 'pr_head'])
def test_issuer_rejects_stale_merge_parents(subject, field):
    root, expected, _, _, _ = subject
    with pytest.raises(ValueError, match='merge parents'):
        identity.issue(root, {**expected, field: 'a' * 40})


def test_mutable_merge_ref_cannot_switch_pinned_subject(subject, tmp_path):
    root, expected, _, _, env = subject
    git(root, 'update-ref', 'refs/pull/44/merge', env['VALIDATION_SUBJECT'])
    pinned = git(root, 'rev-parse', 'refs/pull/44/merge')
    git(root, 'update-ref', 'refs/pull/44/merge', expected['pr_head'])
    git(root, 'checkout', '-q', '--detach', pinned)
    result = run_verified(subject, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(root, 'rev-parse', 'HEAD') == pinned
    assert git(root, 'rev-parse', 'refs/pull/44/merge') != pinned
    assert (root.parent / 'executed').read_text() == 'yes'


def test_workflow_pins_all_candidate_jobs_before_execution():
    import yaml
    workflow = yaml.safe_load((TOOLS.parents[1] / '.github/workflows/factorlab-pr.yml').read_text())
    jobs = workflow['jobs']
    assert jobs['risk']['outputs']['subject'] == '${{ steps.identity.outputs.subject }}'
    assert jobs['risk']['outputs']['risk_sha256'] == '${{ steps.seal.outputs.risk_sha256 }}'
    risk_steps = jobs['risk']['steps']
    positions = {step.get('id'): index for index, step in enumerate(risk_steps)}
    assert positions['identity'] < positions['select'] < positions['seal']
    for name in ('fast', 'current', 'locking', 'package'):
        steps = jobs[name]['steps']
        assert steps[0]['with']['ref'] == '${{ needs.risk.outputs.subject }}'
        verification = next(i for i, step in enumerate(steps)
                            if 'pr_identity.py verify' in step.get('run', ''))
        installs = [i for i, step in enumerate(steps) if 'pip install' in step.get('run', '')]
        assert installs and verification < min(installs)
        assert steps[verification]['env']['VALIDATION_SUBJECT'] == '${{ needs.risk.outputs.subject }}'
        assert steps[verification]['env']['RISK_SHA256'] == '${{ needs.risk.outputs.risk_sha256 }}'
        downloads = [step['with'] for step in steps[:verification]
                     if step.get('uses', '').startswith('actions/download-artifact@')]
        assert downloads == [dict(name='pr-risk', path='trusted-risk')]
        assert steps[1]['with']['ref'] == '${{ github.event.pull_request.base.sha || github.sha }}'
    targeted = next(step for step in jobs['fast']['steps'] if step.get('name') == 'Trusted targeted test execution')
    assert targeted['if'] == '${{ success() }}'
    assert 'pr_identity.py verify' in targeted['run']


def test_issue_and_seal_cli(subject, tmp_path):
    root, _, risk, signed, env = subject
    env['GITHUB_OUTPUT'] = str(tmp_path / 'outputs')
    for action in ('issue', 'seal'):
        result = subprocess.run([sys.executable, '-I', str(TOOLS / 'pr_identity.py'), action,
                                 '--repository', str(root), '--identity', str(signed), '--risk-plan', str(risk)],
                                env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    assert f"subject={env['VALIDATION_SUBJECT']}" in Path(env['GITHUB_OUTPUT']).read_text()
    assert run_verified(subject, tmp_path).returncode == 0
    assert (root.parent / 'executed').exists()
