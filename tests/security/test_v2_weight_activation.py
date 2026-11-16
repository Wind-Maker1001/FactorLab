"""Production V2 provenance, archived assignments and complete receipt replay."""
import copy
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from tools.ci import sharding
from tests.security.test_trusted_shard_plan import trusted_shards
from tests.security.test_validation_v2 import shard_count, shard_receipts, pr_shard_receipts

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'docs/validation/ci-perf-05'
WEIGHTS = ROOT / sharding.WEIGHTS_PATH
SHA256 = '75a5bac467d1bc23cf9f1e4e6b83d3b00beb87301994051d2442f6e032fbc5d3'


def read(name):
    return sharding.read_json((EVIDENCE / name).read_bytes())


def test_production_v2_provenance():
    raw = WEIGHTS.read_bytes()
    assert raw == (EVIDENCE / 'candidate_module_timings.json').read_bytes()
    loaded = sharding.load_weights(raw)
    assert loaded['schema'] == 'FACTORLAB_MEASURED_MODULE_TIMINGS_V2'
    assert loaded['sha256'] == SHA256
    source = read('sources-43735547182.json')
    target = read('target-inventory.json')
    analysis = read('analysis-43735547182.json')
    assert len(target['current']) == len(set(target['current'])) == 7819
    for version in sharding.VERSIONS:
        table = loaded['versions'][version]
        assert table['source_identity'] == source['identity']
        assert table['subject_sha'] == '31346cca2d233ce65bf64728009884c53788b86c'
        assert table['source_run'] == (
            'https://github.com/Wind-Maker1001/FactorLab/actions/runs/43735547182')
        assert table['source_artifact'] == [f'extended-{version}-shard-{i}' for i in range(3)]
        assert table['test_count'] == table['target_test_count'] == 7819
        assert table['target_subject_sha'] == target['subject_sha']
        assert table['unmeasured_modules'] == []
        weights = table['module_seconds']
        assert len(weights) == 315
        assert set(weights) == set(target['module_sha256'])
        assert weights == analysis['versions'][version]['candidate_weights']
        assert weights == analysis['versions'][version]['observed_weights']
        for module in weights:
            path = Path(module)
            assert module.startswith('tests/') and '..' not in path.parts
            assert not path.is_absolute() and path.as_posix() == module


@pytest.mark.parametrize('count', [3, 4])
def test_production_v2_matches_p05_assignments(count):
    loaded = sharding.load_weights(WEIGHTS.read_bytes())
    nodes = read('target-inventory.json')['current']
    analysis = read('analysis-43735547182.json')
    for version in sharding.VERSIONS:
        weights = loaded['versions'][version]['module_seconds']
        partitions = sharding.partition(nodes, weights, count)
        assignments = {node.split('::')[0]: index
                       for index, selected in enumerate(partitions) for node in selected}
        assert assignments == analysis['versions'][version]['plans'][str(count)]['assignments']
        assert sharding.partition(nodes, dict(reversed(list(weights.items()))), count) == partitions
        assert sorted(node for selected in partitions for node in selected) == sorted(nodes)
        unknown = sharding.module_plan(['tests/unmeasured.py::test'], weights)
        assert unknown['tests/unmeasured.py'] == {
            'seconds': max(60.0, max(weights.values())), 'status': 'UNMEASURED'}


def test_production_v2_complete_synthetic_receipts(trusted_shards, shard_count):
    root, output, identity, original_spec = trusted_shards
    spec = sharding.build_spec(WEIGHTS.read_bytes(), original_spec['identity'], shard_count)
    nodes = read('target-inventory.json')['current']
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
            receipt = sharding.read_json((folder / 'receipt.json').read_bytes())
            receipt.update(sharding=bound, counts=dict(tests=len(selected), passed=len(selected),
                                                      failures=0, errors=0, skipped=0))
            for name, value in [('selection', inventory), ('execution', execution), ('receipt', receipt)]:
                (folder / (name + '.json')).write_text(json.dumps(value), encoding='utf-8')
            suite = ET.Element('testsuite')
            for node in selected:
                module, name = node.split('::', 1)
                ET.SubElement(suite, 'testcase', classname=module[:-3].replace('/', '.'), name=name)
            ET.ElementTree(suite).write(folder / 'tests.xml')
    sharding.aggregate_pr(root, output, identity, shard_count, spec)
    for version in sharding.VERSIONS:
        receipt = sharding.read_json((output / version / 'receipt.json').read_bytes())
        assert receipt['counts']['passed'] == 7819


@pytest.mark.parametrize('attack', ['source', 'digest', 'version', 'weights'])
def test_production_v2_plan_mutations_rejected(monkeypatch, attack):
    raw = WEIGHTS.read_bytes()
    identity = dict(base='trusted-base')
    plan = dict(sharding=sharding.build_spec(raw, identity, 3))
    monkeypatch.setattr(sharding, 'base_weights', lambda repository, base: raw)
    spec = copy.deepcopy(plan['sharding'])
    if attack == 'source':
        spec['weights']['versions']['3.14']['source_run'] = 'wrong-source'
    elif attack == 'digest':
        spec['weights']['sha256'] = '0' * 64
    elif attack == 'version':
        del spec['weights']['versions']['3.14']
    else:
        spec['weights']['versions']['3.14']['module_seconds']['tests/forged.py'] = 0
    with pytest.raises(ValueError, match='trusted base'):
        sharding.verify_spec(dict(sharding=spec), ROOT, identity, 3)


def test_archived_v1_runtime_weights_replay():
    raw = (EVIDENCE / 'previous_module_timings.json').read_bytes()
    loaded = sharding.load_weights(raw)
    assert loaded['schema'] == 'FACTORLAB_MEASURED_MODULE_TIMINGS_V1'
    nodes = read('target-inventory.json')['current']
    analysis = read('analysis-43735547182.json')
    for version in sharding.VERSIONS:
        weights = sharding.runtime_weights(raw, version)
        assert weights == loaded['versions']['3.13']['module_seconds']
        parts = sharding.partition(nodes, weights, 3)
        assignments = {node.split('::')[0]: i for i, part in enumerate(parts) for node in part}
        assert assignments == analysis['shared']['3']['existing_v1']['assignments']
