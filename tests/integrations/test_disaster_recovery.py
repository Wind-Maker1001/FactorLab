from contextlib import closing
import os
import sqlite3
import shutil

from factorlab.integrations.ledger_replication import BatchReceiver, EventAdmission, ReplicationCursor
from factorlab.integrations.ledger_replay import AccountEventWriter, AccountReplay, DomainReplay, SnapshotPosition
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


def test_replication_connection_is_captured_inside_the_database_freeze(source, tmp_path):
    captured = dict(stream='account', generation='g1', sequence=0, digest=None,
                    domains={'runtime': 'runtime.sqlite3'})

    def capture(databases):
        assert source / 'runtime.sqlite3' in databases
        with closing(sqlite3.connect(source / 'runtime.sqlite3', timeout=0)) as writer:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                writer.execute('BEGIN IMMEDIATE')
        return captured

    snapshot = storage.recovery_snapshot(source, tmp_path / 'backup', 'binance', REVISION,
                                         replication_capture=capture)
    assert storage.verify_snapshot(snapshot)['replication'] == captured


def test_failed_replication_capture_does_not_seal_or_publish_source(source, tmp_path):
    def capture(_databases):
        raise SafetyBlocked('pending account admission')

    with pytest.raises(SafetyBlocked, match='pending account admission'):
        storage.recovery_snapshot(source, tmp_path / 'backup', 'binance', REVISION,
                                  replication_capture=capture,
                                  final_target=dr._target(TARGET, tmp_path / 'target'))
    assert not (tmp_path / 'backup').exists()
    with operator_lock(source):
        pass


def test_recover_replays_offsite_originals_before_official_get_without_source(source, tmp_path, monkeypatch):
    baseline = SnapshotPosition('account', 'g1', 0, None)
    receiver = BatchReceiver(tmp_path / 'offsite', stream='account', generation='g1')

    def reduce_control(db, event):
        db.execute('UPDATE durable SET control=?', (event['desired'],))

    def send(payload):
        receiver.accept(payload)
        return ReplicationCursor('account', 'g1', payload['sequence'], payload['digest'])

    with closing(sqlite3.connect(source / 'runtime.sqlite3', isolation_level=None)) as db, closing(
            sqlite3.connect(source / 'admission.sqlite3', isolation_level=None)) as journal:
        os.chmod(source / 'admission.sqlite3', 0o600)
        replay = AccountReplay({'runtime': DomainReplay(db, reduce_control, baseline=baseline)}, baseline=baseline)
        writer = AccountEventWriter(EventAdmission(journal, stream='account', generation='g1', send=send), replay)
        snapshot = storage.recovery_snapshot(source, tmp_path / 'backup', 'binance', REVISION,
                                             replication_capture=writer.snapshot_capture(source))
        writer.submit(dict(event_id='pause-original', targets=['runtime'], desired='PAUSED'),
                      committed_at='2026-11-28T00:00:00+00:00')
    shutil.rmtree(source)

    def official_get(_market, root, _since):
        with closing(sqlite3.connect(root / 'runtime.sqlite3')) as db:
            assert db.execute('SELECT control,spent FROM durable').fetchone() == ('PAUSED', 19)
        return dict(reconciled=True, budget={'verified': True})

    monkeypatch.setattr(dr, '_catch_up', official_get)
    destination = tmp_path / 'investigation'
    result = dr.recover(snapshot, destination, receiver=receiver, reducers={'runtime': reduce_control})
    assert result['incremental_replay']['position']['sequence'] == 1
    assert result['incremental_replay']['all_declared_domains_applied']
    assert not result['published_for_execution']
    assert result['missing_internal_coverage']
    with pytest.raises(SafetyBlocked, match='GET-only'), operator_lock(destination):
        pytest.fail('incremental investigation acquired execution authority')
    receiver.close()


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


def test_wal_snapshot_remains_self_contained_after_restore(source, tmp_path):
    with closing(sqlite3.connect(source / 'runtime.sqlite3')) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("INSERT INTO allocations VALUES ('wal-owner', '12')")
        db.commit()

        snapshot = storage.recovery_snapshot(source, tmp_path / 'backup', 'binance', REVISION)
        restored = storage.restore_state(snapshot, tmp_path / 'restore')

        storage.verify_snapshot(snapshot)
        assert not list(snapshot.glob('runtime.sqlite3-*'))
        assert db.execute('PRAGMA journal_mode').fetchone() == ('wal',)
    with closing(sqlite3.connect(restored / 'runtime.sqlite3')) as db:
        assert db.execute("SELECT cash FROM allocations WHERE owner='wal-owner'").fetchone() == ('12',)


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
