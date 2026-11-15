"""Rebuild CI timing evidence and module-atomic LPT plans (no workflow execution)."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.ci.sharding import module_plan, partition, verify_execution
from tools.ci.validation_v2 import outcomes

VERSIONS = ('3.13', '3.14')
IDENTITY = ('subject_sha', 'github_pr_head_sha', 'trusted_base_sha', 'github_event_sha',
            'github_run_id', 'github_run_attempt')
P01 = 'tests/integrations/test_demo_grid_supervisor.py'
P02 = 'tests/evidence/test_pit_archive_verifiers.py'


def load(path):
    return json.loads(path.read_bytes())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def seconds(start, end):
    return (datetime.fromisoformat(end.replace('Z', '+00:00')) -
            datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds()


def junit_times(path, nodes):
    """Map classname/name through the selector inventory, including nested classes."""
    observed = outcomes(path, nodes)
    require(set(observed) == set(nodes) and set(observed.values()) == {'PASS'},
            'JUnit coverage/outcomes mismatch')
    identities = {}
    for node in nodes:
        module, qualified = node.split('::', 1)
        name, bracket, parameter = qualified.partition('[')
        parts = name.split('::')
        key = ('.'.join([module.removesuffix('.py').replace('/', '.'), *parts[:-1]]),
               parts[-1] + bracket + parameter)
        require(key not in identities, 'ambiguous JUnit identity')
        identities[key] = node
    root = ET.parse(path).getroot()
    result = {}
    for case in root.iter('testcase'):
        key = case.get('classname', ''), case.get('name')
        require(key in identities, 'unmapped JUnit testcase')
        value = float(case.attrib['time'])
        require(math.isfinite(value) and value >= 0, 'invalid JUnit duration')
        result[identities[key]] = value
    suites = [root] if root.tag == 'testsuite' else list(root)
    return result, sum(float(s.attrib['time']) for s in suites)


def allocate(nodes, weights, count):
    plan = module_plan(nodes, weights)
    loads = [0.0] * count
    assignments = {}
    for module in sorted(plan, key=lambda m: (-plan[m]['seconds'], m)):
        shard = min(range(count), key=lambda i: (loads[i], i))
        assignments[module] = shard
        loads[shard] += plan[module]['seconds']
    return assignments, loads


def metrics(assignments, costs, count):
    loads = [0.0] * count
    for module, shard in assignments.items():
        loads[shard] += costs[module]
    total = sum(loads)
    lower = max(total / count, max(costs.values()))
    return dict(loads_seconds=loads, max_seconds=max(loads), total_seconds=total,
                imbalance=max(loads) / (total / count) - 1,
                lower_bound_seconds=lower, bound_gap_seconds=max(loads) - lower)


def read_run(directory, old_weights):
    run, jobs, artifacts = (load(directory / f'{name}.json') for name in ('run', 'jobs', 'artifacts'))
    require(run['conclusion'] == 'success' and run['status'] == 'completed', 'run not successful')
    artifacts = {a['name']: a for a in artifacts['artifacts']}
    manifest = []
    versions = {}
    identity = None
    input_identity = None
    for version in VERSIONS:
        inventory = None
        combined = {}
        baseline = []
        module_sources = {}
        first_receipt = None
        for shard in range(3):
            name = f'extended-{version}-shard-{shard}'
            folder = directory / name / 'current'
            selection, execution, receipt = (load(folder / f'{n}.json') for n in ('selection', 'execution', 'receipt'))
            expected = partition(selection['current'], old_weights)[shard]
            observed = outcomes(folder / 'tests.xml', selection['current'])
            verify_execution(selection, execution, observed, expected)
            require(selection['shard'] == shard and selection['shard_count'] == 3 and
                    selection['module_plan'] == module_plan(selection['current'], old_weights), 'selection plan drift')
            require(receipt['status'] == 'PASS' and receipt['exit_code'] == 0 and
                    receipt['suite'] == 'current' and receipt['shard'] == shard and
                    receipt['validation_inputs_unchanged'] and receipt['required_execution_observed'], 'failed receipt')
            require(receipt['python'].startswith(version + '.') and receipt['platform'].startswith('Windows-'),
                    'runtime mismatch')
            current_identity = {k: receipt[k] for k in IDENTITY}
            require(all(current_identity.values()), 'incomplete source identity')
            require(current_identity['github_run_id'] == str(run['id']) and
                    current_identity['github_run_attempt'] == str(run['run_attempt']) and
                    current_identity['github_pr_head_sha'] == run['head_sha'], 'run identity mismatch')
            if identity is not None:
                require(current_identity == identity, 'cross-shard identity mismatch')
                require(receipt['validation_input_sha256'] == input_identity, 'frozen input mismatch')
            identity, input_identity = current_identity, receipt['validation_input_sha256']
            if first_receipt is not None:
                for key in ('python', 'platform', 'environment', 'dependencies_sha256'):
                    require(receipt[key] == first_receipt[key], 'cross-shard runtime/dependency drift')
            first_receipt = receipt
            dependency_text = (folder / 'dependencies.txt').read_text(encoding='utf-8')
            require(hashlib.sha256(dependency_text.encode()).hexdigest() == receipt['dependencies_sha256'],
                    'dependency digest mismatch')
            if inventory is not None:
                for key in ('current', 'historical_collected', 'historical_collection_errors'):
                    require(selection[key] == inventory[key], 'full inventory mismatch')
            inventory = selection
            times, suite_seconds = junit_times(folder / 'tests.xml', expected)
            require(not set(combined).intersection(times), 'overlapping actual executions')
            combined.update(times)
            require(receipt['counts'] == dict(tests=len(times), passed=len(times), failures=0, errors=0, skipped=0),
                    'receipt counts mismatch')
            job_name = f'Extended software Python {version} shard {shard}'
            matching = [j for j in jobs['jobs'] if j['name'] == job_name]
            require(len(matching) == 1, 'missing/duplicate GitHub job')
            job = matching[0]
            require(job['conclusion'] == 'success' and 'windows-latest' in job['labels'], 'job runtime mismatch')
            steps = [s for s in job['steps'] if 'integration_checks.py --suite current' in s['name']]
            require(len(steps) == 1 and steps[0]['conclusion'] == 'success', 'missing/failed test step')
            step = steps[0]
            elapsed = seconds(step['started_at'], step['completed_at'])
            job_seconds = seconds(job['started_at'], job['completed_at'])
            setup = next(s for s in job['steps'] if s['name'] == 'Install dependencies and package')
            baseline.append(dict(shard=shard, tests=len(times), job_id=job['id'],
                runner=job['runner_name'], labels=job['labels'], platform=receipt['platform'],
                python=receipt['python'], dependencies_sha256=receipt['dependencies_sha256'],
                testcase_seconds=sum(times.values()), suite_seconds=suite_seconds,
                receipt_seconds=receipt['elapsed_seconds'], test_step_seconds=elapsed,
                install_seconds=seconds(setup['started_at'], setup['completed_at']),
                non_test_job_seconds=job_seconds-elapsed, job_seconds=job_seconds,
                step_residual_seconds=elapsed-sum(times.values())))
            for node in times:
                module_sources[node.split('::')[0]] = dict(artifact=name, artifact_id=artifacts[name]['id'],
                                                          shard=shard, job_id=job['id'])
            for filename in ('tests.xml', 'selection.json', 'execution.json', 'receipt.json', 'dependencies.txt', 'tests.log'):
                path = folder / filename
                manifest.append(dict(path=path.relative_to(directory).as_posix(),
                                     sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size))
        require(Counter(list(combined)) == Counter(inventory['current']), 'actual union differs from inventory')
        aggregate_folder = directory / 'verified-pr-current-coverage' / version
        aggregate_receipt, aggregate_execution = (load(aggregate_folder / f'{n}.json') for n in ('receipt', 'execution'))
        require(all(aggregate_receipt[k] == identity[k] for k in IDENTITY), 'aggregate identity mismatch')
        require(aggregate_receipt['status'] == 'PASS' and aggregate_receipt['counts']['tests'] == len(combined) and
                aggregate_execution['count'] == len(combined) and aggregate_execution['status'] == 'PASS' and
                aggregate_execution['actual_disjoint'] and aggregate_execution['actual_union_equals_full_inventory'],
                'aggregate coverage mismatch')
        require(outcomes(aggregate_folder / 'tests.xml', inventory['current']) == {n: 'PASS' for n in combined},
                'aggregate JUnit mismatch')
        weights = defaultdict(float)
        module_nodes = defaultdict(list)
        for node, value in combined.items():
            module = node.split('::')[0]
            weights[module] += value
            module_nodes[module].append(node)
        versions[version] = dict(nodes=list(combined), module_nodes=dict(module_nodes), weights=dict(weights),
                                 module_sources=module_sources, baseline=baseline, python=first_receipt['python'])
    require(set(versions['3.13']['nodes']) == set(versions['3.14']['nodes']), 'Python inventory differs')
    for path in [directory / f'{n}.json' for n in ('run', 'jobs', 'artifacts')] + sorted((directory / 'verified-pr-current-coverage').glob('*/*')):
        if path.is_file():
            manifest.append(dict(path=path.relative_to(directory).as_posix(),
                                 sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size))
    return dict(run=run, jobs=jobs, artifacts=artifacts, identity=identity, versions=versions, manifest=manifest,
                input_sha256=input_identity)


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def analyze(source, target_nodes, old_weights, target_hashes=None):
    result = {}
    for version, data in source['versions'].items():
        target_modules = defaultdict(list)
        for node in target_nodes:
            target_modules[node.split('::')[0]].append(node)
        measured = {m: value for m, value in data['weights'].items()
                    if m in target_modules and set(target_modules[m]) == set(data['module_nodes'][m])
                    and (target_hashes is None or target_hashes[m] == source['input_sha256'].get(m))}
        costs = {m: p['seconds'] for m, p in module_plan(target_nodes, measured).items()}
        scenarios = {}
        for scenario, targets in [('raw', ()), ('p01', (P01,)), ('p02', (P02,)), ('both', (P01, P02))]:
            for fraction in ((1.0,) if not targets else (1.0, 0.75, 0.5, 0.25, 0.0)):
                scenario_costs = {m: c * fraction if m in targets and m in measured else c for m, c in costs.items()}
                entry = dict(status='MEASURED_INPUT_MODEL' if not targets else 'SENSITIVITY_NOT_OPTIMIZATION_RESULT',
                             remaining_fraction=fraction, affected_modules=list(targets), plans={})
                for count in (3, 4):
                    assignment, _ = allocate(target_nodes, scenario_costs, count)
                    entry['plans'][str(count)] = dict(metrics(assignment, scenario_costs, count), assignments=assignment)
                scenarios[f'{scenario}:{fraction:g}'] = entry
        old_assignment, _ = allocate(target_nodes, old_weights, 3)
        new_assignment = scenarios['raw:1']['plans']['3']['assignments']
        result[version] = dict(module_estimated_seconds=costs, measured_modules=len(measured), unmeasured_modules=sorted(set(costs)-set(measured)),
            unknown_estimate_seconds=max(60.0, max(measured.values(), default=0.0)),
            old_plan_evaluated_with_current_costs=metrics(old_assignment, costs, 3),
            modules_moved=sorted(m for m in costs if old_assignment[m] != new_assignment[m]), scenarios=scenarios)
    for version, result_version in result.items():
        for key, scenario in result_version['scenarios'].items():
            costs = dict(result_version['module_estimated_seconds'])
            for module in scenario['affected_modules']:
                if module in costs and module not in result_version['unmeasured_modules']:
                    costs[module] *= scenario['remaining_fraction']
            for count, plan in scenario['plans'].items():
                shared = result['3.13']['scenarios'][key]['plans'][count]['assignments']
                plan['shared_python313_assignment_metrics'] = metrics(shared, costs, int(count))
    return result


def overhead(source):
    jobs, run = source['jobs']['jobs'], source['run']
    by_name = {j['name']: j for j in jobs}
    risk = by_name['Trusted risk selection']
    coverage = by_name['Verify PR actual six-shard coverage']
    gate = by_name['FactorLab merge gate']
    extended = [j for j in jobs if j['name'].startswith('Extended software Python')]
    latest = max(j['completed_at'] for j in extended)
    prefix = seconds(run['created_at'], risk['completed_at'])
    tail = seconds(latest, coverage['started_at']) + seconds(coverage['started_at'], coverage['completed_at'])
    tail += seconds(coverage['completed_at'], gate['started_at']) + seconds(gate['started_at'], gate['completed_at'])
    dispatch = max(seconds(risk['completed_at'], j['started_at']) for j in extended)
    non_test = max(b['non_test_job_seconds'] for d in source['versions'].values() for b in d['baseline'])
    residual = max(b['step_residual_seconds'] for d in source['versions'].values() for b in d['baseline'])
    budget = 900-prefix-dispatch-tail-non_test
    other_finish = max(seconds(run['created_at'], j['completed_at']) for j in jobs
                       if j['name'] not in ('Trusted risk selection', coverage['name'], gate['name']) and j not in extended)
    return dict(prefix_seconds=prefix, dispatch_seconds=dispatch, coverage_and_gate_tail_seconds=tail,
        max_non_test_job_seconds=non_test, max_test_step_residual_seconds=residual,
        max_test_step_budget_seconds=budget, max_testcase_budget_seconds=budget-residual,
        other_parallel_jobs_observed_finish_seconds=other_finish,
        observed_e2e_seconds=seconds(run['created_at'], gate['completed_at']),
        other_parallel_no_queue_finish_seconds=prefix + max(seconds(j['started_at'], j['completed_at']) for j in jobs
            if j['name'] not in ('Trusted risk selection', coverage['name'], gate['name']) and j not in extended),
        other_jobs_runner_seconds=sum(seconds(j['started_at'], j['completed_at']) for j in jobs if j not in extended),
        other_jobs_rounded_minutes=sum(math.ceil(seconds(j['started_at'], j['completed_at'])/60) for j in jobs if j not in extended))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, nargs='+', required=True, help='chronological run directories; last is primary')
    parser.add_argument('--old-weights', type=Path, required=True)
    parser.add_argument('--target-selection', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    old = load(args.old_weights)
    sources = [read_run(p, old['module_seconds']) for p in args.evidence]
    target = load(args.target_selection)
    require(all(k in target for k in ('module_sha256', 'subject_sha', 'collection_python')),
            'target must include collection identity and module source hashes; use collection.py')
    nodes = target['current']
    require(nodes and len(nodes) == len(set(nodes)), 'target inventory empty/duplicated')
    primary = sources[-1]
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    all_results = []
    for source in sources:
        run_id = str(source['run']['id'])
        timing_overhead = overhead(source)
        baseline_analysis = analyze(source, source['versions']['3.13']['nodes'], old['module_seconds'])
        target_analysis = analyze(source, nodes, old['module_seconds'], target.get('module_sha256'))
        for analysis in (baseline_analysis, target_analysis):
            for version, result in analysis.items():
                base = source['versions'][version]['baseline']
                setup = max(b['non_test_job_seconds'] for b in base)
                residual = max(b['step_residual_seconds'] for b in base)
                for entry in result['scenarios'].values():
                    for count, plan in entry['plans'].items():
                        modeled_jobs = [load + setup + residual for load in plan['loads_seconds']]
                        shared_jobs = [load + setup + residual for load in plan['shared_python313_assignment_metrics']['loads_seconds']]
                        plan['shared_assignment_e2e_seconds'] = max(
                            timing_overhead['prefix_seconds'] + timing_overhead['dispatch_seconds'] + max(shared_jobs),
                            timing_overhead['other_parallel_jobs_observed_finish_seconds']) + timing_overhead['coverage_and_gate_tail_seconds']
                        plan['runner_seconds_with_observed_overhead'] = sum(modeled_jobs)
                        plan['rounded_runner_minutes'] = sum(math.ceil(value/60) for value in modeled_jobs)
                        plan['max_test_step_seconds_with_residual'] = plan['max_seconds'] + residual
                        plan['modeled_e2e_seconds_observed_other_job_queue'] = max(
                            timing_overhead['prefix_seconds'] + timing_overhead['dispatch_seconds'] + max(modeled_jobs),
                            timing_overhead['other_parallel_jobs_observed_finish_seconds']) + timing_overhead['coverage_and_gate_tail_seconds']
                        plan['modeled_e2e_seconds_no_other_job_queue'] = max(
                            timing_overhead['prefix_seconds'] + timing_overhead['dispatch_seconds'] + max(modeled_jobs),
                            timing_overhead['other_parallel_no_queue_finish_seconds']) + timing_overhead['coverage_and_gate_tail_seconds']
        record = dict(run_id=run_id, run_url=source['run']['html_url'], identity=source['identity'],
            baseline={v: d['baseline'] for v, d in source['versions'].items()},
            counts={v: len(d['nodes']) for v, d in source['versions'].items()},
            target_count=len(nodes), overhead=timing_overhead,
            historical_inventory=baseline_analysis, current_inventory=target_analysis)
        all_results.append(record)
        write_json(output / f'run-{run_id}.json', record)
        write_json(output / f'source-metadata-{run_id}.json', dict(run=source['run'], jobs=source['jobs']))
        write_json(output / f'provenance-{run_id}.json', dict(identity=source['identity'],
            run_url=source['run']['html_url'], artifacts=source['artifacts'], files=source['manifest'],
            timing_configuration_sha256={k: source['input_sha256'][k] for k in
                ('pyproject.toml', 'tools/ci/integration_checks.py')},
            module_source_sha256={k: v for k, v in source['input_sha256'].items() if k.startswith('tests/') and k.endswith('.py')}))
    rows = []
    target_modules = defaultdict(list)
    for node in nodes:
        target_modules[node.split('::')[0]].append(node)
    for source in sources:
        for version, data in source['versions'].items():
            for module in sorted(set(data['weights']) | set(target_modules)):
                measured_nodes = data['module_nodes'].get(module, [])
                current_nodes = target_modules.get(module, [])
                same_source = (not target.get('module_sha256') or
                               target['module_sha256'].get(module) == source['input_sha256'].get(module))
                status = ('MEASURED' if set(measured_nodes) == set(current_nodes) and same_source else
                          'PARTIAL_OR_CHANGED' if measured_nodes and current_nodes else
                          'HISTORICAL_ONLY' if measured_nodes else 'UNMEASURED')
                rows.append(dict(run_id=source['run']['id'], python=version, module=module,
                    evidence_status=status, measured_tests=len(measured_nodes), current_tests=len(current_nodes),
                    measured_seconds=round(data['weights'][module], 6) if measured_nodes else '',
                    **data['module_sources'].get(module, {})))
    with (output / 'module-timings.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=['run_id', 'python', 'module', 'evidence_status', 'measured_tests',
            'current_tests', 'measured_seconds', 'artifact', 'artifact_id', 'shard', 'job_id'])
        writer.writeheader()
        writer.writerows(rows)
    data = primary['versions']['3.13']
    weights = {m: round(value, 6) for m, value in data['weights'].items()
               if m in target_modules and set(target_modules[m]) == set(data['module_nodes'][m])
               and (not target.get('module_sha256') or
                    target['module_sha256'][m] == primary['input_sha256'].get(m))}
    write_json(output / 'candidate_module_timings.json', dict(schema=old['schema'],
        source_run=primary['run']['html_url'], source_artifact='extended-3.13-shard-{0,1,2}',
        subject_sha=primary['identity']['subject_sha'], python=data['python'], test_count=len(data['nodes']),
        module_seconds=weights, previous_source={k:v for k,v in old.items() if k != 'module_seconds'},
        timing_basis='sum of rounded JUnit testcase time; pytest default total setup/call/teardown, excluding session overhead',
        source_identity=primary['identity'], evidence_manifest=f'docs/validation/ci-perf-03/provenance-{primary["run"]["id"]}.json',
        measured_module_count=len(weights), measured_weight_test_count=sum(len(data['module_nodes'][m]) for m in weights),
        target_test_count=len(nodes),
        unmeasured_modules=sorted(set(target_modules)-set(weights))))
    write_json(output / 'target-inventory.json', {k: target[k] for k in
        ('current', 'historical_collected', 'historical_collection_errors', 'module_sha256', 'subject_sha', 'collection_python')
        if k in target})
    print(json.dumps([dict(run=r['run_id'], counts=r['counts'], target=r['target_count'], overhead=r['overhead']) for r in all_results], indent=2))


if __name__ == '__main__':
    main()
