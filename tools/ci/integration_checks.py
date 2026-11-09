"""Declared incremental checks and a full run that retains and propagates failures."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parents[2]
SUITES = {
    'core': [
        'tests/research/test_factors_phase1.py',
        'tests/research/test_factor_metrics_golden.py',
        'tests/research/test_models_phase2.py',
        'tests/research/test_tree_models_phase3.py',
        'tests/research/test_walk_forward_runner_phase2.py',
        'tests/research/test_prediction_metrics_phase2.py',
        'tests/research/test_exploratory_statistical_audit.py',
        'tests/research/test_portfolio_backtest_phase2.py',
        'tests/research/test_execution_backtest_phase3.py',
    ],
    'cross-market': ['tests/cross_market'],
    'financial-gates': [
        'tests/security',
        'tests/research/test_qualification_admission_boundaries.py',
        'tests/research/test_forward_activation_phase4.py',
        'tests/research/test_forward_policy_phase4.py',
        'tests/cross_market/test_exploratory_scenario.py',
    ],
    'current': [],  # Exhaustive nonhistorical selection, with an exact inventory.
    'configuration': [
        'tests/research/test_config_roots.py', 'tests/research/test_config.py',
        'tests/research/test_local_cli_pipeline.py::test_relative_import_paths_use_repository_root_from_another_cwd',
        'tests/research/test_real_factor_label_smoke.py::test_main_from_other_cwd_resolves_repo_relative_config',
    ],
    'locking': ['tests/research/test_forward_full_predictions.py',
                'tests/research/test_forward_signal_recorder_phase4.py',
                'tests/research/test_signal_lock_native.py'],
    'historical': [],  # Only the immutable 186-record historical partition.
    'legacy': [],  # All configured tests, with no selection or deselection override.
}


def validation_inputs():
    paths = subprocess.check_output(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).split(b'\0')
    return {p.decode('utf-8'): hashlib.sha256((ROOT/p.decode('utf-8')).read_bytes()).hexdigest()
            for p in paths if p and (ROOT/p.decode('utf-8')).is_file()}


def summary(path):
    if not path.exists():
        return {'status': 'NOT_OBSERVED'}
    cases = list(ET.parse(path).getroot().iter('testcase'))
    counts = dict(tests=len(cases), passed=0, failures=0, errors=0, skipped=0)
    for case in cases:
        outcome = ('errors' if case.find('error') is not None else
                   'failures' if case.find('failure') is not None else
                   'skipped' if case.find('skipped') is not None else 'passed')
        counts[outcome] += 1
    return counts


def run(suite, output, shard=None, risk_plan=None):
    output.mkdir(parents=True, exist_ok=False)
    before = validation_inputs()
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if suite == 'targeted':
        from targeted_validation import validate_plan
        targets = validate_plan(ROOT, risk_plan)
        if not targets:
            receipt = dict(schema='FACTORLAB_INCREMENTAL_CI_V1', suite=suite,
                           subject_sha=head, status='PASS', targets=[],
                           counts=dict(tests=0), required_execution_observed=True,
                           reason='trusted plan requires no additional tests')
            (output/'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
            print('No additional tests required by trusted plan', flush=True)
            return 0
    else:
        targets = SUITES[suite]
    if suite == 'historical':
        with (ROOT/'docs/ci/CI_BASELINE_FAILURE_MATRIX.csv').open(encoding='utf-8-sig', newline='') as stream:
            import csv
            targets = sorted({row['test_id'].split('::')[0] for row in csv.DictReader(stream)})
    command = [sys.executable, '-u', '-m', 'pytest', *targets, '-v' if shard is not None else '-q',
               '--continue-on-collection-errors', '--basetemp='+str(output/'tmp'),
               '-o', 'cache_dir='+str(output/'pytest-cache'),
               '--junitxml='+str(output/'tests.xml')]
    if suite == 'targeted':
        command = [sys.executable, '-I', '-u', str(Path(__file__).with_name('targeted_validation.py')),
                   '--execution-inventory='+str(output/'execution.json'), *command[4:]]
    if suite in ('current', 'historical'):
        command += ['-p', 'tools.ci.current_validation',
                    '--current-layer-inventory='+str(output/'selection.json'),
                    '--execution-inventory='+str(output/'execution.json')]
    if suite == 'historical':
        command += ['--historical-only']
    if shard is not None:
        command += ['--current-shard='+str(shard), '--durations=30']
    env = dict(os.environ, MPLBACKEND='Agg', PYTHONUTF8='1', PYTHONHASHSEED='0', TZ='UTC')
    env.pop('PYTHONPATH', None)
    dependencies = subprocess.check_output([sys.executable, '-m', 'pip', 'freeze', '--all'], text=True)
    (output/'dependencies.txt').write_text(dependencies, encoding='utf-8')
    started = time.perf_counter()
    print('Running:', subprocess.list2cmdline(command), flush=True)
    with (output/'tests.log').open('wb') as log:
        with subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT) as process:
            for line in process.stdout:
                log.write(line)
                log.flush()
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
            code = process.wait()
    after = validation_inputs()
    unchanged = before == after
    observed = True
    if suite == 'targeted':
        from targeted_validation import evidence_passed
        observed = evidence_passed(output/'execution.json', targets)
    receipt = dict(schema='FACTORLAB_INCREMENTAL_CI_V1', suite=suite, shard=shard,
                   scope='FULL_PROJECT_REGRESSION' if suite == 'legacy' else 'DECLARED_INCREMENTAL_SCOPE',
                   status='PASS' if code == 0 and unchanged and observed else 'FAIL',
                   exit_code=code, subject_sha=head,
                   github_event_sha=os.environ.get('GITHUB_SHA'),
                   github_pr_head_sha=os.environ.get('FACTORLAB_PR_HEAD_SHA'),
                   python=sys.version, platform=platform.platform(), command=command,
                   dependencies_sha256=hashlib.sha256(dependencies.encode()).hexdigest(),
                   environment={k: env[k] for k in ('MPLBACKEND', 'PYTHONUTF8', 'PYTHONHASHSEED', 'TZ')},
                   targets=targets, counts=summary(output/'tests.xml'),
                   validation_inputs_unchanged=unchanged, validation_input_sha256=after,
                   required_execution_observed=observed,
                   elapsed_seconds=time.perf_counter()-started,
                   financial_authorization='BLOCKED', formal_admission='FORMAL_ADMISSION_BLOCKED',
                   pit='PIT_UNVERIFIED', hfq='HFQ_UNVERIFIED', no_live_money=True)
    (output/'receipt.json').write_text(json.dumps(receipt, indent=2)+'\n', encoding='utf-8')
    print(suite, receipt['status'], json.dumps(receipt['counts']), 'exit', code, flush=True)
    return code if code else int(not unchanged or not observed)


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path)
    parser.add_argument('--risk-plan', type=Path)
    parser.add_argument('--suite', choices=[*SUITES, 'incremental', 'targeted'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--shard', type=int, choices=range(3))
    args = parser.parse_args()
    if args.shard is not None and args.suite != 'current':
        parser.error('--shard requires --suite current')
    if args.suite == 'targeted' and (args.repository is None or args.risk_plan is None):
        parser.error('--suite targeted requires --repository and --risk-plan')
    if args.risk_plan is not None and args.suite != 'targeted':
        parser.error('--risk-plan requires --suite targeted')
    if args.repository is not None:
        ROOT = args.repository.resolve()
    risk_plan = json.loads(args.risk_plan.read_text(encoding='utf-8')) if args.risk_plan else None
    output = args.output.resolve()
    selected = ['core', 'cross-market', 'financial-gates'] if args.suite == 'incremental' else [args.suite]
    codes = [run(suite, output/suite, args.shard, risk_plan) for suite in selected]
    return max(codes)


if __name__ == '__main__':
    raise SystemExit(main())
