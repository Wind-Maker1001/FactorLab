"""Bounded candidate-only publication acceptance with per-round file evidence."""
import argparse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import hashlib
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--thread-rounds', type=int, default=100)
    p.add_argument('--process-rounds', type=int, default=12)
    a = p.parse_args()
    if not 1 <= a.thread_rounds <= 300 or not 1 <= a.process_rounds <= 30:
        p.error('thread rounds 1..300, process rounds 1..30')
    output = a.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sys.path[:0] = [str(ROOT/'src'), str(ROOT)]
    tests = importlib.import_module('tests.research.test_signal_lock_native')
    rounds = []
    scopes = [('single', 1, False, 1), ('threads_same_date', 4, False, a.thread_rounds),
              ('threads_different_dates', 4, True, 10),
              ('processes_same_date', 4, False, a.process_rounds),
              ('processes_different_dates', 4, True, 4)]
    for scope, workers, different, count in scopes:
        pool_class = ProcessPoolExecutor if scope.startswith('processes') else ThreadPoolExecutor
        options = {'mp_context': multiprocessing.get_context('spawn')} if pool_class is ProcessPoolExecutor else {}
        with pool_class(max_workers=workers, **options) as pool:
            for iteration in range(count):
                root = output/scope/('%03d' % iteration)
                started = time.perf_counter()
                results, failure = [], None
                try:
                    results = list(pool.map(tests.publish, [(root, i, i if different else 0) for i in range(workers)]))
                    expected = ['v000001']*workers if different else ['v%06d' % (i+1) for i in range(workers)]
                    assert sorted(v for _, v, _ in results) == expected
                    assert len({(d, v) for d, v, _ in results}) == workers
                    for manifest in root.rglob('manifest.json'):
                        tests.load_signal_record(manifest.parent, require_completion=False)
                except Exception as exc:
                    failure = {'type': type(exc).__name__, 'errno': getattr(exc, 'errno', None),
                               'winerror': getattr(exc, 'winerror', None), 'ntstatus': getattr(exc, 'ntstatus', None)}
                files = {f.relative_to(root).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
                         for f in root.rglob('*') if f.is_file()}
                locks = [n for n in files if n.endswith('.lock')]
                valid = failure is None and len(files) == workers*4 and not locks
                rounds.append(dict(scope=scope, iteration=iteration, executions=workers,
                                   status='PASS' if valid else 'FAIL', seconds=time.perf_counter()-started,
                                   file_count=len(files), files=files, publications=results,
                                   unique_versions=len({(d, v) for d, v, _ in results}),
                                   hash_integrity='PASS' if valid else 'FAIL', lock_residue=locks, error=failure))
    command = [sys.executable, '-B', '-m', 'pytest', 'tests/research/test_signal_lock_native.py', '-q',
               '--basetemp='+str(output/'fault-tmp'), '-o', 'cache_dir='+str(output/'cache'),
               '--junitxml='+str(output/'faults.xml')]
    started = time.perf_counter()
    with (output/'faults.log').open('wb') as log:
        code = subprocess.call(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    cases = list(ET.parse(output/'faults.xml').getroot().iter('testcase')) if (output/'faults.xml').exists() else []
    faults = dict(exit_code=code, seconds=time.perf_counter()-started, tests=len(cases),
                  passed=sum(all(n.find(t) is None for t in ('error','failure','skipped')) for n in cases),
                  failed=sum(n.find('failure') is not None for n in cases),
                  errors=sum(n.find('error') is not None for n in cases),
                  skipped=sum(n.find('skipped') is not None for n in cases))
    result = dict(schema='FACTORLAB_LOCK_CONCURRENCY_ACCEPTANCE_V2',
                  status='PASS' if code == 0 and all(r['status']=='PASS' for r in rounds) else 'FAIL',
                  subject_sha=subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
                  github_sha=os.environ.get('GITHUB_SHA'), pr_head=os.environ.get('FACTORLAB_PR_HEAD_SHA'),
                  python=sys.version, platform=platform.platform(), rounds=rounds,
                  round_count=len(rounds), passed_rounds=sum(r['status']=='PASS' for r in rounds),
                  failed_rounds=sum(r['status']!='PASS' for r in rounds), faults=faults,
                  source_sha256={name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                                 ('src/factorlab/forward/signal_recorder.py','src/factorlab/forward/_windows_lock.py',
                                  'tests/research/test_signal_lock_native.py','tools/ci/lock_acceptance.py')},
                  no_test_retries=True, financial_authorization='BLOCKED', no_live_money=True)
    (output/'LOCK_CONCURRENCY_TEST_RESULTS.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('status','subject_sha','round_count','passed_rounds','failed_rounds','faults')}))
    return int(result['status'] != 'PASS')


if __name__ == '__main__':
    raise SystemExit(main())
