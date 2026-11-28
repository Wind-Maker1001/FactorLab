from contextlib import closing, nullcontext
from copy import deepcopy
import sqlite3

import pytest

from factorlab.integrations.alpaca.native_v69 import NativeWeeklyExecution
from factorlab.strategies import loader
from factorlab.strategies.deployment import DeploymentStore
from factorlab.trading.storage import dumps
from test_alpaca_instance_runtime import Account
from test_alpaca_portfolio import NOW
from test_p10_12_policy_admission import migration


@pytest.fixture
def copied_native_store(tmp_path, monkeypatch):
    original = tmp_path / 'original'
    original.mkdir()
    monkeypatch.setattr(loader, 'runtime_identity', lambda: 'a' * 64)
    with closing(NativeWeeklyExecution(original, Account(), clock=lambda: NOW,
                                       revision='1' * 40)) as execution:
        package_id = execution.package_id
        binding = execution.strategy.binding
        manifest = execution.view.manifest(package_id)
    copied = tmp_path / 'copied'
    copied.mkdir()
    with closing(sqlite3.connect(original / 'deployments.sqlite3')) as source:
        with closing(sqlite3.connect(copied / 'deployments.sqlite3')) as target:
            source.backup(target)
    before = dict(binding=binding, manifest=manifest,
                  sources={name: pair[0] for name, pair in migration._POLICY_SOURCES.items()},
                  dependencies={}, interpreter={})
    after = deepcopy(before)
    after['binding']['runtime_sha256'] = 'b' * 64
    after['sources'].update({name: pair[1] for name, pair in migration._POLICY_SOURCES.items()})
    monkeypatch.setattr(migration, 'private_root', lambda path: path)
    monkeypatch.setattr(migration, 'owner_lock', lambda path: nullcontext())
    monkeypatch.setattr(migration, 'policy_probe',
                        lambda python, package: (deepcopy(before if python == 'old' else after), set()))
    return original, copied, package_id, binding


def test_copied_store_admission_replays_and_preserves_original_binding(copied_native_store):
    original, copied, package_id, binding = copied_native_store
    first = migration.migrate(copied, 'old', host_policy_transition=True)
    assert first['admission_created']
    assert first['qualification']['kind'] == 'HOST_POLICY_CHANGE'
    assert not migration.migrate(copied, 'old', host_policy_transition=True)['admission_created']
    for root in (original, copied):
        with closing(DeploymentStore(root / 'deployments.sqlite3', instance_id='V69_CAUSAL')) as store:
            assert store._runtime_binding(package_id)['binding'] == dumps(binding)
            assert store.snapshot()['active'] == package_id
            assert len(store._rows('runtime_admissions').fetchall()) == (root == copied)


def test_uncommitted_frozen_event_cannot_admit_policy_change(copied_native_store):
    _, copied, _, _ = copied_native_store
    signals = copied / 'signals'
    signals.mkdir()
    (signals / 'pending.native.json').write_text(dumps({'event_id': 'pending'}))
    with pytest.raises(ValueError, match='finish the frozen event'):
        migration.migrate(copied, 'old', host_policy_transition=True)
    with closing(DeploymentStore(copied / 'deployments.sqlite3', instance_id='V69_CAUSAL')) as store:
        assert not store._rows('runtime_admissions').fetchall()
