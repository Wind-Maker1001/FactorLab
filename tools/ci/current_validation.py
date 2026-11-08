"""Serial sharding and historical isolation over the frozen current selector."""
import json
from pathlib import Path

import pytest
from tools.ci import validation_layers


def pytest_addoption(parser):
    validation_layers.pytest_addoption(parser)
    parser.addoption('--current-shard', type=int, choices=range(3))
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
        if shard is not None:
            from tools.ci.sharding import TIMINGS, module_plan, partition
            weights = json.loads(TIMINGS.read_bytes())['module_seconds']
            inventory['module_plan'] = module_plan(inventory['current'], weights)
            nodes = set(partition(inventory['current'], weights)[shard])
            config.hook.pytest_deselected(items=[item for item in items if item.nodeid not in nodes])
            items[:] = [item for item in items if item.nodeid in nodes]
        inventory.update(shard=shard, shard_count=3)
    inventory['selected'] = [item.nodeid for item in items]
    destination.write_text(json.dumps(inventory, indent=2) + '\n', encoding='utf-8')


class ExecutionInventory:
    def __init__(self, config):
        self.path = config.getoption('--execution-inventory')
        self.started = []
        self.completed = []
        self.collection_errors = []

    def pytest_runtest_logstart(self, nodeid, location):
        self.started.append(nodeid)

    def pytest_runtest_logreport(self, report):
        if report.when == 'teardown':
            self.completed.append(report.nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        self.path.write_text(json.dumps(dict(
            schema='FACTORLAB_ACTUAL_TEST_EXECUTION_V1', started=self.started,
            completed=self.completed, collection_errors=self.collection_errors,
            exit_code=int(exitstatus)), indent=2) + '\n', encoding='utf-8')
