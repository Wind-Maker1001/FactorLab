from contextlib import closing, nullcontext
from decimal import Decimal
import importlib.util
import json

import pytest

from factorlab.integrations.alpaca.native_v69 import NativeWeeklyExecution
from factorlab.strategies.deployment import DeploymentStore
from test_alpaca_instance_runtime import Account
from test_alpaca_portfolio import NOW
from test_p10_12_alpaca_preparation import preparation, ROOT

spec = importlib.util.spec_from_file_location('activate_p10_12_alpaca', ROOT / 'tools/activate_p10_12_alpaca.py')
activation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activation)


def test_activation_queues_only_replayable_model_refresh_and_preserves_native(tmp_path, monkeypatch):
    with closing(NativeWeeklyExecution(tmp_path, Account(), clock=lambda: NOW, revision='a' * 40)) as native:
        original = native.package_id
    prepared = tmp_path / 'prepared'
    preparation.prepare(account_id='paper', revision='b' * 40,
        artifact=json.loads((ROOT / 'examples/v179_v182_alpaca/artifacts/v179_v182_ridge_target.json').read_text()),
        destination=prepared, releases=tmp_path / 'releases', benchmark=tmp_path / 'benchmark.json',
        account_budget=Decimal(100000), fee_rate=Decimal(0),
        symbol_limits={symbol: Decimal(99900) for symbol in preparation.ASSETS})
    config = json.loads((prepared / 'proposed-instances.json').read_text())
    monkeypatch.setattr(activation, 'private_root', lambda root: root)
    monkeypatch.setattr(activation, 'owner_lock', lambda root: nullcontext())
    first = activation.activate(tmp_path, config, prepared)
    assert activation.activate(tmp_path, config, prepared) == first
    assert not first['champion_promoted'] and not first['capital_applied'] and not first['official_orders_submitted']
    assert set(first['model_refresh_requests']) == {'P10-12-V179', 'P10-12-V182'}
    with closing(DeploymentStore(tmp_path / 'deployments.sqlite3')) as registry:
        for owner in ('V69_CAUSAL', 'P10-12-V179', 'P10-12-V182'):
            with closing(registry.for_instance(owner)) as view:
                if owner == 'V69_CAUSAL':
                    assert view.snapshot()['active'] == original and view.pending() is None
                else:
                    assert view.snapshot()['active'] is None
                    assert view.pending()['policy'] == 'artifact_refresh'
                    assert len(view._rows('switches').fetchall()) == 1


def test_activation_rejects_old_configuration_without_explicit_policy(tmp_path):
    with pytest.raises(ValueError, match='complete explicit'):
        activation.activate(tmp_path, {'capital_policy': {'symbol_limits': {}}}, tmp_path)
