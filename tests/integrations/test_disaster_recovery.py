from contextlib import closing
import os
import sqlite3
import sys

import pytest

from factorlab.integrations import disaster_recovery as dr
from factorlab.integrations.binance_futures_trading import demo_cloud_storage as storage
from factorlab.integrations.binance_futures_trading.demo_runtime import operator_lock
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.strategies.package import install_package, load_package

from test_strategy_deployment_s03 import package


SOURCE = '1' * 32
TARGET = '2' * 32
REVISION = 'a' * 40


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'machine_id', lambda: SOURCE)
    state = tmp_path / 'source'
    state.mkdir(mode=0o700)
    database = state / 'runtime.sqlite3'
    with closing(sqlite3.connect(database)) as db, db:
        os.chmod(database, 0o600)
        db.execute('CREATE TABLE durable(control, spent, pending_exit)')
        db.execute("INSERT INTO durable VALUES ('HALTED', 19, 'original-client-order-id')")
        db.execute('CREATE TABLE allocations(owner, cash)')
        db.execute("INSERT INTO allocations VALUES ('old-owner', '35')")
        db.execute('CREATE TABLE decisions(sequence, intent, state)')
        db.execute("INSERT INTO decisions VALUES (89, 'next-client-order-id', 'PREPARED')")
    return state


def test_corrupt_copy_cannot_leave_quarantine_boundary(source, tmp_path):
    snapshot = storage.recovery_snapshot(source, tmp_path / 'backup', 'binance', REVISION)
    database = snapshot / 'runtime.sqlite3'
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('UPDATE durable SET spent = 0')
    with pytest.raises(SafetyBlocked, match='digest mismatch'):
        dr.recover(snapshot, tmp_path / 'recovered')
    assert not (tmp_path / 'recovered').exists()


def test_snapshot_restores_the_referenced_strategy_package_without_credentials(source, tmp_path, package):
    archive, _ = package
    release, manifest = install_package(archive, source / 'strategy-releases')
    credentials = source / 'paper-secret.json'
    credentials.write_text('{"secret":"fixture-only"}', encoding='utf-8')
    os.chmod(credentials, 0o600)
    snapshot = storage.recovery_snapshot(source, tmp_path / 'backup', 'binance', REVISION)
    verified = storage.verify_snapshot(snapshot)
    restored = storage.restore_verified_state(snapshot, tmp_path / 'restore', verified)
    assert load_package(restored / 'strategy-releases' / release.name) == manifest
    assert 'paper-secret.json' not in verified['files']


def test_stale_internal_state_remains_read_only_after_clean_official_facts(source, tmp_path, monkeypatch):
    snapshot = storage.recovery_snapshot(source, tmp_path / 'backup', 'binance', REVISION)
    # An internal cash transfer leaves the same exchange balance.
    with closing(sqlite3.connect(source / 'runtime.sqlite3')) as db, db:
        db.execute("UPDATE allocations SET owner='new-owner'")
    recovered = tmp_path / 'recovered'
    monkeypatch.setattr(dr, '_catch_up', lambda *_: {'reconciled': True})
    result = dr.recover(snapshot, recovered)
    assert 'allocation and capital transfers' in result['missing_internal_coverage']
    assert result['ledger']['reconciled']
    assert not result['published_for_execution']
    with closing(sqlite3.connect(recovered / 'runtime.sqlite3')) as db:
        assert db.execute('SELECT owner, cash FROM allocations').fetchone() == ('old-owner', '35')
    with pytest.raises(SafetyBlocked, match='GET-only'), operator_lock(recovered):
        pytest.fail('a stale restore acquired execution ownership')
    with pytest.raises(SafetyBlocked, match='final source seal'):
        dr.take_over(snapshot, tmp_path / 'live', configuration=tmp_path / 'config',
                     fence_file=tmp_path / 'proof', revision=REVISION)


if sys.platform == 'linux':
    def test_final_source_seal_survives_reboot_and_publication_requires_reconciled_ledger(
        source, tmp_path, monkeypatch,
    ):
        configuration = tmp_path / 'configuration'
        configuration.write_text('deployed-settings', encoding='utf-8')
        digest = storage.file_sha256(configuration)
        target = tmp_path / 'new-owner'
        backup = storage.recovery_snapshot(source, tmp_path / 'backup', 'binance', REVISION,
            configuration_sha256=digest, final_target=dr._target(TARGET, target))
        with pytest.raises(SafetyBlocked, match='GET-only'), operator_lock(source):
            pytest.fail('the original worker could restart')
        monkeypatch.setattr(storage, 'machine_id', lambda: TARGET)
        proof = tmp_path / 'fencing.json'
        with pytest.raises(SafetyBlocked, match='administrator-owned'):
            dr.take_over(backup, target, configuration=configuration, fence_file=proof, revision=REVISION)
        assert not target.exists()
        monkeypatch.setattr(dr, '_assert_fence', lambda *_: {'mechanism': 'test-durable-fence'})
        monkeypatch.setattr(dr, '_catch_up', lambda *_: {'reconciled': False, 'cash_residual': '0.01'})
        result = dr.take_over(backup, target, configuration=configuration, fence_file=proof, revision=REVISION)
        assert not result['published_for_execution'] and not target.exists()

        monkeypatch.setattr(dr, '_catch_up', lambda *_: {'reconciled': True, 'budget': {'verified': True}})
        result = dr.take_over(backup, target, configuration=configuration, fence_file=proof, revision=REVISION)
        assert result['published_for_execution']
        with operator_lock(target), closing(sqlite3.connect(target / 'runtime.sqlite3')) as db:
            assert db.execute('SELECT * FROM durable').fetchone() == ('HALTED', 19, 'original-client-order-id')
            assert db.execute('SELECT * FROM decisions').fetchone() == (89, 'next-client-order-id', 'PREPARED')
        with pytest.raises(FileExistsError):
            dr.take_over(backup, target, configuration=configuration, fence_file=proof, revision=REVISION)


def test_scheduler_recovers_interrupted_alpaca_maintenance(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    recovery = tmp_path / 'restart.json'
    storage.atomic_json(recovery, {'unit': dr.ALPACA_UNIT})

    class Runner:
        started = []

        def start_unit(self, unit):
            self.started.append(unit)

    runner = Runner()
    dr.finish_alpaca_backup(recovery, state, runner)
    assert runner.started == [dr.ALPACA_UNIT]
    assert not recovery.exists()
    # A scheduled-backup stop hook can also run just after the operator sealed the source.
    storage.atomic_json(state / storage._QUARANTINE, {'reason': storage._DR_SOURCE})
    storage.atomic_json(recovery, {'unit': dr.ALPACA_UNIT})
    dr.finish_alpaca_backup(recovery, state, runner)
    assert runner.started == [dr.ALPACA_UNIT]
