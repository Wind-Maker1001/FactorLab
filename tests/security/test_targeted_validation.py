"""Risk-plan binding and actual pytest execution evidence."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

TRUSTED = Path(__file__).resolve().parents[2] / 'tools/ci'
spec = importlib.util.spec_from_file_location('trusted_targeted_validation', TRUSTED / 'targeted_validation.py')
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'candidate'
    (root / 'tests').mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n', encoding='utf-8')
    (root / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
    (root / 'tests/test_one.py').write_text('def test_ok():\n    assert True\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    'commit', '-qm', 'fixture'], check=True)
    return root


def plan(repository, targets=None):
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository, text=True).strip()
    return dict(schema='FACTORLAB_PR_RISK_V2', head=head,
                targeted_tests=targets if targets is not None else ['tests/test_one.py'])


@pytest.mark.parametrize('target', ['../test_bad.py', 'tests/../test_bad.py',
    'tests\\test_one.py', 'tests/test_one.py::test_ok', '/tests/test_one.py',
    '-q', 'tests//test_one.py', 'tests/./test_one.py', 'tests/test_missing.py'])
def test_reject_target_paths(repository, target):
    with pytest.raises(ValueError):
        validation.validate_plan(repository, plan(repository, [target]))


def test_reject_tracked_symlink_mode(repository):
    blob = subprocess.check_output(['git', 'hash-object', 'tests/test_one.py'],
                                   cwd=repository, text=True).strip()
    subprocess.run(['git', 'update-index', '--cacheinfo', f'120000,{blob},tests/test_one.py'],
                   cwd=repository, check=True)
    with pytest.raises(ValueError, match='tracked regular'):
        validation.validate_plan(repository, plan(repository))


def test_reject_nonregular_worktree_target(repository):
    target = repository / 'tests/test_one.py'
    target.unlink()
    target.mkdir()
    with pytest.raises(ValueError, match='regular file'):
        validation.validate_plan(repository, plan(repository))


def test_head_and_schema_binding(repository):
    assert validation.validate_plan(repository, plan(repository)) == ['tests/test_one.py']
    for key, value in [('head', '0' * 40), ('schema', 'FACTORLAB_PR_RISK_V1')]:
        invalid = plan(repository)
        invalid[key] = value
        with pytest.raises(ValueError):
            validation.validate_plan(repository, invalid)


@pytest.mark.parametrize(('source', 'extra', 'passed'), [
    ('def test_ok():\n    assert True\n', [], True),
    ('def test_fail():\n    assert False\n', [], False),
    ('VALUE = 1\n', [], False),
    ('import pytest\n@pytest.mark.skip\ndef test_skip():\n    pass\n', [], False),
    ('def test_ok():\n    pass\ndef test_other():\n    pass\n', ['-k', 'test_ok'], False),
    ('import pytest\n@pytest.fixture\ndef unavailable():\n    pytest.skip("no setup")\ndef test_skip(unavailable):\n    pass\n', [], False),
])
def test_real_pytest_evidence(repository, tmp_path, source, extra, passed):
    (repository / 'tests/test_one.py').write_text(source, encoding='utf-8')
    evidence = tmp_path / 'execution.json'
    subprocess.run([sys.executable, '-I', str(TRUSTED / 'targeted_validation.py'),
                    '--execution-inventory', str(evidence), 'tests/test_one.py', '-q', *extra],
                   cwd=repository, capture_output=True, text=True)
    assert validation.evidence_passed(evidence, ['tests/test_one.py']) is passed
    if passed:
        assert not validation.evidence_passed(evidence, ['tests/test_one.py', 'tests/test_missing.py'])


@pytest.mark.parametrize('empty', [False, True])
def test_runner_uses_trusted_observer(repository, tmp_path, empty):
    planted = repository / 'tools/ci'
    planted.mkdir(parents=True)
    (planted / 'targeted_validation.py').write_text('raise RuntimeError("candidate observer")\n', encoding='utf-8')
    risk = tmp_path / 'risk.json'
    risk.write_text(json.dumps(plan(repository, [] if empty else None)), encoding='utf-8')
    output = tmp_path / 'result'
    result = subprocess.run([sys.executable, '-I', str(TRUSTED / 'integration_checks.py'),
                             '--repository', str(repository), '--risk-plan', str(risk),
                             '--suite', 'targeted', '--output', str(output)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((output / 'targeted/receipt.json').read_text(encoding='utf-8'))
    assert receipt['status'] == 'PASS'
    assert receipt['subject_sha'] == plan(repository)['head']
