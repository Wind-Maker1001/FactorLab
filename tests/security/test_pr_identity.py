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
    refresh = next(i for i, step in enumerate(risk_steps)
                   if 'pr_merge_ref.py' in step.get('run', ''))
    assert risk_steps[refresh]['run'] == 'python -I trusted/tools/ci/pr_merge_ref.py --repository candidate'
    assert 1 < refresh < positions['identity'] < positions['select'] < positions['seal']
    for name in ('fast', 'current', 'locking', 'package'):
        steps = jobs[name]['steps']
        assert steps[0]['with']['ref'] == '${{ github.event.pull_request.base.sha || github.sha }}'
        assert steps[0]['with']['path'] == 'candidate'
        assert jobs[name]['defaults']['run']['working-directory'] == 'candidate'
        reconstruction = next(i for i, step in enumerate(steps)
                              if 'pr_merge_ref.py' in step.get('run', ''))
        assert '--expected-subject "$VALIDATION_SUBJECT"' in steps[reconstruction]['run']
        assert steps[reconstruction]['shell'] == 'bash'
        assert steps[reconstruction]['env']['VALIDATION_SUBJECT'] == '${{ needs.risk.outputs.subject }}'
        verification = next(i for i, step in enumerate(steps)
                            if 'pr_identity.py verify' in step.get('run', ''))
        installs = [i for i, step in enumerate(steps) if 'pip install' in step.get('run', '')]
        assert installs and reconstruction < verification < min(installs)
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


refresh_spec = importlib.util.spec_from_file_location('pr_merge_ref', TOOLS / 'pr_merge_ref.py')
merge_ref = importlib.util.module_from_spec(refresh_spec)
refresh_spec.loader.exec_module(merge_ref)


@pytest.mark.parametrize('stale', [None, 'base', 'pr_head'])
def test_event_merge_ignores_published_ref_and_rebuilds(subject, tmp_path, stale):
    root, expected, _, _, env = subject
    remote = tmp_path / 'remote.git'
    git(tmp_path, 'clone', '--bare', str(root), str(remote))
    git(root, 'remote', 'add', 'origin', str(remote))
    if stale:
        git(remote, 'update-ref', 'refs/pull/44/merge', expected[stale])
    event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text())
    sha = merge_ref.reconstruct(root, event)
    assert git(root, 'show', '-s', '--format=%P', sha).split() == [expected['base'], expected['pr_head']]
    assert git(root, 'rev-parse', sha + '^{tree}') == git(root, 'rev-parse', expected['pr_head'] + '^{tree}')
    consumer = tmp_path / 'consumer'
    git(tmp_path, 'clone', str(remote), str(consumer))
    # Simulate concurrent branch and merge-ref movement after the issuer.
    git(remote, 'update-ref', 'refs/heads/main', expected['pr_head'])
    git(remote, 'update-ref', 'refs/pull/44/head', expected['base'])
    git(remote, 'update-ref', 'refs/pull/44/merge', expected['base'])
    git(consumer, 'config', 'merge.renames', 'false')
    git(consumer, 'config', 'user.name', 'Different user')
    assert merge_ref.reconstruct(consumer, event, expected_subject=sha) == sha
    assert identity.issue(consumer, expected)['subject'] == sha
    assert not (root.parent / 'executed').exists()


@pytest.mark.parametrize('failure', ['missing', 'subject', 'sha', 'timeout'])
def test_reconstruction_rejects_invalid_input(subject, tmp_path, failure):
    root, expected, _, _, env = subject
    git(root, 'remote', 'add', 'origin', str(root))
    original = git(root, 'rev-parse', 'HEAD')
    event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text())
    kwargs = {}
    if failure == 'missing':
        event['pull_request']['head']['sha'] = 'a' * 40
    elif failure == 'subject':
        kwargs['expected_subject'] = 'a' * 40
    elif failure == 'sha':
        event['pull_request']['head']['sha'] = '--upload-pack=malicious'
    else:
        kwargs['timeout'] = 0
    with pytest.raises((ValueError, TimeoutError, subprocess.CalledProcessError)):
        merge_ref.reconstruct(root, event, **kwargs)
    assert git(root, 'rev-parse', 'HEAD') == original
    assert not (root.parent / 'executed').exists()


def test_real_merge_conflict_rejected(subject, tmp_path, monkeypatch):
    root, expected, _, _, env = subject
    git(root, 'checkout', '--detach', expected['base'])
    (root / 'base').write_text('left', encoding='utf-8')
    (root / '.gitattributes').write_text('base merge=hostile\n', encoding='utf-8')
    base = commit(root, 'left')
    git(root, 'checkout', '--detach', expected['pr_head'])
    (root / 'base').write_text('right', encoding='utf-8')
    head = commit(root, 'right')
    marker = tmp_path / 'driver-executed'
    command = f'touch "{marker.as_posix()}"; exit 0'
    git(root, 'config', 'merge.hostile.driver', command)
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'merge.hostile.driver')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', command)
    git(root, 'remote', 'add', 'origin', str(root))
    event = dict(number=44, pull_request=dict(base=dict(sha=base), head=dict(sha=head)))
    with pytest.raises(subprocess.CalledProcessError):
        merge_ref.reconstruct(root, event)
    assert git(root, 'rev-parse', 'HEAD') == head
    assert not marker.exists()
    assert not (root.parent / 'executed').exists()


def test_divergent_merge_and_untrusted_driver_never_executes(subject, tmp_path, monkeypatch):
    root, expected, _, _, env = subject
    git(root, 'checkout', '--detach', expected['base'])
    (root / 'left').write_text('base addition', encoding='utf-8')
    (root / '.gitattributes').write_text('base merge=hostile\n', encoding='utf-8')
    base = commit(root, 'divergent base')
    git(root, 'checkout', '--detach', expected['pr_head'])
    (root / 'right').write_text('PR addition', encoding='utf-8')
    head = commit(root, 'divergent PR')
    git(root, 'remote', 'add', 'origin', str(root))
    marker = tmp_path / 'driver-executed'
    command = f'touch "{marker.as_posix()}"; exit 1'
    git(root, 'config', 'merge.hostile.driver', command)
    hooks = tmp_path / 'hooks'
    hooks.mkdir()
    hook = hooks / 'post-checkout'
    hook.write_text('#!/bin/sh\n' + command + '\n', encoding='utf-8')
    hook.chmod(0o755)
    git(root, 'config', 'core.hooksPath', str(hooks))
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'merge.hostile.driver')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', command)
    event = dict(number=44, pull_request=dict(base=dict(sha=base), head=dict(sha=head)))
    sha = merge_ref.reconstruct(root, event)
    assert (root / 'left').read_text() == 'base addition'
    assert (root / 'right').read_text() == 'PR addition'
    assert not marker.exists()
    assert git(root, 'show', '-s', '--format=%P', sha).split() == [base, head]


def test_rebuilt_subject_is_verified_before_execution(subject, tmp_path):
    root, expected, risk, signed, env = subject
    git(root, 'remote', 'add', 'origin', str(root))
    event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text())
    sha = merge_ref.reconstruct(root, event)
    plan = json.loads(risk.read_text())
    plan['head'] = sha
    risk.write_text(json.dumps(plan), encoding='utf-8')
    receipt = identity.issue(root, expected)
    receipt['risk_sha256'] = hashlib.sha256(risk.read_bytes()).hexdigest()
    signed.write_text(json.dumps(receipt), encoding='utf-8')
    env.update(VALIDATION_SUBJECT=sha, RISK_SHA256=receipt['risk_sha256'])
    result = run_verified(subject, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root.parent / 'executed').exists()


def test_refresh_non_pr_preserves_identity(subject, tmp_path):
    root, _, _, _, env = subject
    head = git(root, 'rev-parse', 'HEAD')
    event_path = tmp_path / 'dispatch.json'
    event_path.write_text('{}', encoding='utf-8')
    env.update(GITHUB_EVENT_PATH=str(event_path), GITHUB_EVENT_NAME='workflow_dispatch',
               GITHUB_SHA=head, GITHUB_OUTPUT=str(tmp_path / 'dispatch-output'))
    for command in ([str(TOOLS / 'pr_merge_ref.py'), '--repository', str(root)],
                    [str(TOOLS / 'pr_identity.py'), 'issue', '--repository', str(root),
                     '--identity', str(tmp_path / 'dispatch-identity.json')]):
        result = subprocess.run([sys.executable, '-I', *command], env=env,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / 'dispatch-identity.json').read_text())['subject'] == head


def test_refresh_non_pr_does_not_run_git(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('non-PR refresh must not run Git')
    monkeypatch.setattr(merge_ref.subprocess, 'check_output', forbidden)
    assert merge_ref.reconstruct('unused', {}) is None
