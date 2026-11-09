"""A software coverage checker must refuse lost tests and disguised historical failures."""
import copy
import pytest
import xml.etree.ElementTree as ET
import json
import subprocess
import sys
from types import SimpleNamespace
from tools.ci import sharding, validation_layers, current_validation
from tools.ci.validation_v2 import compare, comparison_pass, outcomes


@pytest.fixture
def evidence():
    nodes = ['software-%04d' % i for i in range(4157)]
    current = {n:'PASS' for n in nodes}
    history = {'archive':'ERROR'}
    return (dict(current_nodes=nodes,critical_nodes={'safety':[nodes[0]]}), history,
            dict(current, **history), current,
            dict(current=nodes,historical_collected=['archive'],historical_collection_errors=[]))


def test_complete_independent_comparison(evidence):
    assert comparison_pass(compare(*evidence))


@pytest.mark.parametrize('attack', ['lost_software','lost_safety','historical_pass','new_failure','outcome_drift','inventory_lie','lost_history'])
def test_coverage_regressions_are_refused(evidence, attack):
    baseline, history, full, current, inventory = copy.deepcopy(evidence)
    node = baseline['current_nodes'][0 if attack=='lost_safety' else 1]
    if attack in ('lost_software','lost_safety'):
        del current[node]
    elif attack == 'historical_pass':
        full['archive'] = 'PASS'
    elif attack == 'new_failure':
        current['new'] = full['new'] = 'FAIL'
        inventory['current'].append('new')
    elif attack == 'outcome_drift':
        full[node] = 'FAIL'
    elif attack == 'inventory_lie':
        inventory['current'].remove(node)
    else:
        inventory['historical_collected'] = []
    assert not comparison_pass(compare(baseline,history,full,current,inventory))


def test_duplicate_junit_node_is_refused(tmp_path):
    path=tmp_path/'duplicate.xml'
    path.write_text('<testsuite><testcase classname="same" name="node"/><testcase classname="same" name="node"/></testsuite>')
    with pytest.raises(ValueError,match='duplicate'):
        outcomes(path)


@pytest.mark.parametrize('suffix', [
    'test_function',
    'EvidenceTests::test_cash_price_is_not_extra_holder_income',
    'Outer::Inner::test_method[value.with.dots]',
    'Outer::test_method[value::with::separators]',
])
def test_junit_identity_matches_collection(tmp_path, suffix):
    node = 'tests/evidence/test_p3_d03.py::'+suffix
    qualified, bracket, parameter = suffix.partition('[')
    parts = qualified.split('::')
    root = ET.Element('testsuite')
    ET.SubElement(root, 'testcase',
                  classname='.'.join(['tests.evidence.test_p3_d03', *parts[:-1]]),
                  name=parts[-1] + bracket + parameter)
    path = tmp_path/'tests.xml'
    ET.ElementTree(root).write(path)
    assert outcomes(path, [node]) == {node: 'PASS'}


def test_ambiguous_junit_identity_is_refused(tmp_path):
    with pytest.raises(ValueError, match='ambiguous JUnit identity'):
        outcomes(tmp_path/'unused.xml', [
            'tests/example.py::Group::test_method',
            'tests/example/Group.py::test_method',
        ])


def test_uncollected_junit_node_is_not_hidden(tmp_path, evidence):
    path = tmp_path/'unexpected.xml'
    path.write_text('<testsuite><testcase classname="tests.unexpected" name="test_extra"/></testsuite>')
    baseline, history, full, current, inventory = evidence
    observed = outcomes(path, inventory['current'])
    full.update(observed)
    current.update(observed)
    result = compare(baseline, history, full, current, inventory)
    assert result['inventory_drift'] == ['tests/unexpected.py::test_extra']
    assert not comparison_pass(result)


def test_module_balance_is_deterministic_and_exhaustive():
    nodes = [f'tests/{module}.py::test_{i}' for module in 'abcd' for i in range(2)]
    weights = {f'tests/{m}.py': v for m, v in zip('abcd', [8, 5, 3, 2])}
    shards = sharding.partition(nodes, weights)
    assert shards == sharding.partition(nodes, dict(reversed(list(weights.items()))))
    assert sorted(n for shard in shards for n in shard) == sorted(nodes)
    for module in weights:
        assert sum(any(n.startswith(module + '::') for n in shard) for shard in shards) == 1
    assert [sum(weights[n.split('::')[0]] for n in shard) / 2 for shard in shards] == [8, 5, 5]


@pytest.mark.parametrize('nodes,weights', [([], {}), (['a', 'a'], {'a': 1}), (['new::test'], {})])
def test_undefined_partition_is_refused(nodes, weights):
    with pytest.raises(ValueError):
        sharding.partition(nodes, weights)


@pytest.mark.parametrize('measured', [False, True])
def test_new_modules_are_unmeasured_but_never_omitted(measured):
    modules = ['tests/research/test_alpha_robustness.py',
               'tests/research/test_factor_candidates.py',
               'tests/research/test_pipeline_feedback.py']
    nodes = [module + '::test_' + str(i) for module in modules for i in range(2)]
    weights = {'existing.py': 205.881} if measured else {}
    plan = sharding.module_plan(nodes, weights)
    assert all(p == dict(seconds=205.881 if measured else 60.0, status='UNMEASURED')
               for p in plan.values())
    shards = sharding.partition(nodes, weights)
    assert shards == sharding.partition(list(nodes), dict(weights))
    assert all(shards)
    assert sorted(n for shard in shards for n in shard) == sorted(nodes)
    assert all(not (set(shards[i]) & set(shards[j])) for i in range(3) for j in range(i))
    assert sharding.module_plan(['existing.py::test'], weights)['existing.py'] == (
        dict(seconds=205.881, status='MEASURED') if measured else
        dict(seconds=60.0, status='UNMEASURED'))


@pytest.mark.parametrize('modules', [1, 2])
def test_empty_shards_are_refused(modules):
    with pytest.raises(ValueError, match='empty shard'):
        sharding.partition([f'module{i}.py::test' for i in range(modules)], {})


@pytest.fixture
def shard_receipts(tmp_path, monkeypatch):
    nodes = [f'tests/{m}.py::test_node' for m in 'abc']
    weights = {n.split('::')[0]: float(3 - i) for i, n in enumerate(nodes)}
    timings = tmp_path / 'timings.json'
    timings.write_text(json.dumps(dict(module_seconds=weights)))
    monkeypatch.setattr(sharding, 'TIMINGS', timings)
    folders = []
    for i, node in enumerate(nodes):
        folder = tmp_path / str(i)
        folder.mkdir()
        folders.append(folder)
        inventory = dict(current=nodes, selected=[node], shard=i, shard_count=3,
                         historical_collected=['old::test'], historical_collection_errors=['broken.py'],
                         module_plan=sharding.module_plan(nodes, weights))
        execution = dict(started=[node], completed=[node], exit_code=0, collection_errors=[])
        receipt = dict(status='PASS', exit_code=0, validation_inputs_unchanged=True,
                       python='3.13.17', platform='Windows-2025Server', subject_sha='head',
                       github_pr_head_sha='head', validation_input_sha256={'source': 'bytes'},
                       dependencies_sha256='dependencies', environment={'TZ': 'UTC'}, elapsed_seconds=i+1)
        for name, value in [('selection', inventory), ('execution', execution), ('receipt', receipt)]:
            (folder / (name + '.json')).write_text(json.dumps(value))
        root = ET.Element('testsuite')
        ET.SubElement(root, 'testcase', classname='tests.' + 'abc'[i], name='test_node')
        ET.ElementTree(root).write(folder / 'tests.xml')
        (folder / 'tests.log').write_text(node)
    return folders, tmp_path / 'combined'


def test_actual_shards_aggregate_without_reexecuting_tests(shard_receipts):
    folders, output = shard_receipts
    sharding.aggregate(folders, output, '3.13')
    evidence = json.loads((output / 'execution.json').read_bytes())
    assert evidence['count'] == 3
    assert evidence['actual_disjoint'] and evidence['actual_union_equals_full_inventory']
    assert len(outcomes(output / 'tests.xml')) == 3


@pytest.mark.parametrize('attack', ['missing_shard', 'wrong_shard', 'missing_started', 'duplicate_completed',
    'overlap', 'failed', 'cancelled', 'collection_error', 'subject', 'inputs', 'changed_inputs',
    'dependencies', 'runtime', 'full_inventory', 'junit_missing', 'junit_failed', 'plan_only'])
def test_actual_shard_faults_fail_closed(shard_receipts, attack):
    folders, output = shard_receipts
    if attack == 'missing_shard':
        folders.pop()
    elif attack.startswith('junit_'):
        root = ET.Element('testsuite')
        if attack == 'junit_failed':
            ET.SubElement(ET.SubElement(root, 'testcase', classname='tests.b', name='test_node'), 'failure')
        ET.ElementTree(root).write(folders[1] / 'tests.xml')
    else:
        target = 'selection' if attack in ('wrong_shard', 'full_inventory') else (
            'execution' if attack in ('missing_started', 'duplicate_completed', 'overlap',
                                      'cancelled', 'collection_error', 'plan_only') else 'receipt')
        path = folders[1] / (target + '.json')
        data = json.loads(path.read_bytes())
        if attack == 'wrong_shard': data['shard'] = 0
        elif attack == 'missing_started': data['started'] = []
        elif attack == 'duplicate_completed': data['completed'] *= 2
        elif attack == 'overlap': data['started'] = data['completed'] = ['tests/a.py::test_node']
        elif attack == 'failed': data['exit_code'] = 1
        elif attack == 'cancelled': data['exit_code'] = 2
        elif attack == 'collection_error': data['collection_errors'] = ['broken.py']
        elif attack == 'subject': data['subject_sha'] = 'other'
        elif attack == 'inputs': data['validation_input_sha256'] = {}
        elif attack == 'changed_inputs': data['validation_inputs_unchanged'] = False
        elif attack == 'dependencies': data['dependencies_sha256'] = 'other'
        elif attack == 'runtime': data['python'] = '3.14.0'
        elif attack == 'full_inventory': data['current'].append('unexpected.py::test_node')
        elif attack == 'plan_only': data['started'] = data['completed'] = []
        path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        sharding.aggregate(folders, output, '3.13')
    assert not output.exists()


@pytest.mark.parametrize('fail', [False, True])
def test_execution_inventory_records_real_pytest_reports(tmp_path, fail):
    test = tmp_path / 'test_serial.py'
    test.write_text('def test_real():\n    assert ' + str(not fail) + '\n')
    evidence = tmp_path / 'execution.json'
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    code = '''import sys, pytest
from pathlib import Path
from tools.ci.current_validation import ExecutionInventory
class Config:
    def getoption(self, name): return Path(sys.argv[2])
raise SystemExit(pytest.main([sys.argv[1], '-q', '-o', 'addopts=', '-c', str(Path(sys.argv[1]).parent / 'pytest.ini')], plugins=[ExecutionInventory(Config())]))
'''
    result = subprocess.run([sys.executable, '-c', code, str(test), str(evidence)],
                            cwd=sharding.ROOT, capture_output=True, text=True)
    assert result.returncode == int(fail), result.stdout + result.stderr
    actual = json.loads(evidence.read_bytes())
    assert len(actual['started']) == len(actual['completed']) == 1
    assert actual['started'] == actual['completed']
    assert actual['exit_code'] == int(fail)


def test_collection_partitions_full_inventory_before_sharding(tmp_path, monkeypatch):
    nodes = ['tests/a.py::test_1', 'tests/b.py::test_2', 'tests/c.py::test_3']
    monkeypatch.setattr(validation_layers, 'historical_nodes', lambda: {'old.py::test_old', 'broken.py'})
    weights = tmp_path / 'weights.json'
    weights.write_text(json.dumps(dict(module_seconds={n.split('::')[0]: i+1 for i, n in enumerate(nodes)})))
    monkeypatch.setattr(sharding, 'TIMINGS', weights)
    inventory = tmp_path / 'selection.json'
    deselected = []
    options = {'--historical-only': False, '--current-shard': 1, '--current-layer-inventory': inventory}
    config = SimpleNamespace(getoption=options.__getitem__, hook=SimpleNamespace(
        pytest_deselected=lambda items: deselected.extend(item.nodeid for item in items)))
    items = [SimpleNamespace(nodeid=n) for n in [*nodes, 'old.py::test_old']]
    current_validation.pytest_collection_modifyitems(config, items)
    observed = json.loads(inventory.read_bytes())
    assert observed['current'] == nodes
    assert observed['selected'] == [item.nodeid for item in items]
    assert set(observed['selected']) | set(deselected) == {*nodes, 'old.py::test_old'}
    assert observed['historical_collection_errors'] == ['broken.py']
