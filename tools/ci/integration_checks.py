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


def run(suite, output):
    output.mkdir(parents=True, exist_ok=False)
    before = validation_inputs()
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    command = [sys.executable, '-m', 'pytest', *SUITES[suite], '-q',
               '--continue-on-collection-errors', '--basetemp='+str(output/'tmp'),
               '-o', 'cache_dir='+str(output/'pytest-cache'),
               '--junitxml='+str(output/'tests.xml')]
    if suite == 'current':
        command += ['-p', 'tools.ci.validation_layers',
                    '--current-layer-inventory='+str(output/'selection.json')]
    env = dict(os.environ, MPLBACKEND='Agg', PYTHONUTF8='1', PYTHONHASHSEED='0', TZ='UTC')
    env.pop('PYTHONPATH', None)
    dependencies = subprocess.check_output([sys.executable, '-m', 'pip', 'freeze', '--all'], text=True)
    (output/'dependencies.txt').write_text(dependencies, encoding='utf-8')
    started = time.perf_counter()
    with (output/'tests.log').open('wb') as log:
        code = subprocess.call(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    after = validation_inputs()
    unchanged = before == after
    receipt = dict(schema='FACTORLAB_INCREMENTAL_CI_V1', suite=suite,
                   scope='FULL_PROJECT_REGRESSION' if suite == 'legacy' else 'DECLARED_INCREMENTAL_SCOPE',
                   status='PASS' if code == 0 and unchanged else 'FAIL',
                   exit_code=code, subject_sha=head,
                   github_event_sha=os.environ.get('GITHUB_SHA'),
                   github_pr_head_sha=os.environ.get('FACTORLAB_PR_HEAD_SHA'),
                   python=sys.version, platform=platform.platform(), command=command,
                   dependencies_sha256=hashlib.sha256(dependencies.encode()).hexdigest(),
                   environment={k: env[k] for k in ('MPLBACKEND', 'PYTHONUTF8', 'PYTHONHASHSEED', 'TZ')},
                   targets=SUITES[suite], counts=summary(output/'tests.xml'),
                   validation_inputs_unchanged=unchanged, validation_input_sha256=after,
                   elapsed_seconds=time.perf_counter()-started,
                   financial_authorization='BLOCKED', formal_admission='FORMAL_ADMISSION_BLOCKED',
                   pit='PIT_UNVERIFIED', hfq='HFQ_UNVERIFIED', no_live_money=True)
    (output/'receipt.json').write_text(json.dumps(receipt, indent=2)+'\n', encoding='utf-8')
    print(suite, receipt['status'], json.dumps(receipt['counts']), 'exit', code, flush=True)
    return code if code else int(not unchanged)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=[*SUITES, 'incremental'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    selected = ['core', 'cross-market', 'financial-gates'] if args.suite == 'incremental' else [args.suite]
    codes = [run(suite, output/suite) for suite in selected]
    return max(codes)


if __name__ == '__main__':
    raise SystemExit(main())
