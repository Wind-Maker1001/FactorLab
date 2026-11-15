"""Module-balanced current validation and independent execution-receipt aggregation."""
import argparse
import hashlib
from collections import Counter
import json
import math
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

from .validation_v2 import outcomes

ROOT = Path(__file__).resolve().parents[2]
TIMINGS = ROOT / 'tools/ci/current_module_timings.json'
SHARDS = 3
VERSIONS = ('3.13', '3.14')
WEIGHTS_PATH = 'tools/ci/current_module_timings.json'
PLAN_SCHEMA = 'FACTORLAB_TRUSTED_SHARD_PLAN_V1'
ALGORITHM = 'MODULE_LPT_V1'


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def read_json(raw):
    return json.loads(raw, object_pairs_hook=unique_object)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def load_weights(raw):
    document = read_json(raw)
    schema = document.get('schema')
    if schema == 'FACTORLAB_MEASURED_MODULE_TIMINGS_V1':
        tables = {version: document for version in VERSIONS}
    elif schema == 'FACTORLAB_MEASURED_MODULE_TIMINGS_V2':
        tables = document['versions']
        if set(tables) != set(VERSIONS):
            raise ValueError('weights require exactly Python 3.13 and 3.14')
    else:
        raise ValueError('unsupported module weights schema')
    for version, table in tables.items():
        weights = table['module_seconds']
        if not isinstance(weights, dict) or any(
                not isinstance(module, str) or not module.endswith('.py')
                or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0
                for module, seconds in weights.items()):
            raise ValueError('invalid measured module weights')
        for key in ('source_run', 'subject_sha', 'python'):
            if not isinstance(table.get(key), str) or not table[key]:
                raise ValueError('missing measured weights provenance: ' + key)
        artifacts = table.get('source_artifact')
        if isinstance(artifacts, str):
            artifacts = [artifacts]
        if (not isinstance(artifacts, list) or not artifacts
                or any(not isinstance(name, str) or not name for name in artifacts)
                or len(set(artifacts)) != len(artifacts)):
            raise ValueError('invalid measured source artifacts')
        if type(table.get('test_count')) is not int or table['test_count'] <= 0:
            raise ValueError('invalid measured test count')
        if schema.endswith('_V2') and not table['python'].startswith(version + '.'):
            raise ValueError('measured Python differs from weights table')
    return dict(schema=schema, sha256=hashlib.sha256(raw).hexdigest(), versions=tables)


def runtime_weights(raw, version):
    loaded = load_weights(raw)
    key = '3.13' if loaded['schema'].endswith('_V1') else version
    if key not in loaded['versions']:
        raise ValueError('unsupported weights Python version')
    return loaded['versions'][key]['module_seconds']


def base_weights(repository, base):
    return subprocess.check_output(['git', '-C', str(repository), 'show', base + ':' + WEIGHTS_PATH])


def build_spec(raw_weights, identity, shard_count):
    if shard_count not in (3, 4):
        raise ValueError('shard_count must be 3 or 4')
    return dict(schema=PLAN_SCHEMA, algorithm=ALGORITHM, shard_count=shard_count,
                identity=identity, weights=load_weights(raw_weights))


def receipt_identity(identity):
    return dict(subject_sha=identity['subject'], github_pr_head_sha=identity['pr_head'],
                trusted_base_sha=identity['base'], github_event_sha=identity['event_sha'],
                github_run_id=identity['run_id'], github_run_attempt=identity['run_attempt'])


def verify_spec(plan, repository, identity, shard_count):
    expected = build_spec(base_weights(repository, identity['base']), identity, shard_count)
    if plan.get('sharding') != expected:
        raise ValueError('sharding plan differs from trusted base, identity or shard count')
    return expected


def binding(spec, version, nodes):
    if version not in VERSIONS:
        raise ValueError('unsupported shard Python')
    return dict(plan_sha256=digest(spec), weights_sha256=spec['weights']['sha256'],
                weights_schema=spec['weights']['schema'], python=version,
                shard_count=spec['shard_count'], inventory_sha256=digest(nodes))


def verify_artifacts(pages, run_id, shard_count):
    expected = {f'extended-{version}-shard-{index}'
                for version in VERSIONS for index in range(shard_count)}
    artifacts = [artifact for page in pages for artifact in page['artifacts']
                 if artifact['name'].startswith('extended-')]
    if Counter(artifact['name'] for artifact in artifacts) != Counter(expected):
        raise ValueError('missing, duplicate or unexpected shard artifact names')
    if any(artifact['expired'] or str(artifact['workflow_run']['id']) != str(run_id)
           for artifact in artifacts):
        raise ValueError('shard artifact differs from trusted workflow run')


def module_plan(nodes, weights):
    """Unknown modules reserve at least the slowest measured module's cost."""
    modules = sorted({node.split('::')[0] for node in nodes})
    estimate = max(60.0, max(weights.values(), default=0.0))
    return {module: dict(seconds=weights[module] if module in weights else estimate,
                         status='MEASURED' if module in weights else 'UNMEASURED')
            for module in modules}


def partition(nodes, weights, shard_count=SHARDS):
    if shard_count not in (3, 4):
        raise ValueError('shard_count must be 3 or 4')
    if not nodes or len(nodes) != len(set(nodes)):
        raise ValueError('full current inventory must be nonempty and unique')
    modules = {}
    for node in nodes:
        modules.setdefault(node.split('::')[0], []).append(node)
    plan = module_plan(nodes, weights)
    loads, assignments = [0.0] * shard_count, {}
    for module in sorted(modules, key=lambda m: (-plan[m]['seconds'], m)):
        shard = min(range(shard_count), key=lambda i: (loads[i], i))
        assignments[module] = shard
        loads[shard] += plan[module]['seconds']
    shards = [[node for node in nodes if assignments[node.split('::')[0]] == i]
              for i in range(shard_count)]
    if any(not shard for shard in shards):
        raise ValueError(f'empty shard: {shard_count} nonempty module partitions required')
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


def aggregate_pr(root, output, identity, shard_count=SHARDS, spec=None):
    """Bind both Python coverage checks to this trusted workflow's exact subject."""
    if not all(identity.values()):
        raise ValueError('complete expected PR validation identity required')
    if shard_count not in (3, 4):
        raise ValueError('shard_count must be 3 or 4')
    if spec is not None:
        if receipt_identity(spec['identity']) != identity or spec['shard_count'] != shard_count:
            raise ValueError('trusted sharding identity differs')
        if {path.name for path in root.iterdir()} != set(VERSIONS):
            raise ValueError('missing or unexpected Python artifacts')
    inputs = None
    collection = None
    for version in VERSIONS:
        if {path.name for path in (root / version).iterdir()} != {str(i) for i in range(shard_count)}:
            raise ValueError('missing or unexpected shard artifacts')
        folders = [root / version / str(shard) / 'current' for shard in range(shard_count)]
        for shard, folder in enumerate(folders):
            receipt = read_json((folder / 'receipt.json').read_bytes())
            inventory = read_json((folder / 'selection.json').read_bytes())
            observed_collection = {key: inventory[key] for key in
                                   ('current', 'historical_collected', 'historical_collection_errors')}
            if collection is not None and observed_collection != collection:
                raise ValueError('full collection differs across Python versions')
            collection = observed_collection
            if (spec is not None or shard_count == 4) and receipt.get('shard_count') != shard_count:
                raise ValueError('receipt shard count differs from trusted plan')
            if any(receipt.get(key) != value for key, value in identity.items()):
                raise ValueError('shard differs from expected PR validation identity')
            if receipt['suite'] != 'current' or receipt['shard'] != shard:
                raise ValueError('wrong suite or receipt shard identity')
            dependencies = (folder / 'dependencies.txt').read_text(encoding='utf-8')
            if hashlib.sha256(dependencies.encode()).hexdigest() != receipt['dependencies_sha256']:
                raise ValueError('dependency evidence differs from receipt')
            if inputs is not None and receipt['validation_input_sha256'] != inputs:
                raise ValueError('frozen inputs differ across Python versions')
            inputs = receipt['validation_input_sha256']
        aggregate(folders, output / version, version, shard_count, spec)


def aggregate(folders, output, python_version, shard_count=SHARDS, spec=None):
    if shard_count not in (3, 4) or len(folders) != shard_count:
        raise ValueError(f'exactly {shard_count} shard receipts required')
    weights = (spec['weights']['versions'][python_version]['module_seconds'] if spec is not None
               else runtime_weights(TIMINGS.read_bytes(), python_version))
    combined, inventories, receipts, executions = {}, [], [], []
    junit = ET.Element('testsuites')
    for index, folder in enumerate(folders):
        inventory = read_json((folder / 'selection.json').read_bytes())
        execution = read_json((folder / 'execution.json').read_bytes())
        receipt = read_json((folder / 'receipt.json').read_bytes())
        if inventory['shard'] != index or inventory['shard_count'] != shard_count:
            raise ValueError('missing or repeated shard identity')
        if inventories and any(inventory[k] != inventories[0][k] for k in
                               ('current', 'historical_collected', 'historical_collection_errors')):
            raise ValueError('full collection differs between shards')
        expected = partition(inventory['current'], weights, shard_count)[index]
        if inventory['module_plan'] != module_plan(inventory['current'], weights):
            raise ValueError('measured/unmeasured module plan differs')
        if spec is not None:
            expected_binding = binding(spec, python_version, inventory['current'])
            if any(evidence.get('sharding') != expected_binding
                   for evidence in (inventory, execution, receipt)):
                raise ValueError('shard evidence plan binding differs')
            reports = execution['reports']
            if set(reports) != set(expected) or any(
                    phases != {'setup': 'passed', 'call': 'passed', 'teardown': 'passed'}
                    for phases in reports.values()) or execution['report_errors']:
                raise ValueError('actual passing test phases differ from expected execution')
        observed = outcomes(folder / 'tests.xml', inventory['current'])
        verify_execution(inventory, execution, observed, expected)
        if spec is not None or shard_count == 4:
            counts = dict(tests=len(expected), passed=len(expected), failures=0, errors=0, skipped=0)
            if (receipt.get('shard_count') != shard_count or receipt.get('shard') != index or
                    receipt.get('counts') != counts):
                raise ValueError('receipt count or shard identity differs from actual execution')
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
        schema=('FACTORLAB_THREE_SHARD_COVERAGE_V1' if shard_count == 3 else
                'FACTORLAB_FOUR_SHARD_COVERAGE_V1'), shard_count=shard_count, status='PASS', python=python_version,
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
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--shards', nargs='+', type=Path)
    parser.add_argument('--shard-count', type=int, choices=(3, 4), default=SHARDS)
    source.add_argument('--pr-shards', type=Path)
    source.add_argument('--verify-artifacts', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--python', choices=['3.13', '3.14'])
    parser.add_argument('--subject-sha')
    parser.add_argument('--pr-head-sha')
    parser.add_argument('--trusted-base-sha')
    parser.add_argument('--event-sha')
    parser.add_argument('--run-id')
    parser.add_argument('--run-attempt')
    parser.add_argument('--risk-plan', type=Path)
    parser.add_argument('--identity', type=Path)
    parser.add_argument('--artifact-manifest', type=Path)
    args = parser.parse_args()
    if args.verify_artifacts is not None:
        if not args.run_id:
            parser.error('--verify-artifacts requires --run-id')
        verify_artifacts(read_json(args.verify_artifacts.read_bytes()), args.run_id, args.shard_count)
        return
    if args.output is None:
        parser.error('aggregation requires --output')
    if args.pr_shards is not None:
        identity = dict(subject_sha=args.subject_sha, github_pr_head_sha=args.pr_head_sha,
                        trusted_base_sha=args.trusted_base_sha, github_event_sha=args.event_sha,
                        github_run_id=args.run_id, github_run_attempt=args.run_attempt)
        if args.python is not None or not all(identity.values()):
            parser.error('--pr-shards requires exact PR, subject, base, event and run identity, not --python')
        if args.risk_plan is None or args.identity is None or args.artifact_manifest is None:
            parser.error('--pr-shards requires sealed --risk-plan, --identity and --artifact-manifest')
        verify_artifacts(read_json(args.artifact_manifest.read_bytes()), args.run_id, args.shard_count)
        from . import pr_identity
        expected = pr_identity.event_identity(
            read_json(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes()), os.environ)
        issued = read_json(args.identity.read_bytes())
        plan = pr_identity.validate(ROOT, expected, issued, args.risk_plan.read_bytes(),
                                    os.environ['VALIDATION_SUBJECT'], os.environ['RISK_SHA256'],
                                    checkout=False, shard_count=args.shard_count)
        if identity != receipt_identity(issued):
            raise ValueError('aggregation CLI identity differs from trusted event')
        aggregate_pr(args.pr_shards, args.output, identity, args.shard_count, plan['sharding'])
    else:
        if args.python is None:
            parser.error('--shards requires --python')
        aggregate(args.shards, args.output, args.python, args.shard_count)


if __name__ == '__main__':
    main()
