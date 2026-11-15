"""Focused checks for evidence mapping and offline shard models."""
import importlib.util
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

SPEC = importlib.util.spec_from_file_location('ci_perf_profile', Path(__file__).with_name('profile.py'))
profile = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = profile
SPEC.loader.exec_module(profile)


def xml_file(tmp_path, cases):
    suite = ET.Element('testsuite', time='12.0')
    for attrs in cases:
        ET.SubElement(suite, 'testcase', attrs)
    path = tmp_path / 'tests.xml'
    ET.ElementTree(suite).write(path)
    return path


def test_nested_parameterized_junit_maps_exact_inventory(tmp_path):
    nodes = ['tests/test_example.py::Outer::Inner::test_one[a::b]', 'tests/test_example.py::test_two']
    path = xml_file(tmp_path, [dict(classname='tests.test_example.Outer.Inner', name='test_one[a::b]', time='2.5'),
                               dict(classname='tests.test_example', name='test_two', time='3.5')])
    times, suite = profile.junit_times(path, nodes)
    assert times == dict(zip(nodes, [2.5, 3.5]))
    assert suite == 12


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'extra', 'nonfinite', 'negative', 'failure'])
def test_invalid_junit_evidence_rejected(tmp_path, fault):
    attrs = dict(classname='tests.test_example', name='test_one', time='1')
    path = xml_file(tmp_path, [attrs])
    tree = ET.parse(path)
    if fault == 'missing':
        tree.getroot().clear()
    elif fault in ('duplicate', 'extra'):
        ET.SubElement(tree.getroot(), 'testcase', dict(attrs, name='test_extra' if fault == 'extra' else 'test_one'))
    elif fault in ('nonfinite', 'negative'):
        tree.getroot()[0].set('time', 'nan' if fault == 'nonfinite' else '-1')
    else:
        ET.SubElement(tree.getroot()[0], 'failure')
    tree.write(path)
    with pytest.raises(ValueError):
        profile.junit_times(path, ['tests/test_example.py::test_one'])


@pytest.mark.parametrize('count', [3, 4])
def test_module_atomic_partition_is_complete_deterministic_and_balanced(count):
    nodes = [f'{m}.py::test_{n}' for m in range(9) for n in range(2)]
    weights = {f'{m}.py': float(m + 1) for m in range(9)}
    assignment, loads = profile.allocate(nodes, weights, count)
    assert set(assignment) == set(weights)
    assert set(assignment.values()) == set(range(count))
    assert sum(loads) == sum(weights.values())
    assert (assignment, loads) == profile.allocate(list(reversed(nodes)), dict(reversed(list(weights.items()))), count)
    metrics = profile.metrics(assignment, weights, count)
    assert metrics['loads_seconds'] == loads
    assert metrics['max_seconds'] >= metrics['lower_bound_seconds']
    if count == 3:
        existing = profile.partition(nodes, weights)
        assert all(assignment[n.split('::')[0]] == i for i, shard in enumerate(existing) for n in shard)


def test_candidate_weights_cannot_replace_trusted_base_weights_unilaterally():
    nodes = [f'{m}.py::test' for m in range(6)]
    old = {f'{m}.py': float(m + 1) for m in range(6)}
    candidate = dict(old, **{'0.py': 100.0})
    trusted = profile.partition(nodes, old)
    proposed = profile.partition(nodes, candidate)
    assert trusted != proposed
    shard = next(i for i in range(3) if trusted[i] != proposed[i])
    execution = dict(started=proposed[shard], completed=proposed[shard], exit_code=0, collection_errors=[])
    with pytest.raises(ValueError, match='independently balanced inventory'):
        profile.verify_execution(dict(selected=proposed[shard]), execution,
                                 {n: 'PASS' for n in proposed[shard]}, trusted[shard])


def test_new_or_changed_modules_remain_unmeasured_and_sensitivity_is_local():
    nodes = [profile.P01 + '::test_one', profile.P02 + '::test_one', 'changed.py::old', 'changed.py::new', 'new.py::test']
    weights = {profile.P01: 100.0, profile.P02: 20.0, 'changed.py': 1.0}
    data = dict(weights=weights, module_nodes={profile.P01: nodes[:1], profile.P02: nodes[1:2], 'changed.py': nodes[2:3]})
    source = dict(versions={v: data for v in profile.VERSIONS})
    result = profile.analyze(source, nodes, weights)['3.13']
    assert result['measured_modules'] == 2
    assert result['unmeasured_modules'] == ['changed.py', 'new.py']
    assert result['unknown_estimate_seconds'] == 100
    raw = result['scenarios']['raw:1']['plans']['3']
    p01 = result['scenarios']['p01:0.5']['plans']['3']
    both = result['scenarios']['both:0.5']['plans']['4']
    assert raw['total_seconds'] == 320
    assert p01['total_seconds'] == 270
    assert both['total_seconds'] == 260
    assert result['scenarios']['both:0.5']['status'] == 'SENSITIVITY_NOT_OPTIMIZATION_RESULT'
    source['input_sha256'] = {m: 'original' for m in weights}
    hashes = {n.split('::')[0]: 'original' for n in nodes}
    hashes[profile.P01] = 'changed'
    changed = profile.analyze(source, nodes, weights, hashes)['3.13']
    assert profile.P01 in changed['unmeasured_modules']
    assert changed['scenarios']['p01:0.5']['plans']['3']['total_seconds'] == changed['scenarios']['raw:1']['plans']['3']['total_seconds']
