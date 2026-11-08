"""A software coverage checker must refuse lost tests and disguised historical failures."""
import copy
import pytest
import xml.etree.ElementTree as ET
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
])
def test_junit_identity_matches_collection(tmp_path, suffix):
    node = 'tests/evidence/test_p3_d03.py::'+suffix
    parts = suffix.split('::')
    root = ET.Element('testsuite')
    ET.SubElement(root, 'testcase',
                  classname='.'.join(['tests.evidence.test_p3_d03', *parts[:-1]]),
                  name=parts[-1])
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
