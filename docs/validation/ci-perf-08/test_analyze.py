"""Synthetic parser/replay tests; never Runner performance evidence."""
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

HERE = Path(__file__).resolve().parent


def module(name):
    spec = importlib.util.spec_from_file_location('p08_' + name, HERE / f'{name}.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


analysis = module('analyze')
collection = module('collect')


def test_timeline_cost_rounds_each_job():
    run = {'created_at': '2026-11-15T00:00:00Z'}
    jobs = [dict(id=i, name=str(i), conclusion='success', labels=['windows-latest'],
                 started_at='2026-11-15T00:00:10Z', completed_at=end, steps=[])
            for i, end in enumerate(['2026-11-15T00:01:11Z', '2026-11-15T00:00:11Z'])]
    rows, costs = analysis.timeline(run, jobs)
    assert rows[0]['start_seconds'] == 10
    assert costs['runner_compute_seconds'] == 62
    assert costs['modeled_rounded_job_minutes'] == 3


@pytest.mark.parametrize('creator,state,url', [(8, 'success', 'run'), (7, 'failure', 'run'), (7, 'success', 'other')])
def test_gate_rejects_wrong_publisher_failure_or_run(creator, state, url):
    run = dict(created_at='2026-11-15T00:00:00Z', run_started_at='2026-11-15T00:00:00Z', head_sha='sha', html_url='run')
    status = dict(id=1, context='FactorLab trusted merge gate', target_url=url,
                  creator={'id': creator}, state=state, created_at='2026-11-15T00:15:00Z')
    with pytest.raises(ValueError):
        analysis.gate_status(run, {'github_pr_head_sha': 'sha'}, [status], 7)


@pytest.mark.parametrize('elapsed', [899, 900, 901])
def test_gate_uses_status_timestamp(elapsed):
    run = dict(created_at='2026-11-15T00:00:00Z', run_started_at='2026-11-15T00:00:00Z', head_sha='sha', html_url='run')
    stamp = f'2026-11-15T00:{elapsed // 60:02d}:{elapsed % 60:02d}Z'
    statuses = [dict(id=1, context='FactorLab trusted merge gate', target_url='run',
                     creator={'id': 7}, state='success', created_at=stamp)]
    assert analysis.gate_status(run, {'github_pr_head_sha': 'sha'}, statuses, 7)['e2e_seconds'] == elapsed
    statuses.append(dict(statuses[0], id=2, state='failure'))
    with pytest.raises(ValueError):
        analysis.gate_status(run, {'github_pr_head_sha': 'sha'}, statuses, 7)


@pytest.mark.parametrize('name', ['../escape', '/escape', '..\\escape'])
def test_artifact_rejects_unsafe_paths(tmp_path, name):
    archive = tmp_path / 'input.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        entry = zipfile.ZipInfo('placeholder')
        entry.filename = name
        bundle.writestr(entry, 'untrusted')
    with pytest.raises(ValueError):
        collection.extract(archive, tmp_path / 'unpacked')


def replay_fixture(tmp_path, monkeypatch):
    fixture = json.loads((HERE.parent / 'ci-perf-04/synthetic-receipts.json').read_bytes())
    source = tmp_path / 'receipts'
    for name, text in fixture['artifacts'].items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    weights = tmp_path / 'weights.json'
    weights.write_text(json.dumps({'module_seconds': {f'tests/{letter}.py': 4 - i for i, letter in enumerate('abcd')}}))
    monkeypatch.setattr(analysis.sharding, 'TIMINGS', weights)
    return source, fixture['identity']


def test_existing_four_shard_replay(tmp_path, monkeypatch):
    source, identity = replay_fixture(tmp_path, monkeypatch)
    analysis.sharding.aggregate_pr(source, tmp_path / 'verified', identity, 4)
    assert json.loads((tmp_path / 'verified/3.14/execution.json').read_bytes())['count'] == 4


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'attempt', 'sha', 'weights', 'skip', 'python'])
def test_existing_replay_rejects_corrupt_evidence(tmp_path, monkeypatch, mutation):
    source, identity = replay_fixture(tmp_path, monkeypatch)
    folder = source / '3.14/3/current'
    if mutation == 'missing':
        (folder / 'receipt.json').unlink()
    elif mutation == 'skip':
        (folder / 'tests.xml').write_text('<testsuite><testcase classname="tests.d" name="test_node"><skipped /></testcase></testsuite>')
    elif mutation in ('attempt', 'sha', 'python'):
        path = folder / 'receipt.json'
        data = json.loads(path.read_bytes())
        data[{'attempt': 'github_run_attempt', 'sha': 'subject_sha', 'python': 'python'}[mutation]] = 'wrong'
        path.write_text(json.dumps(data))
    else:
        path = folder / ('execution.json' if mutation == 'duplicate' else 'selection.json')
        data = json.loads(path.read_bytes())
        if mutation == 'duplicate':
            data['completed'] *= 2
        else:
            data['module_plan']['tests/d.py']['seconds'] = 100
        path.write_text(json.dumps(data))
    with pytest.raises((ValueError, FileNotFoundError)):
        analysis.sharding.aggregate_pr(source, tmp_path / 'verified', identity, 4)


def test_comparison_refuses_different_inventory():
    left = dict(shard_count=3, weights_sha256='w', versions={}, resources=dict(runner_compute_seconds=5, modeled_rounded_job_minutes=1))
    right = dict(left, shard_count=4, resources=dict(runner_compute_seconds=8, modeled_rounded_job_minutes=2))
    result = analysis.compare(left, right)
    assert not result['comparable']
    assert result['runner_compute_seconds_delta'] == 3


def test_cancelled_unscheduled_job_retains_unknown_cost():
    rows, cost = analysis.timeline({'created_at': '2026-11-15T00:00:00Z'}, [
        dict(id=1, name='cancelled', conclusion='cancelled', labels=[], steps=[],
             started_at=None, completed_at=None)])
    assert rows[0]['elapsed_seconds'] is None
    assert cost['untimed_jobs'] == [1]


def test_full_analyzer_keeps_evidence_but_never_self_authorizes(tmp_path, monkeypatch):
    import shutil
    source, identity = replay_fixture(tmp_path, monkeypatch)
    for version in ('3.13', '3.14'):
        for index in range(4):
            receipt_path = source / version / str(index) / 'current/receipt.json'
            receipt = json.loads(receipt_path.read_bytes())
            receipt['required_execution_observed'] = True
            receipt_path.write_text(json.dumps(receipt))
            xml = receipt_path.with_name('tests.xml')
            xml.write_text(xml.read_text().replace('<testsuite>', '<testsuite time="1">').replace('name="test_node"', 'name="test_node" time="1"'))
    analysis.sharding.aggregate_pr(source, source / 'verified-pr-current-coverage', identity, 4)
    names = []
    for version in ('3.13', '3.14'):
        for index in range(4):
            name = f'extended-{version}-shard-{index}'
            names.append(name)
            shutil.move(str(source / version / str(index)), str(source / name))
    start, middle, end = '2026-11-15T00:00:00Z', '2026-11-15T00:00:01Z', '2026-11-15T00:15:00Z'
    run = dict(id=123, run_attempt=2, head_sha='pr-head', repository={'full_name': 'Wind-Maker1001/FactorLab'},
               name='FactorLab PR validation', event='pull_request_target', status='completed', conclusion='success',
               created_at=start, run_started_at=start, html_url='run')
    job_names = ['Trusted risk selection', 'Fast Python 3.13', 'Extended lock ubuntu-latest',
                 'Extended lock windows-latest', 'Extended package Python 3.14', 'CI control review',
                 'Verify PR actual sharded coverage', 'FactorLab merge gate']
    job_names += [f'Extended software Python {v} shard {i}' for v in ('3.13', '3.14') for i in range(4)]
    jobs = [dict(id=i, name=name, conclusion='success', run_id=123, run_attempt=2,
                 started_at=middle, completed_at=end, labels=['windows-latest'], steps=[
                     dict(name='Install dependencies and package', started_at=middle, completed_at=middle, conclusion='success'),
                     dict(name='Run python tools/ci/integration_checks.py --suite current', started_at=middle, completed_at=end, conclusion='success')])
            for i, name in enumerate(job_names)]
    jobs[0]['started_at'], jobs[0]['completed_at'] = start, middle
    records = dict(run=run, jobs={'jobs': jobs}, artifacts={'artifacts': [{'name': name} for name in names]},
                   collection={'kind': 'SYNTHETIC_NOT_RUNNER_EVIDENCE'},
                   statuses=[dict(id=1, context='FactorLab trusted merge gate', target_url='run',
                                  creator={'id': 7}, state='success', created_at=end)])
    records['pr-risk/risk'] = dict(current=True, locking=True, package=True, review=True)
    records['fast-pr-receipts/financial-gates/receipt'] = dict(identity, status='PASS', exit_code=0,
        validation_inputs_unchanged=True, counts={'passed': 1})
    for name, record in records.items():
        path = source / f'{name}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
    monkeypatch.setattr(analysis.subprocess, 'check_output', lambda _: analysis.sharding.TIMINGS.read_bytes())
    result = analysis.analyze(source, tmp_path / 'analysis', 4, 7)
    assert result['coverage_replay'] == 'PASS'
    assert result['gate']['e2e_seconds'] == 900
    assert result['conclusion'] == 'NOT_YET_ACCEPTED'
    assert len(result['acceptance_blockers']) == 1
    assert result['versions']['3.14']['tests'] == 4
    assert (tmp_path / 'analysis/provenance.json').is_file()
