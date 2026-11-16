"""Rebaseline fixed CI observations without executing tests or changing live weights."""
import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.ci.sharding import partition, module_plan

SPEC = importlib.util.spec_from_file_location('p03_profile', ROOT / 'docs/validation/ci-perf-03/profile.py')
p03 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p03)
VERSIONS = ('3.13', '3.14')
LAST_MERGE = '2026-11-15T16:47:29Z'


def write(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def assignment(nodes, weights, count):
    shards = partition(nodes, weights, count)
    return {node.split('::')[0]: i for i, shard in enumerate(shards) for node in shard}


def evaluate(plan, costs, count):
    loads = [0.0] * count
    for module, shard in plan.items():
        loads[shard] += costs[module]
    total = sum(loads)
    lower = max(total / count, max(costs.values()))
    return dict(loads_seconds=loads, max_seconds=max(loads), total_seconds=total,
                lower_bound_seconds=lower, bound_gap_seconds=max(loads)-lower,
                imbalance=max(loads)/(total/count)-1)


def matching_weights(data, target, hashes, source_hashes, runtime_matches=True):
    measured, states = {}, {}
    modules = defaultdict(list)
    for node in target:
        modules[node.split('::')[0]].append(node)
    for module, nodes in sorted(modules.items()):
        observed = data['module_nodes'].get(module, [])
        match = (Counter(nodes) == Counter(observed) and hashes[module] == source_hashes.get(module)
                 and runtime_matches)
        status = 'MEASURED' if match else 'PARTIAL' if observed else 'UNMEASURED'
        if match:
            measured[module] = data['weights'][module]
        states[module] = dict(status=status, measured_tests=len(observed), target_tests=len(nodes),
                              observed_seconds=data['weights'].get(module),
                              nodes_match=Counter(nodes) == Counter(observed),
                              source_match=hashes[module] == source_hashes.get(module))
    return measured, states


def ranges(values):
    return [min(values), max(values)]


def timing_model(source, version, plan, costs, count):
    """Observed-envelope scenarios, not confidence intervals or four-shard observations."""
    jobs, run = source['jobs']['jobs'], source['run']
    risk = next(j for j in jobs if j['name'] == 'Trusted risk selection')
    coverage = next(j for j in jobs if j['name'].startswith('Verify PR actual'))
    gate = next(j for j in jobs if j['name'] == 'FactorLab merge gate')
    extended = [j for j in jobs if j['name'].startswith('Extended software Python')]
    other = [j for j in jobs if j not in extended and j not in (risk, coverage, gate)]
    baseline = source['versions'][version]['baseline']
    residual = ranges([b['step_residual_seconds'] for b in baseline])
    preparation = ranges([b['non_test_job_seconds'] for b in baseline])
    dispatch = ranges([p03.seconds(risk['completed_at'], j['started_at']) for j in extended])
    prefix = p03.seconds(run['created_at'], risk['completed_at'])
    coverage_duration = p03.seconds(coverage['started_at'], coverage['completed_at'])
    gate_duration = p03.seconds(gate['started_at'], gate['completed_at'])
    gate_ready = max(j['completed_at'] for j in jobs if j != gate)
    gate_wait = p03.seconds(gate_ready, gate['started_at'])
    latest_extended = max(j['completed_at'] for j in extended)
    coverage_wait = p03.seconds(latest_extended, coverage['started_at'])
    other_finish = max(p03.seconds(run['created_at'], j['completed_at']) for j in other)
    body = evaluate(plan, costs, count)['max_seconds']
    step = [body+r for r in residual]
    job = [step[i]+preparation[i] for i in range(2)]
    e2e = [max(prefix+dispatch[i]+job[i]+coverage_wait+coverage_duration, other_finish)+
            gate_wait+gate_duration for i in range(2)]
    return dict(kind='OBSERVED_OVERHEAD_ENVELOPE_NOT_CONFIDENCE_INTERVAL',
                test_body_seconds=body, test_step_seconds=step, job_seconds=job,
                workflow_e2e_seconds=e2e, residual_seconds=residual,
                non_test_job_seconds=preparation, dispatch_seconds=dispatch,
                prefix_seconds=prefix, coverage_wait_seconds=coverage_wait,
                coverage_gate_tail_seconds=coverage_duration+gate_wait+gate_duration,
                coverage_duration_seconds=coverage_duration, gate_dispatch_seconds=gate_wait,
                gate_duration_seconds=gate_duration, other_parallel_finish_seconds=other_finish,
                budget_seconds=900, e2e_margin_seconds=[900-e2e[1], 900-e2e[0]],
                assumptions=['Serial additive JUnit costs; fixture attribution may change.',
                             'Observed same-run overhead envelope; fourth-runner queue unmeasured.',
                             'Other parallel branches held at observed finish; no new CI run.'])


def normalized_source(directory, runid):
    metadata = p03.load(directory / f'sources-{runid}.json')
    analysis = p03.load(directory / f'analysis-{runid}.json')
    versions = {}
    for version, data in analysis['versions'].items():
        observed_nodes = p03.load(directory/data['inventory_reference'])['current']
        module_nodes = defaultdict(list)
        for node in observed_nodes:
            module_nodes[node.split('::')[0]].append(node)
        module_sources = {}
        with (directory/'module-timings.csv').open(encoding='utf-8', newline='') as stream:
            for row in csv.DictReader(stream):
                if row['run_id']==runid and row['python']==version and row.get('artifact'):
                    module_sources[row['module']] = dict(artifact=row['artifact'],
                        artifact_id=int(row['artifact_id']), shard=int(row['shard']), job_id=int(row['job_id']))
        versions[version] = dict(nodes=observed_nodes, module_nodes=dict(module_nodes),
            weights=data['observed_weights'], module_sources=module_sources,
            baseline=data['baseline'], python=data['baseline'][0]['python'])
    return dict(run=metadata['run'], jobs=metadata['jobs'], artifacts={a['name']:a for a in metadata['artifacts'].values()},
                identity=metadata['identity'], manifest=metadata['source_files'], versions=versions,
                input_sha256=p03.load(directory/f'validation-inputs-{runid}.json'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--evidence', type=Path, nargs='+', help='Last run is primary; never merge observations')
    mode.add_argument('--normalized', type=Path, help='Offline replay from committed observation columns; ignores saved predictions')
    parser.add_argument('--target', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    old = p03.load((args.normalized if args.normalized else Path(__file__).resolve().parent)
                   / 'previous_module_timings.json')
    if args.normalized:
        target = p03.load(args.normalized/'target-inventory.json')
        runids = [s['run_id'] for s in p03.load(args.normalized/'sources.json')['sources']]
        sources = [normalized_source(args.normalized, runid) for runid in runids]
        directories = [None] * len(sources)
    else:
        if args.target is None:
            parser.error('--evidence requires --target')
        target = p03.load(args.target)
        sources = [p03.read_run(directory, old['module_seconds']) for directory in args.evidence]
        directories = args.evidence
    nodes = target['current']
    p03.require(nodes and len(nodes) == len(set(nodes)), 'duplicate/empty target nodes')
    if args.normalized:
        runtime_hashes = target['runtime_sha256']
    else:
        source_paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        runtime_hashes = {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in source_paths
                          if p and ((p.endswith('.py') and not p.startswith('docs/'))
                                    or p in ('pyproject.toml', 'uv.lock', 'requirements.txt'))}
        target['runtime_sha256'] = runtime_hashes
    write(args.output/'previous_module_timings.json', old)
    outputs, manifest, rows = {}, [], []
    for source, directory in zip(sources, directories):
        runid = str(source['run']['id'])
        versions, candidates = {}, {}
        changed_runtime = [p for p,h in runtime_hashes.items() if source['input_sha256'].get(p) != h]
        changed_runtime += [p for p in source['input_sha256'] if p.endswith('.py')
                            and not p.startswith('docs/') and p not in runtime_hashes]
        for version in VERSIONS:
            data = source['versions'][version]
            measured, states = matching_weights(data, nodes, target['module_sha256'], source['input_sha256'], not changed_runtime)
            costs = {m: p['seconds'] for m, p in module_plan(nodes, measured).items()}
            plans = {}
            for count in (3, 4):
                plan = assignment(nodes, measured, count)
                prediction = timing_model(source, version, plan, costs, count)
                prediction['input_status'] = 'ALL_MODULES_MEASURED' if len(measured)==len(states) else 'INCOMPLETE_INPUT_ESTIMATE'
                prediction['budget_judgment'] = ('MODEL_WITHIN_BUDGET' if prediction['workflow_e2e_seconds'][1] <= 900
                    else 'MODEL_EXCEEDS_BUDGET') if len(measured)==len(states) else 'UNDETERMINED_MISSING_MEASUREMENTS'
                plans[str(count)] = dict(assignments=plan, metrics=evaluate(plan, costs, count), prediction=prediction)
            versions[version] = dict(inventory_reference=f'inventory-{runid}.json', node_count=len(data['nodes']),
                module_states=states, observed_weights=data['weights'],
                candidate_weights=measured, modeled_costs=costs, baseline=data['baseline'], plans=plans,
                measured_modules=len(measured), partial_modules=sum(s['status']=='PARTIAL' for s in states.values()),
                unmeasured_modules=sum(s['status']=='UNMEASURED' for s in states.values()),
                runtime_changes=changed_runtime,
                observed_inventory_plans={str(c):dict(assignments=assignment(data['nodes'],data['weights'],c),
                    metrics=evaluate(assignment(data['nodes'],data['weights'],c),data['weights'],c)) for c in (3,4)})
            candidates[version] = dict(source_run=source['run']['html_url'],
                source_artifact=[f'extended-{version}-shard-{i}' for i in range(3)],
                subject_sha=source['identity']['subject_sha'], python=data['python'],
                test_count=sum(s['target_tests'] for s in states.values() if s['status']=='MEASURED'),
                target_test_count=len(nodes), module_seconds=measured,
                target_subject_sha=target['subject_sha'], source_identity=source['identity'],
                timing_basis='JUnit testcase time: setup+call+teardown; stages not separately observed',
                status='CANDIDATE_NOT_ACTIVE_PREMERGE_OBSERVATION',
                unmeasured_modules=[m for m,s in states.items() if s['status']!='MEASURED'])
            for module, state in states.items():
                rows.append(dict(run_id=runid, python=version, module=module, **state,
                                 setup_seconds=None, call_seconds=None, teardown_seconds=None,
                                 phase_status='UNMEASURED', timing_basis='setup+call+teardown',
                                 **data['module_sources'].get(module, {})))
            dependency_path = (args.normalized / f'dependencies-{runid}-{version}.txt') if directory is None else (
                directory/f'extended-{version}-shard-0'/'current'/'dependencies.txt')
            (args.output/f'dependencies-{runid}-{version}.txt').write_bytes(dependency_path.read_bytes())
        shared = {}
        for count in (3, 4):
            comparisons = {}
            for name, weights in [('shared_3.13', versions['3.13']['candidate_weights']),
                                  ('shared_3.14', versions['3.14']['candidate_weights']),
                                  ('existing_v1', old['module_seconds'])]:
                plan = assignment(nodes, weights, count)
                comparisons[name] = dict(assignments=plan, evaluated={v:evaluate(plan, versions[v]['modeled_costs'], count) for v in VERSIONS})
            shared[str(count)] = comparisons
        write(args.output/f'inventory-{runid}.json', dict(current=source['versions']['3.13']['nodes'],
            python_versions=list(VERSIONS), same_inventory_verified=True))
        metadata = dict(run=source['run'], jobs=source['jobs'], artifacts=source['artifacts'],
                        identity=source['identity'], evidence_temporal_status='BEFORE_FINAL_MERGE',
                        last_merge_time=LAST_MERGE, source_files=source['manifest'])
        statuses = p03.load((directory/'commit-statuses.json') if directory is not None else
                            args.normalized/f'commit-statuses-{runid}.json')
        success = [s for s in statuses if s['context']=='FactorLab trusted merge gate'
                   and s['state']=='success' and s['target_url']==source['run']['html_url']
                   and s['creator']['login']=='factorlab-ci-gate[bot]']
        p03.require(len(success)==1, 'missing or ambiguous protected App success status')
        metadata['protected_app_status'] = success[0]
        metadata['protected_status_e2e_seconds'] = p03.seconds(source['run']['created_at'], success[0]['created_at'])
        write(args.output/f'commit-statuses-{runid}.json', statuses)
        write(args.output / f'sources-{runid}.json', metadata)
        joint = {str(c):[max(versions[v]['plans'][str(c)]['prediction']['workflow_e2e_seconds'][i]
                                  for v in VERSIONS) for i in range(2)] for c in (3,4)}
        write(args.output / f'analysis-{runid}.json', dict(versions=versions, shared=shared,
              two_version_e2e_envelope_seconds=joint,
              observed_e2e_seconds=p03.seconds(source['run']['created_at'],max(j['completed_at'] for j in source['jobs']['jobs'])),
              versions_must_not_be_added_for_e2e=True))
        candidate = dict(schema='FACTORLAB_MEASURED_MODULE_TIMINGS_V2', versions=candidates,
                         evidence_manifest=f'sources-{runid}.json', analysis_target=target['subject_sha'])
        write(args.output / f'candidate-{runid}.json', candidate)
        if source is sources[-1]:
            write(args.output / 'candidate_module_timings.json', candidate)
            for v in VERSIONS:
                write(args.output / f'candidate-python-{v}.json', dict(schema='FACTORLAB_MEASURED_MODULE_TIMINGS_V1', **candidates[v]))
        manifest.append(dict(run_id=runid, attempt=source['run']['run_attempt'], evidence_status='PREMERGE',
                             url=source['run']['html_url'], source=f'sources-{runid}.json'))
        # Normalized receipts share one frozen-input map and omit repeated execution arrays.
        if directory is None:
            receipts = p03.load(args.normalized/f'receipts-{runid}.json')
            risk_data = p03.load(args.normalized/f'risk-{runid}.json')
        else:
            receipts = {}
            for path in sorted(directory.rglob('*.json')):
                if path.name in ('receipt.json', 'execution.json'):
                    record = p03.load(path)
                    if record.get('validation_input_sha256') == source['input_sha256']:
                        record = dict(record)
                        record.pop('validation_input_sha256')
                        record['validation_inputs_reference'] = 'validation-inputs-' + runid + '.json'
                    record = dict(record)
                    for key in ('started', 'completed'):
                        if isinstance(record.get(key), list):
                            record[key + '_count'] = len(record.pop(key))
                    receipts[path.relative_to(directory).as_posix()] = record
            risk = list((directory / 'pr-risk').rglob('*.json'))
            risk_data = {p.relative_to(directory/'pr-risk').as_posix():p03.load(p) for p in risk}
        risk_plan, sealed = risk_data['risk.json'], risk_data['identity.json']
        p03.require(all(risk_plan[k] for k in ('current', 'locking', 'package', 'review')), 'not a full high-risk run')
        for key, receipt_key in (('run_id','github_run_id'), ('run_attempt','github_run_attempt'),
                                 ('base','trusted_base_sha'), ('pr_head','github_pr_head_sha'),
                                 ('subject','subject_sha'), ('event_sha','github_event_sha')):
            p03.require(sealed[key] == source['identity'][receipt_key], 'sealed risk identity mismatch')
        if directory is not None:
            p03.require(hashlib.sha256((directory/'pr-risk/risk.json').read_bytes()).hexdigest() == sealed['risk_sha256'],
                        'risk file differs from sealed identity')
        write(args.output / f'validation-inputs-{runid}.json', source['input_sha256'])
        write(args.output / f'receipts-{runid}.json', receipts)
        write(args.output / f'risk-{runid}.json', risk_data)
        outputs[runid] = dict(counts={v:dict(tests=versions[v]['node_count'], modules=len(versions[v]['observed_weights']),
                             measured=versions[v]['measured_modules'],partial=versions[v]['partial_modules'],
                             unmeasured=versions[v]['unmeasured_modules']) for v in VERSIONS})
    fields = sorted({key for row in rows for key in row})
    with (args.output / 'module-timings.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)
    write(args.output / 'target-inventory.json', {k:target[k] for k in
          ('current', 'module_sha256', 'subject_sha', 'collection_python', 'runtime_sha256')})
    write(args.output / 'sources.json', dict(schema='FACTORLAB_CI_PERF_05_SOURCES_V1', sources=manifest,
          primary_run=str(sources[-1]['run']['id']), final_merge_time=LAST_MERGE,
          analysis_subject_sha=target['subject_sha'], postmerge_run_available=False,
          previous_weights_source=old['source_run']))
    print(json.dumps(outputs, indent=2))


if __name__ == '__main__':
    main()
