"""Risk-plan binding and actual pytest execution evidence."""
import csv
import importlib.util
import shutil
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
    ('import pytest\n@pytest.mark.xfail\ndef test_xpass():\n    pass\n', [], False),
    ('import pytest\n@pytest.fixture\ndef broken():\n    raise ValueError("setup")\ndef test_error(broken):\n    pass\n', [], False),
    ('import pytest\n@pytest.fixture\ndef broken():\n    yield\n    raise ValueError("teardown")\ndef test_error(broken):\n    pass\n', [], False),
    ('raise ValueError("collection")\n', [], False),
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


@pytest.fixture
def historical_controller(tmp_path):
    trusted = tmp_path / 'trusted'
    scripts = trusted / 'tools/ci'
    scripts.mkdir(parents=True)
    for name in ('targeted_validation.py', 'validation_layers.py', 'integration_checks.py'):
        shutil.copyfile(TRUSTED / name, scripts / name)
    matrix = trusted / 'docs/ci/CI_BASELINE_FAILURE_MATRIX.csv'
    matrix.parent.mkdir(parents=True)
    with (TRUSTED.parents[1] / 'docs/ci/CI_BASELINE_FAILURE_MATRIX.csv').open(
            encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    rows[0]['test_id'] = 'tests/test_one.py::test_historical'
    with matrix.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(rows)
    return scripts


@pytest.mark.parametrize(('source', 'extra', 'passed'), [
    ('def test_historical():\n    raise RuntimeError("historic")\ndef test_current():\n    pass\n', [], True),
    ('def test_historical():\n    raise RuntimeError("historic")\n', [], True),
    ('def test_current():\n    pass\n', [], False),
    ('def test_historical():\n    pass\ndef test_new_failure():\n    assert False\n', [], False),
    ('def test_historical():\n    pass\ndef test_current():\n    pass\n', ['-k', 'historical'], False),
    ('raise ValueError("new collection failure")\n', [], False),
])
def test_strict_historical_subset(repository, tmp_path, historical_controller, source, extra, passed):
    (repository / 'tests/test_one.py').write_text(source, encoding='utf-8')
    planted = repository / 'docs/ci/CI_BASELINE_FAILURE_MATRIX.csv'
    planted.parent.mkdir(parents=True)
    planted.write_text('candidate-controlled matrix must never be read', encoding='utf-8')
    evidence = tmp_path / 'historical-execution.json'
    subprocess.run([sys.executable, '-I', str(historical_controller / 'targeted_validation.py'),
                    '--execution-inventory', str(evidence), '--target-file', 'tests/test_one.py',
                    'tests/test_one.py', '-q', *extra], cwd=repository, capture_output=True, text=True)
    spec = importlib.util.spec_from_file_location('isolated_trusted', historical_controller / 'targeted_validation.py')
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    assert controller.evidence_passed(evidence, ['tests/test_one.py']) is passed
    observed = json.loads(evidence.read_text(encoding='utf-8'))
    assert observed['historical_status'] == 'BLOCKED'
    if passed:
        assert 'tests/test_one.py::test_historical' not in observed['reports']
        for key, value in [('deselected', []), ('historical_matrix_sha256', 'forged'),
                           ('reports', {}), ('historical_status', 'PASS')]:
            forged = dict(observed, **{key: value})
            evidence.write_text(json.dumps(forged), encoding='utf-8')
            if key != 'reports' or observed['reports']:
                assert not controller.evidence_passed(evidence, ['tests/test_one.py'])


@pytest.mark.parametrize('changed', [False, True])
def test_collection_blocker_requires_unchanged_trusted_file(repository, tmp_path, historical_controller, changed):
    trusted = historical_controller.parents[1]
    matrix = trusted / 'docs/ci/CI_BASELINE_FAILURE_MATRIX.csv'
    matrix.write_text(matrix.read_text(encoding='utf-8').replace(
        'tests/test_one.py::test_historical', 'tests/test_one.py'), encoding='utf-8')
    source = 'raise RuntimeError("historical collection blocker")\n'
    (trusted / 'tests').mkdir()
    (trusted / 'tests/test_one.py').write_text(source, encoding='utf-8')
    (repository / 'tests/test_one.py').write_text(
        source + ('def test_new():\n    assert False\n' if changed else ''), encoding='utf-8')
    evidence = tmp_path / 'execution.json'
    result = subprocess.run([sys.executable, '-I', str(historical_controller / 'targeted_validation.py'),
                             '--execution-inventory', str(evidence), '--target-file', 'tests/test_one.py',
                             'tests/test_one.py', '-q'], cwd=repository, capture_output=True, text=True)
    assert (result.returncode == 0) is not changed, result.stdout + result.stderr
    if not changed:
        observed = json.loads(evidence.read_text(encoding='utf-8'))
        assert observed['historical_collection_errors'] == ['tests/test_one.py']
        assert observed['historical_status'] == 'BLOCKED'
        assert observed['reports'] == {}


def test_runner_rejects_failure_after_observer_finish(repository, tmp_path, historical_controller):
    (repository / 'tests/test_one.py').write_text(
        'def test_historical():\n    assert False\ndef test_current():\n    pass\n', encoding='utf-8')
    (repository / 'conftest.py').write_text(
        'def pytest_unconfigure(config):\n    raise RuntimeError("late plugin failure")\n', encoding='utf-8')
    risk = tmp_path / 'risk.json'
    risk.write_text(json.dumps(plan(repository)), encoding='utf-8')
    output = tmp_path / 'result'
    result = subprocess.run([sys.executable, '-I', str(historical_controller / 'integration_checks.py'),
                             '--repository', str(repository), '--risk-plan', str(risk),
                             '--suite', 'targeted', '--output', str(output)], capture_output=True, text=True)
    assert result.returncode != 0, result.stdout + result.stderr
    receipt = json.loads((output / 'targeted/receipt.json').read_text(encoding='utf-8'))
    execution = json.loads((output / 'targeted/execution.json').read_text(encoding='utf-8'))
    assert execution['exit_code'] == 0
    assert receipt['exit_code'] != 0
    assert receipt['status'] == 'FAIL'


@pytest.mark.parametrize('failure', ['import', 'missing', 'json', 'schema', 'unreadable', 'identity'])
def test_runner_preserves_receipt_when_evidence_unavailable(
        repository, tmp_path, historical_controller, failure):
    output = tmp_path / 'result'
    evidence = output / 'targeted/execution.json'
    (repository / 'tests/test_one.py').write_text(
        'def test_historical():\n    pass\ndef test_current():\n    pass\n', encoding='utf-8')
    if failure == 'identity':
        trusted = historical_controller.parents[1]
        matrix = trusted / 'docs/ci/CI_BASELINE_FAILURE_MATRIX.csv'
        matrix.write_text(matrix.read_text(encoding='utf-8').replace(
            'tests/test_one.py::test_historical', 'tests/test_one.py'), encoding='utf-8')
        (trusted / 'tests').mkdir()
        (trusted / 'tests/test_one.py').write_text('different trusted bytes', encoding='utf-8')
    elif failure == 'import':
        (repository / 'conftest.py').write_text('raise RuntimeError("early import failure")\n', encoding='utf-8')
    else:
        action = {
            'missing': 'p.unlink()',
            'json': 'p.write_text("{", encoding="utf-8")',
            'schema': 'p.write_text("{}", encoding="utf-8")',
            'unreadable': 'p.unlink(); p.mkdir()',
        }[failure]
        (repository / 'conftest.py').write_text(
            'from pathlib import Path\ndef pytest_unconfigure(config):\n'
            f'    p = Path({str(evidence)!r})\n    {action}\n', encoding='utf-8')
    risk = tmp_path / 'risk.json'
    risk.write_text(json.dumps(plan(repository)), encoding='utf-8')
    result = subprocess.run([sys.executable, '-I', str(historical_controller / 'integration_checks.py'),
                             '--repository', str(repository), '--risk-plan', str(risk),
                             '--suite', 'targeted', '--output', str(output)], capture_output=True, text=True)
    assert result.returncode != 0, result.stdout + result.stderr
    receipt = json.loads((output / 'targeted/receipt.json').read_text(encoding='utf-8'))
    assert receipt['status'] == 'FAIL'
    assert receipt['exit_code'] == ({'import': 4, 'identity': 1}.get(failure, 0))
    assert receipt['execution_evidence_status'] == 'UNAVAILABLE'
    assert receipt['execution_evidence_error']['type']
    assert receipt['required_execution_observed'] is False
    assert receipt['historical_status'] == 'NOT_VERIFIED'
    assert receipt['historical_blocked'] is None
    assert receipt['historical_matrix_sha256'] is None


def test_runner_records_history_without_claiming_historical_pass(repository, tmp_path, historical_controller):
    (repository / 'tests/test_one.py').write_text(
        'def test_historical():\n    assert False\ndef test_current():\n    pass\n', encoding='utf-8')
    risk = tmp_path / 'risk.json'
    risk.write_text(json.dumps(plan(repository)), encoding='utf-8')
    output = tmp_path / 'result'
    result = subprocess.run([sys.executable, '-I', str(historical_controller / 'integration_checks.py'),
                             '--repository', str(repository), '--risk-plan', str(risk),
                             '--suite', 'targeted', '--output', str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((output / 'targeted/receipt.json').read_text(encoding='utf-8'))
    assert receipt['status'] == 'PASS'
    assert receipt['historical_status'] == 'BLOCKED'
    assert receipt['historical_blocked'] == ['tests/test_one.py::test_historical']
