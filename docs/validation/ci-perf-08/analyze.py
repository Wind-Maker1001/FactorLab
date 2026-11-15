"""Offline performance evidence analysis; not a trusted merge-gate publisher."""
import argparse
import csv
from datetime import datetime
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.ci import sharding
from tools.ci.validation_v2 import outcomes

_spec = importlib.util.spec_from_file_location('p03_profile', ROOT / 'docs/validation/ci-perf-03/profile.py')
profile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(profile)
IDENTITY = ('subject_sha', 'github_pr_head_sha', 'trusted_base_sha', 'github_event_sha',
            'github_run_id', 'github_run_attempt')


def load(path):
    return json.loads(path.read_bytes())


def require(value, message):
    if not value:
        raise ValueError(message)


def duration(start, end):
    value = (datetime.fromisoformat(end.replace('Z', '+00:00')) -
             datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds()
    require(value >= 0, 'negative timestamp interval')
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def timeline(run, jobs):
    rows = []
    for job in jobs:
        if job['conclusion'] == 'skipped':
            continue
        rows.append(dict(job_id=job['id'], job=job['name'], step='', conclusion=job['conclusion'],
                         start_seconds=(duration(run['created_at'], job['started_at']) if job.get('started_at') else None),
                         elapsed_seconds=(duration(job['started_at'], job['completed_at'])
                                          if job.get('started_at') and job.get('completed_at') else None),
                         labels=json.dumps(job['labels'])))
        for step in job['steps']:
            if step.get('started_at') and step.get('completed_at'):
                rows.append(dict(job_id=job['id'], job=job['name'], step=step['name'],
                                 conclusion=step['conclusion'],
                                 start_seconds=duration(run['created_at'], step['started_at']),
                                 elapsed_seconds=duration(step['started_at'], step['completed_at']),
                                 labels=json.dumps(job['labels'])))
    timed_jobs = [row for row in rows if not row['step'] and row['elapsed_seconds'] is not None]
    compute = sum(row['elapsed_seconds'] for row in timed_jobs)
    minutes = sum(math.ceil(row['elapsed_seconds'] / 60) for row in timed_jobs)
    return rows, dict(runner_compute_seconds=compute, modeled_rounded_job_minutes=minutes,
                      untimed_jobs=[row['job_id'] for row in rows if not row['step'] and row['elapsed_seconds'] is None],
                      actual_billed_minutes=None,
                      model='sum ceil(job elapsed/60), no OS multiplier or account discount')


def gate_status(run, identity, statuses, publisher_id):
    # Status API exposes the App bot user, not an app.id. The expected bot ID must
    # be independently resolved from the protected publisher before analysis.
    matching = [s for s in statuses if s['context'] == 'FactorLab trusted merge gate' and
                s.get('target_url') == run['html_url'] and
                s['creator']['id'] == publisher_id]
    require(matching, 'missing protected publisher status for this run')
    latest = max(matching, key=lambda s: (s['created_at'], s['id']))
    require(latest['state'] == 'success', 'latest protected status is not success')
    require(identity['github_pr_head_sha'] == run['head_sha'], 'status SHA differs from receipt')
    require(duration(run['created_at'], latest['created_at']) >=
            duration(run['created_at'], run['run_started_at']), 'status predates run attempt')
    return dict(id=latest['id'], creator_id=publisher_id, created_at=latest['created_at'],
                e2e_seconds=duration(run['created_at'], latest['created_at']))


def analyze(source, output, count, publisher_id):
    run = load(source / 'run.json')
    jobs = load(source / 'jobs.json')['jobs']
    require(run['status'] == 'completed', 'attempt still running')
    require(run['repository']['full_name'] == 'Wind-Maker1001/FactorLab', 'wrong repository')
    require(run['name'] == 'FactorLab PR validation' and run['event'] == 'pull_request_target',
            'not the trusted PR workflow')
    require(len({j['id'] for j in jobs}) == len(jobs), 'repeated job metadata')
    require(all(j['run_id'] == run['id'] and j['run_attempt'] == run['run_attempt'] for j in jobs),
            'mixed job run/attempt')
    output.mkdir(parents=True, exist_ok=False)
    rows, cost = timeline(run, jobs)
    with (output / 'waterfall.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=['job_id', 'job', 'step', 'conclusion',
                                                   'start_seconds', 'elapsed_seconds', 'labels'])
        writer.writeheader()
        writer.writerows(rows)
    summary = dict(schema='CI_PERF_08_ANALYSIS_V1', run_id=run['id'], attempt=run['run_attempt'],
                   head_sha=run['head_sha'], run_url=run['html_url'], shard_count=count,
                   actions_conclusion=run['conclusion'], resources=cost,
                   conclusion='NOT_YET_ACCEPTED', acceptance_blockers=[], versions={})
    finished = [row for row in rows if not row['step'] and row['job'] != 'FactorLab merge gate'
                and row['elapsed_seconds'] is not None]
    if finished:
        summary['last_pre_gate_job'] = max(finished, key=lambda row: row['start_seconds'] + row['elapsed_seconds'])
    blockers = summary['acceptance_blockers']
    if run['conclusion'] != 'success':
        blockers.append('Actions attempt did not succeed')
    # Preserve timings/costs for negative runs, even when receipt replay fails.
    workspace = tempfile.TemporaryDirectory(prefix='ci-p08-replay-')
    try:
        first = load(source / 'extended-3.13-shard-0/current/receipt.json')
        identity = {key: first[key] for key in IDENTITY}
        require(identity['github_run_id'] == str(run['id']) and
                identity['github_run_attempt'] == str(run['run_attempt']) and
                identity['github_pr_head_sha'] == run['head_sha'], 'receipt run identity mismatch')
        summary['identity'] = identity
        risk = load(source / 'pr-risk/risk.json')
        require(all(type(risk[key]) is bool for key in ('current', 'locking', 'package', 'review')),
                'invalid risk flags')
        require(all(risk[key] for key in ('current', 'locking', 'package')), 'not a complete high-risk run')
        required_jobs = ['Trusted risk selection', 'Fast Python 3.13',
                         'Extended lock ubuntu-latest', 'Extended lock windows-latest',
                         'Extended package Python 3.14', 'Verify PR actual sharded coverage',
                         'FactorLab merge gate']
        if risk['review']:
            required_jobs.append('CI control review')
        for name in required_jobs:
            matches = [j for j in jobs if j['name'] == name]
            require(len(matches) == 1 and matches[0]['conclusion'] == 'success', 'missing/failed required job: ' + name)
        summary['required_jobs'] = {j['name']: j['conclusion'] for j in jobs}
        summary['risk_flags'] = {key: risk[key] for key in ('current', 'locking', 'package', 'review')}
        financial = load(source / 'fast-pr-receipts/financial-gates/receipt.json')
        summary['financial_receipt'] = {key: financial[key] for key in ('status', 'counts', 'trusted_base_sha', 'subject_sha')}
        require(financial['status'] == 'PASS' and financial['exit_code'] == 0 and
                financial['validation_inputs_unchanged'] and all(financial[k] == identity[k] for k in IDENTITY if k != 'trusted_base_sha'),
                'financial receipt failed or belongs to another identity')
        trusted_weights = subprocess.check_output(
            ['git', '-C', str(ROOT), 'show', f'{identity["trusted_base_sha"]}:tools/ci/current_module_timings.json'])
        local_weights = sharding.TIMINGS.read_bytes()
        require(json.loads(trusted_weights) == json.loads(local_weights),
                'analyzer weights differ from trusted base; use matching checkout')
        summary['weights_sha256'] = hashlib.sha256(trusted_weights).hexdigest()
        summary['weights_canonical_sha256'] = digest(json.loads(trusted_weights))
        summary['validation_inputs_sha256'] = digest(first['validation_input_sha256'])
        artifact_names = [a['name'] for a in load(source / 'artifacts.json')['artifacts']]
        expected_names = {f'extended-{v}-shard-{i}' for v in ('3.13', '3.14') for i in range(count)}
        observed_names = [name for name in artifact_names if name.startswith('extended-3.')]
        require(set(observed_names) == expected_names and len(observed_names) == len(expected_names),
                'missing, duplicate or unexpected shard artifacts')
        replay = Path(workspace.name) / 'replay-input'
        replay.mkdir()
        # aggregate_pr expects version/shard/current; copies only data, never code.
        import shutil
        for version in ('3.13', '3.14'):
            for index in range(count):
                shutil.copytree(source / f'extended-{version}-shard-{index}', replay / version / str(index))
        sharding.aggregate_pr(replay, Path(workspace.name) / 'replayed-coverage', identity, count)
        for version in ('3.13', '3.14'):
            records = []
            for index in range(count):
                folder = replay / version / str(index) / 'current'
                receipt, selection = (load(folder / f'{n}.json') for n in ('receipt', 'selection'))
                require(receipt['required_execution_observed'], 'required execution not observed')
                expected_counts = dict(tests=len(selection['selected']), passed=len(selection['selected']),
                                       failures=0, errors=0, skipped=0)
                require(receipt['counts'] == expected_counts, 'receipt counts differ')
                times, suite = profile.junit_times(folder / 'tests.xml', selection['selected'])
                job = [j for j in jobs if j['name'] == f'Extended software Python {version} shard {index}']
                require(len(job) == 1 and job[0]['conclusion'] == 'success', 'missing/failed shard job')
                test_steps = [s for s in job[0]['steps'] if 'integration_checks.py --suite current' in s['name']]
                require(len(test_steps) == 1 and test_steps[0]['conclusion'] == 'success', 'missing/failed pytest wrapper step')
                test_step = test_steps[0]
                setup = [s for s in job[0]['steps'] if s['name'] == 'Install dependencies and package']
                require(len(setup) == 1, 'missing/duplicate installation step')
                risk_jobs = [j for j in jobs if j['name'] == 'Trusted risk selection']
                require(len(risk_jobs) == 1 and risk_jobs[0]['conclusion'] == 'success', 'missing/failed risk selection')
                records.append(dict(shard=index, nodes=selection['selected'], python=receipt['python'],
                                    dependencies_sha256=receipt['dependencies_sha256'],
                                    installation_seconds=duration(setup[0]['started_at'], setup[0]['completed_at']),
                                    test_step_seconds=duration(test_step['started_at'], test_step['completed_at']),
                                    pre_test_job_seconds=duration(job[0]['started_at'], test_step['started_at']),
                                    risk_finish_to_job_start_seconds=duration(risk_jobs[0]['completed_at'], job[0]['started_at']),
                                    pytest_testcase_seconds=sum(times.values()), pytest_suite_seconds=suite,
                                    wrapper_seconds=receipt['elapsed_seconds'], job_id=job[0]['id'],
                                    module_plan_sha256=digest(selection['module_plan'])))
            actual = source / 'verified-pr-current-coverage' / version
            receipt, execution = (load(actual / f'{n}.json') for n in ('receipt', 'execution'))
            current = load(replay / version / '0/current/selection.json')['current']
            require(all(receipt[k] == identity[k] for k in IDENTITY) and receipt['status'] == 'PASS' and
                    execution['status'] == 'PASS' and execution['count'] == len(current) and
                    execution['shard_count'] == count and execution['actual_disjoint'] and
                    execution['actual_union_equals_full_inventory'], 'actual coverage receipt mismatch')
            require(outcomes(actual / 'tests.xml', current) == {n: 'PASS' for n in current},
                    'actual aggregate JUnit mismatch')
            summary['versions'][version] = dict(tests=len(current), shards=records,
                                                inventory_sha256=digest(sorted(current)))
        summary['coverage_replay'] = 'PASS'
        gate = gate_status(run, identity, load(source / 'statuses.json'), publisher_id)
        gate_jobs = [j for j in jobs if j['name'] == 'FactorLab merge gate']
        require(len(gate_jobs) == 1 and gate_jobs[0]['conclusion'] == 'success', 'missing/failed gate job')
        require(duration(gate_jobs[0]['started_at'], gate['created_at']) >= 0 and
                duration(gate['created_at'], gate_jobs[0]['completed_at']) >= 0,
                'protected status falls outside this attempt gate job')
        summary['gate'] = gate
        seconds = summary['gate']['e2e_seconds']
        summary['over_900_seconds'] = max(0, seconds - 900)
        if seconds > 900:
            blockers.append(f'end-to-end exceeds 900 seconds by {seconds - 900:.3f}')
    except (ValueError, KeyError, FileNotFoundError, subprocess.CalledProcessError) as error:
        blockers.append(str(error))
    finally:
        workspace.cleanup()
    if count != 4:
        blockers.append('three-shard reference, not a four-shard acceptance run')
    blockers.append('formal CI-PERF-06/control authorization, unchanged scope and financial/history review required')
    # This research analyzer cannot attest formal review or authorize a global switch.
    # REPORT.md is the explicit human acceptance record; no automatic certification.
    provenance = {name: load(source / f'{name}.json') for name in ('run', 'jobs', 'artifacts', 'statuses', 'collection')}
    provenance['files'] = [dict(path=path.relative_to(source).as_posix(), bytes=path.stat().st_size,
                                sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                           for path in sorted(source.rglob('*')) if path.is_file() and path.suffix != '.zip']
    (output / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n', encoding='utf-8')
    (output / 'analysis.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    return summary


def compare(left, right):
    reasons = []
    if left.get('weights_sha256') != right.get('weights_sha256') or not left.get('weights_sha256'):
        reasons.append('trusted weights unavailable or differ')
    if any(left['versions'].get(v, {}).get('inventory_sha256') !=
           right['versions'].get(v, {}).get('inventory_sha256') or
           not left['versions'].get(v, {}).get('inventory_sha256') for v in ('3.13', '3.14')):
        reasons.append('test inventories unavailable or differ')
    if (not left.get('validation_inputs_sha256') or
            left.get('validation_inputs_sha256') != right.get('validation_inputs_sha256')):
        reasons.append('frozen validation inputs unavailable or differ')
    for version in ('3.13', '3.14'):
        left_shards = left['versions'].get(version, {}).get('shards', [])
        right_shards = right['versions'].get(version, {}).get('shards', [])
        runtime = lambda shards: {(s['python'], s['dependencies_sha256']) for s in shards}
        if not left_shards or not right_shards or runtime(left_shards) != runtime(right_shards):
            reasons.append(version + ' runtime/dependencies unavailable or differ')
    if left.get('coverage_replay') != 'PASS' or right.get('coverage_replay') != 'PASS':
        reasons.append('successful exact coverage replay required for both runs')
    if (left['shard_count'], right['shard_count']) != (3, 4):
        reasons.append('expected three-shard baseline and four-shard trial')
    return dict(comparable=not reasons, reasons=reasons,
                runner_compute_seconds_delta=right['resources']['runner_compute_seconds'] - left['resources']['runner_compute_seconds'],
                modeled_rounded_job_minutes_delta=right['resources']['modeled_rounded_job_minutes'] - left['resources']['modeled_rounded_job_minutes'],
                e2e_seconds_delta=(right['gate']['e2e_seconds'] - left['gate']['e2e_seconds']
                                   if 'gate' in left and 'gate' in right else None))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--shards', type=int, choices=(3, 4), required=True)
    parser.add_argument('--publisher-bot-id', type=int, required=True)
    parser.add_argument('--baseline', type=Path)
    args = parser.parse_args()
    result = analyze(args.source, args.output, args.shards, args.publisher_bot_id)
    if args.baseline:
        (args.output / 'comparison.json').write_text(json.dumps(compare(load(args.baseline), result), indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('run_id', 'conclusion', 'acceptance_blockers')}, indent=2))


if __name__ == '__main__':
    main()
