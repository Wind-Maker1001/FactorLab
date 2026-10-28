"""A software coverage checker must refuse lost tests and disguised historical failures."""
import copy
import pytest
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
