"""Trusted collection and call-phase evidence for risk-selected test files."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys

import pytest


SCHEMA = 'FACTORLAB_PR_RISK_V2'


def validate_plan(repository, plan):
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository, text=True).strip()
    if plan.get('schema') != SCHEMA or plan.get('head') != head:
        raise ValueError('risk plan schema or checkout head mismatch')
    targets = plan.get('targeted_tests')
    if (not isinstance(targets, list)
            or not all(isinstance(target, str) for target in targets)
            or len(set(targets)) != len(targets)):
        raise ValueError('targeted_tests must be a unique file list')
    tracked = subprocess.check_output(['git', 'ls-files', '--stage', '-z'], cwd=repository)
    modes = {}
    for entry in tracked.split(b'\0'):
        if entry:
            metadata, name = entry.decode('utf-8').split('\t', 1)
            mode, _, stage = metadata.split()
            modes[name] = (mode, stage)
    for target in targets:
        if (not isinstance(target, str) or '\\' in target or ':' in target
                or not re.fullmatch(r'tests/(?:[A-Za-z0-9_.-]+/)*test_[A-Za-z0-9_.-]+\.py', target)
                or any(part in ('.', '..') or part.startswith('-') for part in target.split('/'))
                or PurePosixPath(target).as_posix() != target):
            raise ValueError('invalid targeted test path')
        if modes.get(target) not in (('100644', '0'), ('100755', '0')):
            raise ValueError('target must be a tracked regular file')
        path = repository
        for part in target.split('/'):
            path = path / part
            if path.is_symlink():
                raise ValueError('target must not traverse symlinks')
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError('target must be a regular file')
    return targets


def historical_scope(targets):
    trusted = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        'trusted_validation_layers', trusted / 'tools/ci/validation_layers.py')
    layers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(layers)
    nodes = layers.historical_nodes()
    return {node for node in nodes if node.split('::')[0] in targets}


def matrix_digest():
    matrix = Path(__file__).resolve().parents[2] / 'docs/ci/CI_BASELINE_FAILURE_MATRIX.csv'
    return hashlib.sha256(matrix.read_bytes()).hexdigest()


class TargetedObserver:
    def __init__(self, destination, targets=()):
        self.destination = destination
        self.historical = historical_scope(targets)
        self.historical_collected = []
        self.historical_files = sorted(node for node in self.historical if '::' not in node)
        self.selection_errors = []
        self.collected = []
        self.deselected = []
        self.reports = {}
        self.collection_errors = []

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, session, config, items):
        expected = self.historical - set(self.historical_files)
        self.historical_collected = sorted(item.nodeid for item in items if item.nodeid in expected)
        if set(self.historical_collected) != expected:
            self.selection_errors.append('historical targeted selection drift')
        archive = [item for item in items if item.nodeid in expected]
        items[:] = [item for item in items if item.nodeid not in expected]
        config.hook.pytest_deselected(items=archive)

    def pytest_itemcollected(self, item):
        self.collected.append(item.nodeid)

    def pytest_deselected(self, items):
        self.deselected.extend(item.nodeid for item in items)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.nodeid)

    def pytest_runtest_logreport(self, report):
        phases = self.reports.setdefault(report.nodeid, {})
        if report.when in phases:
            self.selection_errors.append('duplicate execution phase: ' + report.nodeid)
        phases[report.when] = 'xfail' if hasattr(report, 'wasxfail') else report.outcome

    def pytest_sessionfinish(self, session, exitstatus):
        self.destination.write_text(json.dumps(dict(
            schema='FACTORLAB_TARGETED_EXECUTION_V2', collected=self.collected,
            deselected=self.deselected, reports=self.reports,
            historical_collected=self.historical_collected,
            historical_collection_errors=self.historical_files,
            historical_status='BLOCKED' if self.historical else 'NOT_SELECTED',
            historical_matrix_sha256=matrix_digest(), selection_errors=self.selection_errors,
            collection_errors=self.collection_errors, exit_code=int(exitstatus)), indent=2) + '\n',
            encoding='utf-8')


def evidence_passed(path, targets, actual_exit_code=None):
    if not path.exists():
        return False
    evidence = json.loads(path.read_text(encoding='utf-8'))
    nodes = evidence['collected']
    historical = historical_scope(targets)
    blocked_files = {node for node in historical if '::' not in node}
    blocked_nodes = historical - blocked_files
    current = set(nodes) - blocked_nodes
    return (evidence.get('schema') == 'FACTORLAB_TARGETED_EXECUTION_V2'
            and evidence['historical_matrix_sha256'] == matrix_digest()
            and (actual_exit_code is None or actual_exit_code == evidence['exit_code'])
            and (evidence['exit_code'] == 0
                 or (evidence['exit_code'] == 5 and not current and bool(blocked_nodes)))
            and not evidence['collection_errors']
            and not evidence['selection_errors']
            and len(nodes) == len(set(nodes))
            and set(evidence['historical_collected']) == blocked_nodes
            and set(evidence['historical_collection_errors']) == blocked_files
            and evidence['historical_status'] == ('BLOCKED' if historical else 'NOT_SELECTED')
            and len(evidence['deselected']) == len(blocked_nodes)
            and set(evidence['deselected']) == blocked_nodes
            and blocked_nodes <= set(nodes)
            and {node.split('::')[0] for node in nodes} | blocked_files == set(targets)
            and set(evidence['reports']) == current
            and all(evidence['reports'][node] ==
                    {'setup': 'passed', 'call': 'passed', 'teardown': 'passed'} for node in current))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execution-inventory', type=Path, required=True)
    parser.add_argument('--target-file', action='append', default=[])
    args, pytest_args = parser.parse_known_args()
    observer = TargetedObserver(args.execution_inventory, args.target_file)
    trusted = Path(__file__).resolve().parents[2]
    for name in observer.historical_files:
        if (Path.cwd() / name).read_bytes() != (trusted / name).read_bytes():
            raise ValueError('historical collection-blocked file changed: ' + name)
    pytest_args = [arg for arg in pytest_args if arg not in observer.historical_files]
    if args.target_file and set(args.target_file) == set(observer.historical_files):
        observer.pytest_sessionfinish(None, 0)
        return 0
    return pytest.main(pytest_args, plugins=[observer])


if __name__ == '__main__':
    raise SystemExit(main())
