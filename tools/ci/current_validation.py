"""Serial sharding and historical isolation over the frozen current selector."""
import json
from pathlib import Path

import pytest
from . import validation_layers

TRUSTED_SPEC = None


def pytest_addoption(parser):
    validation_layers.pytest_addoption(parser)
    parser.addoption('--current-shard', type=int, choices=range(4))
    parser.addoption('--current-shard-count', type=int, choices=(3, 4), default=3)
    parser.addoption('--historical-only', action='store_true')
    parser.addoption('--execution-inventory', type=Path)


def pytest_configure(config):
    config.pluginmanager.register(ExecutionInventory(config), 'factorlab-execution-inventory')


def pytest_ignore_collect(collection_path, config):
    if not config.getoption('--historical-only'):
        return validation_layers.pytest_ignore_collect(collection_path, config)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    destination = config.getoption('--current-layer-inventory')
    if config.getoption('--historical-only'):
        historical = validation_layers.historical_nodes()
        keep = [item for item in items if item.nodeid in historical]
        observed = {item.nodeid for item in keep}
        expected = {node for node in historical if '::' in node}
        if observed != expected:
            raise pytest.UsageError('historical isolated selection drift')
        config.hook.pytest_deselected(items=[item for item in items if item.nodeid not in historical])
        items[:] = keep
        inventory = dict(schema='FACTORLAB_HISTORICAL_ISOLATION_V1',
                         historical_collected=sorted(observed),
                         historical_collection_errors=sorted(historical - expected))
    else:
        # This frozen selector produces the complete nonhistorical inventory first.
        validation_layers.pytest_collection_modifyitems(config, items)
        inventory = json.loads(destination.read_bytes())
        shard = config.getoption('--current-shard')
        shard_count = config.getoption('--current-shard-count')
        if shard is not None and shard >= shard_count:
            raise pytest.UsageError('current shard must be less than shard count')
        if shard is not None:
            import sys
            from .sharding import TIMINGS, binding, module_plan, partition, runtime_weights
            version = f'{sys.version_info.major}.{sys.version_info.minor}'
            if TRUSTED_SPEC is not None:
                spec = TRUSTED_SPEC
                if spec['shard_count'] != shard_count:
                    raise pytest.UsageError('shard count differs from trusted plan')
                weights = spec['weights']['versions'][version]['module_seconds']
                inventory['sharding'] = binding(spec, version, inventory['current'])
            else:
                weights = runtime_weights(TIMINGS.read_bytes(), version)
            inventory['module_plan'] = module_plan(inventory['current'], weights)
            nodes = set(partition(inventory['current'], weights, shard_count)[shard])
            config.hook.pytest_deselected(items=[item for item in items if item.nodeid not in nodes])
            items[:] = [item for item in items if item.nodeid in nodes]
        inventory.update(shard=shard, shard_count=shard_count)
    inventory['selected'] = [item.nodeid for item in items]
    destination.write_text(json.dumps(inventory, indent=2) + '\n', encoding='utf-8')


class ExecutionInventory:
    def __init__(self, config):
        self.path = config.getoption('--execution-inventory')
        self.started = []
        self.completed = []
        self.collection_errors = []
        self.reports = {}
        self.report_errors = []
        self.selection = config.getoption('--current-layer-inventory')

    def pytest_runtest_logstart(self, nodeid, location):
        self.started.append(nodeid)

    def pytest_runtest_logreport(self, report):
        phases = self.reports.setdefault(report.nodeid, {})
        if report.when in phases:
            self.report_errors.append('duplicate execution phase: ' + report.nodeid)
        phases[report.when] = 'xfail' if hasattr(report, 'wasxfail') else report.outcome
        if report.when == 'teardown':
            self.completed.append(report.nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        evidence = dict(schema='FACTORLAB_ACTUAL_TEST_EXECUTION_V1', started=self.started,
                        completed=self.completed, collection_errors=self.collection_errors,
                        reports=self.reports, report_errors=self.report_errors, exit_code=int(exitstatus))
        if self.selection.exists():
            inventory = json.loads(self.selection.read_bytes())
            if 'sharding' in inventory:
                evidence['sharding'] = inventory['sharding']
        self.path.write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
