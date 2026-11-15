"""Base-blob shard policy and version-specific execution evidence attacks."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from tools.ci import pr_identity, sharding
from tests.security.test_validation_v2 import shard_count, shard_receipts, pr_shard_receipts


def measured(version, weights):
    return dict(source_run='https://example.test/runs/123', source_artifact='extended-' + version,
                subject_sha='measured-subject', python=version + '.0', test_count=12,
                module_seconds=weights)


def weight_bytes():
    modules = [f'tests/{letter}.py' for letter in 'abcdef']
    return json.dumps(dict(schema='FACTORLAB_MEASURED_MODULE_TIMINGS_V2', versions={
        version: measured(version, dict(zip(modules, seconds)))
        for version, seconds in [('3.13', [90, 70, 50, 30, 20, 10]),
                                 ('3.14', [10, 20, 30, 50, 70, 90])]})).encode()


@pytest.fixture
def trusted_shards(pr_shard_receipts, shard_count, monkeypatch):
    root, output, identity = pr_shard_receipts
    issued = dict(schema=pr_identity.SCHEMA, repository='test/FactorLab', event='pull_request_target',
                  pr=44, base=identity['trusted_base_sha'], pr_head=identity['github_pr_head_sha'],
                  subject=identity['subject_sha'], event_sha=identity['github_event_sha'],
                  run_id=identity['github_run_id'], run_attempt=identity['github_run_attempt'])
    spec = sharding.build_spec(weight_bytes(), issued, shard_count)
    nodes = [f'tests/{letter}.py::test_{i}' for letter in 'abcdef' for i in range(2)]
    for version in sharding.VERSIONS:
        weights = spec['weights']['versions'][version]['module_seconds']
        for index, selected in enumerate(sharding.partition(nodes, weights, shard_count)):
            folder = root / version / str(index) / 'current'
            bound = sharding.binding(spec, version, nodes)
            inventory = dict(current=nodes, selected=selected, shard=index, shard_count=shard_count,
                             historical_collected=['old::test'], historical_collection_errors=['broken.py'],
                             module_plan=sharding.module_plan(nodes, weights), sharding=bound)
            execution = dict(started=selected, completed=selected, exit_code=0, collection_errors=[],
                             reports={node: dict(setup='passed', call='passed', teardown='passed')
                                      for node in selected}, report_errors=[], sharding=bound)
            receipt = json.loads((folder / 'receipt.json').read_bytes())
            receipt.update(sharding=bound, counts=dict(tests=len(selected), passed=len(selected),
                                                      failures=0, errors=0, skipped=0))
            for name, data in [('selection', inventory), ('execution', execution), ('receipt', receipt)]:
                (folder / (name + '.json')).write_text(json.dumps(data), encoding='utf-8')
            suite = ET.Element('testsuite')
            for node in selected:
                module, name = node.split('::')
                ET.SubElement(suite, 'testcase', classname=module[:-3].replace('/', '.'), name=name)
            ET.ElementTree(suite).write(folder / 'tests.xml')
    monkeypatch.setattr(sharding, 'TIMINGS', root / 'candidate-injected-weights.json')
    return root, output, identity, spec


def test_versions_use_distinct_base_weights(trusted_shards, shard_count):
    root, output, identity, spec = trusted_shards
    first = json.loads((root / '3.13/0/current/selection.json').read_bytes())['selected']
    second = json.loads((root / '3.14/0/current/selection.json').read_bytes())['selected']
    assert first != second
    sharding.aggregate_pr(root, output, identity, shard_count, spec)
    for version in sharding.VERSIONS:
        assert json.loads((output / version / 'receipt.json').read_bytes())['counts']['passed'] == 12


@pytest.mark.parametrize('field', ['plan_sha256', 'weights_sha256', 'weights_schema', 'python',
                                  'shard_count', 'inventory_sha256'])
@pytest.mark.parametrize('file', ['selection', 'execution', 'receipt'])
def test_binding_mutations_rejected(trusted_shards, shard_count, field, file):
    root, output, identity, spec = trusted_shards
    path = root / '3.14/1/current' / (file + '.json')
    value = json.loads(path.read_bytes())
    value['sharding'][field] = 'wrong'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='binding'):
        sharding.aggregate_pr(root, output, identity, shard_count, spec)


@pytest.mark.parametrize('attack', ['counts', 'missing_call', 'failed_call', 'duplicate_phase',
                                   'duplicate_started', 'duplicate_completed', 'junit_pass',
                                   'weights', 'nodes', 'extra_version', 'missing_shard', 'extra_shard'])
def test_strict_execution_attacks(trusted_shards, shard_count, attack):
    root, output, identity, spec = trusted_shards
    folder = root / '3.13/0/current'
    if attack in ('missing_shard', 'extra_shard', 'extra_version'):
        if attack == 'missing_shard':
            (root / '3.13/0').rename(root / '3.13/missing')
        else:
            (root / ('3.15' if attack == 'extra_version' else f'3.13/{shard_count}')).mkdir()
    elif attack == 'counts':
        path = folder / 'receipt.json'
        value = json.loads(path.read_bytes())
        value['counts']['passed'] += 1
        path.write_text(json.dumps(value))
    elif attack == 'junit_pass':
        (folder / 'tests.xml').write_text(
            '<testsuite><testcase classname="tests.forged" name="test_pass"/></testsuite>')
    elif attack in ('weights', 'nodes'):
        path = folder / 'selection.json'
        value = json.loads(path.read_bytes())
        if attack == 'weights':
            value['module_plan']['tests/a.py']['seconds'] = 1
        else:
            value['current'].append('tests/extra.py::test_omitted')
        path.write_text(json.dumps(value))
    else:
        path = folder / 'execution.json'
        value = json.loads(path.read_bytes())
        node = value['started'][0]
        if attack == 'missing_call':
            del value['reports'][node]['call']
        elif attack == 'failed_call':
            value['reports'][node]['call'] = 'failed'
        elif attack == 'duplicate_phase':
            value['report_errors'].append('duplicate call')
        else:
            value['started' if attack == 'duplicate_started' else 'completed'].append(node)
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        sharding.aggregate_pr(root, output, identity, shard_count, spec)


@pytest.mark.parametrize('attack', ['schema', 'version', 'negative', 'nan', 'infinity', 'boolean',
                                   'duplicate', 'provenance', 'runtime'])
def test_invalid_weight_documents_rejected(attack):
    value = json.loads(weight_bytes())
    table = value['versions']['3.14']
    if attack == 'schema':
        value['schema'] = 'unknown'
    elif attack == 'version':
        del value['versions']['3.14']
    elif attack == 'duplicate':
        raw = weight_bytes().replace(b'"test_count": 12', b'"test_count": 12, "test_count": 12')
        with pytest.raises(ValueError, match='duplicate'):
            sharding.load_weights(raw)
        return
    elif attack == 'provenance':
        del table['source_run']
    elif attack == 'runtime':
        table['python'] = '3.13.0'
    else:
        table['module_seconds']['tests/a.py'] = {
            'negative': -1, 'nan': float('nan'), 'infinity': float('inf'), 'boolean': True}[attack]
    with pytest.raises(ValueError):
        sharding.load_weights(json.dumps(value).encode())


@pytest.mark.parametrize('count', [3, 4])
def test_v1_partition_compatibility_and_unknown_cost(count):
    table = measured('3.13', {f'tests/{letter}.py': seconds
                              for letter, seconds in zip('abcdef', [90, 70, 50, 30, 20, 10])})
    raw = json.dumps(dict(schema='FACTORLAB_MEASURED_MODULE_TIMINGS_V1', **table)).encode()
    loaded = sharding.load_weights(raw)
    nodes = [f'tests/{letter}.py::test_one' for letter in 'abcdef']
    expected = (['a', 'be', 'cdf'] if count == 3 else ['a', 'b', 'cf', 'de'])
    for version in sharding.VERSIONS:
        weights = loaded['versions'][version]['module_seconds']
        assert [[node.split('/')[1][0] for node in part]
                for part in sharding.partition(nodes, weights, count)] == [list(part) for part in expected]
        assert sharding.partition(nodes, dict(reversed(list(weights.items()))), count) == (
            sharding.partition(nodes, weights, count))
        assert sharding.module_plan(['tests/new.py::test'], weights)['tests/new.py'] == {
            'seconds': 90, 'status': 'UNMEASURED'}
    assert sharding.module_plan(['tests/new.py::test'], {})['tests/new.py']['seconds'] == 60


def git(repository, *args):
    return subprocess.check_output(['git', '-C', str(repository), *args], text=True).strip()


@pytest.fixture
def weight_repository(tmp_path):
    root = tmp_path / 'repository'
    root.mkdir()
    git(root, 'init', '-q')
    path = root / sharding.WEIGHTS_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(weight_bytes())
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'weights')
    issued = dict(schema=pr_identity.SCHEMA, base=git(root, 'rev-parse', 'HEAD'), subject='subject',
                  pr_head='head', event_sha='event', run_id='123', run_attempt='2',
                  repository='test/FactorLab', event='pull_request_target', pr=44)
    return root, issued


def test_mutable_checkout_weights_cannot_override_base(weight_repository):
    root, issued = weight_repository
    raw = sharding.base_weights(root, issued['base'])
    plan = dict(sharding=sharding.build_spec(raw, issued, 3))
    (root / sharding.WEIGHTS_PATH).write_text('{"module_seconds": {"tests/a.py": 0}}')
    assert sharding.verify_spec(plan, root, issued, 3) == plan['sharding']
    injected = copy.deepcopy(plan)
    injected['sharding']['weights']['versions']['3.14']['module_seconds']['tests/a.py'] = 0
    with pytest.raises(ValueError, match='trusted base'):
        sharding.verify_spec(injected, root, issued, 3)


@pytest.mark.parametrize('field', ['schema', 'algorithm', 'shard_count', 'weights_schema',
                                  'weights_digest', 'source', 'base', 'pr_head', 'subject',
                                  'event_sha', 'run_id', 'run_attempt'])
def test_spec_independently_rebuilt_from_base(weight_repository, field):
    root, issued = weight_repository
    plan = dict(sharding=sharding.build_spec(sharding.base_weights(root, issued['base']), issued, 3))
    spec = plan['sharding']
    if field in ('schema', 'algorithm', 'shard_count'):
        spec[field] = 'wrong'
    elif field in ('weights_schema', 'weights_digest'):
        spec['weights']['schema' if field == 'weights_schema' else 'sha256'] = 'wrong'
    elif field == 'source':
        spec['weights']['versions']['3.14']['source_run'] = 'untrusted-run'
    else:
        spec['identity'] = {**issued, field: 'wrong'}
    with pytest.raises(ValueError, match='trusted base'):
        sharding.verify_spec(plan, root, issued, 3)


@pytest.mark.skipif(f'{sys.version_info.major}.{sys.version_info.minor}' not in sharding.VERSIONS,
                    reason='trusted execution requires Python 3.13 or 3.14')
@pytest.mark.parametrize('count', [3, 4])
def test_trusted_execution_ignores_candidate_tools(weight_repository, tmp_path, count):
    root, _ = weight_repository
    tools = Path(__file__).resolve().parents[2] / 'tools/ci'
    trusted = tmp_path / 'trusted'
    destination = trusted / 'tools/ci'
    destination.mkdir(parents=True)
    for name in ('current_execution', 'current_validation', 'validation_layers', 'sharding',
                 'validation_v2', 'pr_identity', 'integration_checks'):
        shutil.copyfile(tools / (name + '.py'), destination / (name + '.py'))
    matrix = trusted / 'docs/ci/CI_BASELINE_FAILURE_MATRIX.csv'
    matrix.parent.mkdir(parents=True)
    matrix.write_text('test_id\n' + ''.join(f'tests/historical_{i}.py\n' for i in range(186)))
    (root / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n')
    (root / 'pytest.ini').write_text('[pytest]\npython_files=*.py\ntestpaths=tests\n')
    (root / 'conftest.py').write_text(
        'import os\nfrom pathlib import Path\n'
        'Path(os.environ["FACTORLAB_TEST_RISK"]).write_text("candidate replaced mutable plan")\n')
    (root / 'tests').mkdir()
    (root / 'tools/__init__.py').write_text('')
    (root / 'tools/ci/__init__.py').write_text('')
    (root / 'tools/ci/sentinel.py').write_text('CANDIDATE = True\n')
    for name in ('current_validation', 'sharding'):
        (root / 'tools/ci' / (name + '.py')).write_text('raise RuntimeError("candidate control imported")\n')
    for letter in 'abcdef':
        (root / 'tests' / (letter + '.py')).write_text(
            'from tools.ci.sentinel import CANDIDATE\n'
            'def test_ok():\n    assert CANDIDATE\n')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'candidate')
    base = git(root, 'rev-parse', 'HEAD')
    (root / sharding.WEIGHTS_PATH).write_text('candidate weight injection')
    event = tmp_path / 'event.json'
    event.write_text('{}')
    risk = tmp_path / 'risk.json'
    risk.write_text(json.dumps(dict(schema='FACTORLAB_PR_RISK_V2', base=base, head=base)))
    signed = tmp_path / 'identity.json'
    env = dict(os.environ, GITHUB_REPOSITORY='test/FactorLab', GITHUB_EVENT_NAME='workflow_dispatch',
               GITHUB_EVENT_PATH=str(event), GITHUB_SHA=base, GITHUB_RUN_ID='123',
               GITHUB_RUN_ATTEMPT='2', GITHUB_OUTPUT=str(tmp_path / 'outputs'),
               FACTORLAB_PR_HEAD_SHA=base, FACTORLAB_TRUSTED_BASE_SHA=base,
               FACTORLAB_TEST_RISK=str(risk))
    for action in ('issue', 'seal'):
        command = [sys.executable, '-I', str(destination / 'pr_identity.py'), action,
                   '--repository', str(root), '--identity', str(signed), '--risk-plan', str(risk)]
        if action == 'seal':
            command += ['--shard-count', str(count)]
        result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    env.update(VALIDATION_SUBJECT=base, RISK_SHA256=hashlib.sha256(risk.read_bytes()).hexdigest())
    spec = json.loads(risk.read_bytes())['sharding']
    output = tmp_path / 'execution'
    result = subprocess.run(
        [sys.executable, '-I', str(destination / 'integration_checks.py'), '--repository', str(root),
         '--identity', str(signed), '--risk-plan', str(risk), '--suite', 'current',
         '--shard', '0', '--shard-count', str(count), '--output', str(output)],
        cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    selection = json.loads((output / 'current/selection.json').read_bytes())
    execution = json.loads((output / 'current/execution.json').read_bytes())
    receipt = json.loads((output / 'current/receipt.json').read_bytes())
    version = f'{sys.version_info.major}.{sys.version_info.minor}'
    assert risk.read_text() == 'candidate replaced mutable plan'
    expected = sharding.partition(selection['current'],
                                  spec['weights']['versions'][version]['module_seconds'], count)[0]
    assert selection['selected'] == execution['started'] == execution['completed'] == expected
    assert receipt['status'] == 'PASS'
    assert receipt['sharding'] == execution['sharding'] == selection['sharding']
    assert all(phases == dict(setup='passed', call='passed', teardown='passed')
               for phases in execution['reports'].values())
    assert receipt['sharding']['weights_sha256'] == hashlib.sha256(weight_bytes()).hexdigest()


@pytest.mark.parametrize('attack', [None, 'risk_digest', 'candidate_weights', 'legacy'])
def test_sealed_pr_aggregation_cli(trusted_shards, shard_count, weight_repository, tmp_path,
                                   monkeypatch, attack):
    root, output, _, spec = trusted_shards
    repository, base_identity = weight_repository
    spec['identity']['base'] = base_identity['base']
    issued = spec['identity']
    expected_receipt = sharding.receipt_identity(issued)
    for version in sharding.VERSIONS:
        for index in range(shard_count):
            folder = root / version / str(index) / 'current'
            inventory = json.loads((folder / 'selection.json').read_bytes())
            bound = sharding.binding(spec, version, inventory['current'])
            for file in ('selection', 'execution', 'receipt'):
                path = folder / (file + '.json')
                value = json.loads(path.read_bytes())
                value['sharding'] = bound
                if file == 'receipt':
                    value.update(expected_receipt)
                path.write_text(json.dumps(value))
    plan = dict(schema='FACTORLAB_PR_RISK_V2', base=issued['base'], head=issued['subject'], sharding=spec)
    if attack == 'candidate_weights':
        plan['sharding']['weights']['versions']['3.14']['module_seconds']['tests/a.py'] = 0
    elif attack == 'legacy':
        del plan['sharding']
    risk = tmp_path / 'sealed-risk.json'
    risk.write_text(json.dumps(plan))
    risk_digest = hashlib.sha256(risk.read_bytes()).hexdigest()
    signed = tmp_path / 'sealed-identity.json'
    signed.write_text(json.dumps(dict(issued, risk_sha256=risk_digest)))
    event = tmp_path / 'aggregate-event.json'
    event.write_text(json.dumps(dict(number=44, pull_request=dict(
        base=dict(sha=issued['base']), head=dict(sha=issued['pr_head'])))))
    manifest = tmp_path / 'artifacts.json'
    manifest.write_text(json.dumps([dict(artifacts=[
        dict(name=f'extended-{version}-shard-{index}', expired=False, workflow_run=dict(id=123))
        for version in sharding.VERSIONS for index in range(shard_count)])]))
    environ = dict(GITHUB_EVENT_PATH=str(event), GITHUB_REPOSITORY=issued['repository'],
                   GITHUB_EVENT_NAME=issued['event'], GITHUB_SHA=issued['event_sha'],
                   GITHUB_RUN_ID=issued['run_id'], GITHUB_RUN_ATTEMPT=issued['run_attempt'],
                   VALIDATION_SUBJECT=issued['subject'], RISK_SHA256=risk_digest)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    if attack == 'risk_digest':
        risk.write_bytes(risk.read_bytes() + b' ')
    arguments = ['sharding', '--pr-shards', str(root), '--output', str(output),
                 '--shard-count', str(shard_count), '--risk-plan', str(risk),
                 '--identity', str(signed), '--artifact-manifest', str(manifest)]
    for option, key in [('subject-sha', 'subject_sha'), ('pr-head-sha', 'github_pr_head_sha'),
                        ('trusted-base-sha', 'trusted_base_sha'), ('event-sha', 'github_event_sha'),
                        ('run-id', 'github_run_id'), ('run-attempt', 'github_run_attempt')]:
        arguments += ['--' + option, expected_receipt[key]]
    monkeypatch.setattr(sys, 'argv', arguments)
    monkeypatch.setattr(sharding, 'ROOT', repository)
    if attack is None:
        sharding.main()
        assert (output / '3.14/receipt.json').exists()
    else:
        with pytest.raises(ValueError):
            sharding.main()
        assert not output.exists()


@pytest.mark.parametrize('count', [3, 4])
@pytest.mark.parametrize('attack', [None, 'duplicate', 'missing', 'extra', 'run', 'expired'])
def test_artifact_namespace(count, attack):
    artifacts = [dict(name=f'extended-{version}-shard-{index}', expired=False,
                      workflow_run=dict(id=123))
                 for version in sharding.VERSIONS for index in range(count)]
    if attack == 'duplicate':
        artifacts.append(artifacts[0])
    elif attack == 'missing':
        artifacts.pop()
    elif attack == 'extra':
        artifacts.append(dict(artifacts[0], name='extended-3.15-shard-0'))
    elif attack == 'run':
        artifacts[0]['workflow_run']['id'] = 456
    elif attack == 'expired':
        artifacts[0]['expired'] = True
    pages = [dict(artifacts=artifacts[:2]), dict(artifacts=artifacts[2:])]
    if attack is None:
        sharding.verify_artifacts(pages, '123', count)
    else:
        with pytest.raises(ValueError):
            sharding.verify_artifacts(pages, '123', count)
