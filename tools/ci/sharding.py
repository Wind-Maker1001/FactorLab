"""Module-balanced current validation and independent execution-receipt aggregation."""
import argparse
from collections import Counter
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from tools.ci.validation_v2 import outcomes

ROOT = Path(__file__).resolve().parents[2]
TIMINGS = ROOT / 'tools/ci/current_module_timings.json'
SHARDS = 3


def module_plan(nodes, weights):
    """Unknown modules reserve at least the slowest measured module's cost."""
    modules = sorted({node.split('::')[0] for node in nodes})
    estimate = max(60.0, max(weights.values(), default=0.0))
    return {module: dict(seconds=weights[module] if module in weights else estimate,
                         status='MEASURED' if module in weights else 'UNMEASURED')
            for module in modules}


def partition(nodes, weights):
    if not nodes or len(nodes) != len(set(nodes)):
        raise ValueError('full current inventory must be nonempty and unique')
    modules = {}
    for node in nodes:
        modules.setdefault(node.split('::')[0], []).append(node)
    plan = module_plan(nodes, weights)
    loads, assignments = [0.0] * SHARDS, {}
    for module in sorted(modules, key=lambda m: (-plan[m]['seconds'], m)):
        shard = min(range(SHARDS), key=lambda i: (loads[i], i))
        assignments[module] = shard
        loads[shard] += plan[module]['seconds']
    shards = [[node for node in nodes if assignments[node.split('::')[0]] == i]
              for i in range(SHARDS)]
    if any(not shard for shard in shards):
        raise ValueError('empty shard: three nonempty module partitions required')
    return shards


def verify_execution(inventory, execution, observed, expected):
    if inventory['selected'] != expected:
        raise ValueError('shard selection differs from independently balanced inventory')
    for key in ('started', 'completed'):
        nodes = execution[key]
        if Counter(nodes) != Counter(expected):
            raise ValueError('actual ' + key + ' coverage differs from full inventory partition')
    if set(observed) != set(expected) or any(s != 'PASS' for s in observed.values()):
        raise ValueError('actual JUnit coverage or outcomes differ')
    if execution['exit_code'] != 0 or execution['collection_errors']:
        raise ValueError('pytest execution or collection failed')


def aggregate(folders, output, python_version):
    if len(folders) != SHARDS:
        raise ValueError('exactly three shard receipts required')
    weights = json.loads(TIMINGS.read_bytes())['module_seconds']
    combined, inventories, receipts, executions = {}, [], [], []
    junit = ET.Element('testsuites')
    for index, folder in enumerate(folders):
        inventory = json.loads((folder / 'selection.json').read_bytes())
        execution = json.loads((folder / 'execution.json').read_bytes())
        receipt = json.loads((folder / 'receipt.json').read_bytes())
        if inventory['shard'] != index or inventory['shard_count'] != SHARDS:
            raise ValueError('missing or repeated shard identity')
        if inventories and any(inventory[k] != inventories[0][k] for k in
                               ('current', 'historical_collected', 'historical_collection_errors')):
            raise ValueError('full collection differs between shards')
        expected = partition(inventory['current'], weights)[index]
        if inventory['module_plan'] != module_plan(inventory['current'], weights):
            raise ValueError('measured/unmeasured module plan differs')
        observed = outcomes(folder / 'tests.xml', inventory['current'])
        verify_execution(inventory, execution, observed, expected)
        if set(combined) & set(observed):
            raise ValueError('actual shard executions overlap')
        if (receipt['status'] != 'PASS' or receipt['exit_code'] != 0 or
                not receipt['validation_inputs_unchanged'] or
                not receipt['python'].startswith(python_version + '.') or
                not receipt['platform'].startswith('Windows-')):
            raise ValueError('failed shard or wrong runtime identity')
        if receipts and any(receipt[k] != receipts[0][k] for k in
                            ('subject_sha', 'github_pr_head_sha', 'validation_input_sha256',
                             'dependencies_sha256', 'python', 'platform', 'environment')):
            raise ValueError('shard subject, frozen inputs or runtime differs')
        combined.update(observed)
        inventories.append(inventory)
        receipts.append(receipt)
        executions.append(execution)
        root = ET.parse(folder / 'tests.xml').getroot()
        junit.extend([root] if root.tag == 'testsuite' else list(root))
    if set(combined) != set(inventories[0]['current']):
        raise ValueError('actual shard union differs from complete nonhistorical inventory')
    output.mkdir(parents=True, exist_ok=False)
    ET.ElementTree(junit).write(output / 'tests.xml', encoding='utf-8', xml_declaration=True)
    (output / 'selection.json').write_text(json.dumps(inventories[0], indent=2) + '\n', encoding='utf-8')
    receipt = dict(receipts[0], shard=None, counts=dict(tests=len(combined), passed=len(combined),
                   failures=0, errors=0, skipped=0), scope='COMPLETE_NONHISTORICAL_SHARDED',
                   elapsed_seconds=max(r['elapsed_seconds'] for r in receipts),
                   shard_elapsed_seconds=[r['elapsed_seconds'] for r in receipts])
    (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    (output / 'execution.json').write_text(json.dumps(dict(
        schema='FACTORLAB_THREE_SHARD_COVERAGE_V1', status='PASS', python=python_version,
        subject_sha=receipt['subject_sha'], count=len(combined),
        actual_disjoint=True, actual_union_equals_full_inventory=True,
        shards=[dict(shard=i, count=len(e['completed']), elapsed_seconds=receipts[i]['elapsed_seconds'])
                for i, e in enumerate(executions)]), indent=2) + '\n', encoding='utf-8')
    with (output / 'tests.log').open('wb') as log:
        for folder in folders:
            log.write((folder / 'tests.log').read_bytes())
    print(f'Python {python_version}: actual disjoint union verified, {len(combined)} tests', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shards', nargs=3, type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--python', choices=['3.13', '3.14'], required=True)
    args = parser.parse_args()
    aggregate(args.shards, args.output, args.python)


if __name__ == '__main__':
    main()
