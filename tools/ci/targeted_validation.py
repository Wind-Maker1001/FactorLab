"""Trusted collection and call-phase evidence for risk-selected test files."""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys


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


class TargetedObserver:
    def __init__(self, destination):
        self.destination = destination
        self.collected = []
        self.deselected = []
        self.reports = {}
        self.collection_errors = []

    def pytest_itemcollected(self, item):
        self.collected.append(item.nodeid)

    def pytest_deselected(self, items):
        self.deselected.extend(item.nodeid for item in items)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.nodeid)

    def pytest_runtest_logreport(self, report):
        self.reports.setdefault(report.nodeid, {})[report.when] = report.outcome

    def pytest_sessionfinish(self, session, exitstatus):
        self.destination.write_text(json.dumps(dict(
            schema='FACTORLAB_TARGETED_EXECUTION_V1', collected=self.collected,
            deselected=self.deselected, reports=self.reports,
            collection_errors=self.collection_errors, exit_code=int(exitstatus)), indent=2) + '\n',
            encoding='utf-8')


def evidence_passed(path, targets):
    if not path.exists():
        return False
    evidence = json.loads(path.read_text(encoding='utf-8'))
    nodes = evidence['collected']
    return (evidence['exit_code'] == 0 and not evidence['collection_errors']
            and not evidence['deselected']
            and all(any(node.split('::')[0] == target for node in nodes) for target in targets)
            and all(evidence['reports'].get(node) ==
                    {'setup': 'passed', 'call': 'passed', 'teardown': 'passed'} for node in nodes))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execution-inventory', type=Path, required=True)
    args, pytest_args = parser.parse_known_args()
    import pytest
    return pytest.main(pytest_args, plugins=[TargetedObserver(args.execution_inventory)])


if __name__ == '__main__':
    raise SystemExit(main())
