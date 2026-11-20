"""Offline storage maintenance against real SQLite journals."""

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import sqlite3

import pytest

from factorlab.integrations.binance_futures_trading import demo_cloud_storage as storage
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, operator_lock
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked


@pytest.fixture
def state(tmp_path):
    root = tmp_path / 'state'
    root.mkdir(mode=0o700)
    storage.validate_state(root, 0)
    storage.atomic_json(root / 'binding.json', {'account_id': 'demo-account', 'journal_id': 'journal',
                                               'credential_reference': 'external-credentials'})
    storage.atomic_json(root / 'secrets.json', {'api_secret': 'never-copy-this'})
    with closing(DemoRuntime(root)) as runtime:
        with runtime.db:
            runtime._set('spent_budget', '7')
    (root / 'runtime.sqlite3').chmod(0o600)
    nested = root / 'journals' / 'journal'
    nested.mkdir(parents=True)
    nested.parent.chmod(0o700)
    nested.chmod(0o700)
    path = nested / 'protocol.sqlite3'
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('CREATE TABLE facts (value TEXT)')
        connection.execute("INSERT INTO facts VALUES ('persisted')")
        connection.commit()
    path.chmod(0o600)
    return root


def read_value(path, query):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(query).fetchone()[0]


def test_backup_restore_preserves_binding_journals_and_budget(state, tmp_path):
    backup = storage.backup_state(state, tmp_path / 'backup')
    assert not (backup / 'secrets.json').exists()
    assert not (backup / 'restore-quarantine.json').exists()
    restored = storage.restore_state(backup, tmp_path / 'restored')
    assert json.loads((restored / 'binding.json').read_text())['account_id'] == 'demo-account'
    assert read_value(restored / 'runtime.sqlite3', "SELECT value FROM runtime WHERE key='spent_budget'") == '7'
    assert read_value(restored / 'journals/journal/protocol.sqlite3', 'SELECT value FROM facts') == 'persisted'
    assert (restored / 'restore-quarantine.json').is_file()
    with pytest.raises(SafetyBlocked, match='GET-only'):
        with operator_lock(restored):
            pytest.fail('restored state must be quarantined')
    with operator_lock(restored, allow_quarantined=True):
        pass
    second_backup = storage.backup_state(restored, tmp_path / 'second-backup')
    assert (second_backup / 'restore-quarantine.json').is_file()


def test_incomplete_backup_cannot_restore(state, tmp_path):
    backup = storage.backup_state(state, tmp_path / 'backup')
    (backup / 'journals/journal/protocol.sqlite3').unlink()
    with pytest.raises(SafetyBlocked, match='incomplete backup'):
        storage.restore_state(backup, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_wal_backup_and_metrics_include_committed_wal(state, tmp_path):
    path = state / 'runtime.sqlite3'
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        connection.execute("INSERT INTO runtime VALUES ('wal-only', 'committed')")
        connection.commit()
        metrics = storage.storage_metrics(state)
        record = metrics['databases']['runtime.sqlite3']
        assert record['wal_bytes'] > 0
        assert record['shm_bytes'] > 0
        assert record['total_bytes'] == sum(record[key] for key in ('database_bytes', 'wal_bytes', 'shm_bytes', 'journal_bytes'))
        assert metrics['sidecar_bytes'] >= record['wal_bytes']
        backup = storage.backup_state(state, tmp_path / 'backup')
        assert read_value(backup / 'runtime.sqlite3', "SELECT value FROM runtime WHERE key='wal-only'") == 'committed'
        assert not list(backup.rglob('*-wal'))


def test_freezes_every_database_and_blocks_lock_bypassing_stop(state, tmp_path, monkeypatch):
    original = storage._snapshot
    observed = []

    def snapshot(root, destination, databases, *, quarantine):
        with closing(DemoRuntime(root)) as runtime:
            runtime.db.execute('PRAGMA busy_timeout=0')
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                runtime.stop()
        for path in databases:
            with closing(sqlite3.connect(path, timeout=0)) as connection:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    connection.execute('BEGIN IMMEDIATE')
                observed.append(path)
        return original(root, destination, databases, quarantine=quarantine)

    monkeypatch.setattr(storage, '_snapshot', snapshot)
    storage.backup_state(state, tmp_path / 'backup')
    assert len(observed) == 2
    with closing(sqlite3.connect(state / 'runtime.sqlite3', timeout=0)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.rollback()


def test_busy_writer_and_operator_prevent_backup(state, tmp_path):
    destination = tmp_path / 'backup'
    with closing(sqlite3.connect(state / 'runtime.sqlite3', timeout=0)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            storage.backup_state(state, destination)
    assert not destination.exists()
    with closing(sqlite3.connect(state / 'journals/journal/protocol.sqlite3', timeout=0)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.rollback()
    with operator_lock(state):
        with pytest.raises(SafetyBlocked, match='another Demo session'):
            storage.backup_state(state, destination)
    assert not destination.exists()


@pytest.mark.parametrize('operation', [storage.backup_state, storage.restore_state])
def test_no_overwrite_and_no_nested_destination(state, tmp_path, operation):
    destination = tmp_path / 'existing'
    destination.mkdir()
    with pytest.raises(FileExistsError):
        operation(state, destination)
    assert not list(destination.iterdir())
    with pytest.raises(SafetyBlocked, match='outside'):
        operation(state, state / 'nested')


def test_publication_race_never_replaces_empty_directory(state, tmp_path, monkeypatch):
    original = storage._publish
    destination = tmp_path / 'backup'

    def publish(staging, dest):
        dest.mkdir()
        return original(staging, dest)

    monkeypatch.setattr(storage, '_publish', publish)
    with pytest.raises(OSError):
        storage.backup_state(state, destination)
    assert destination.is_dir()
    assert not list(destination.iterdir())
    assert not list(tmp_path.glob('.backup-*'))


def test_corrupt_database_restore_is_not_published(state, tmp_path):
    backup = storage.backup_state(state, tmp_path / 'backup')
    (backup / 'runtime.sqlite3').write_bytes(b'not a database')
    with pytest.raises(sqlite3.DatabaseError):
        storage.restore_state(backup, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_validation_readonly_uri_escaping_and_free_space(tmp_path):
    special = ' # percent%' if os.name == 'nt' else ' # question? percent%'
    root = tmp_path / ('state' + special)
    with pytest.raises(FileNotFoundError):
        storage.validate_state(root, 0)
    assert not root.exists()
    root.mkdir(mode=0o700)
    metrics = storage.validate_state(root, 0)
    assert metrics['databases'] == {}
    assert list(root.iterdir()) == []
    path = root / ('ledger' + special + '.sqlite3')
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('CREATE TABLE ledger (amount INTEGER)')
    path.chmod(0o600)
    storage.validate_state(root, 0)
    with closing(storage._connect(path)) as connection:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            connection.execute('INSERT INTO ledger VALUES (1)')
    with pytest.raises(SafetyBlocked, match='free space'):
        storage.validate_state(root, storage.storage_metrics(root)['disk_bytes'] + 1)
    with pytest.raises(ValueError):
        storage.validate_state(root, -1)


def test_atomic_json_failure_keeps_old_snapshot(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    root.mkdir(mode=0o700)
    storage.validate_state(root, 0)
    path = root / 'snapshot.json'
    storage.atomic_json(path, {'old': 1})
    with pytest.raises(ValueError):
        storage.atomic_json(path, {'invalid': float('nan')})
    assert json.loads(path.read_text()) == {'old': 1}

    def fail(*args):
        raise OSError('publication failed')

    monkeypatch.setattr(storage.os, 'replace', fail)
    with pytest.raises(OSError, match='publication failed'):
        storage.atomic_json(path, {'new': 2})
    assert json.loads(path.read_text()) == {'old': 1}
    assert sorted(p.name for p in root.iterdir()) == ['snapshot.json']


def test_atomic_json_serializes_runtime_metrics(tmp_path):
    root = tmp_path / 'state'
    root.mkdir(mode=0o700)
    storage.validate_state(root, 0)
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
    storage.atomic_json(root / 'snapshot.json', {'at': instant, 'budget': Decimal('0.04')})
    assert json.loads((root / 'snapshot.json').read_text()) == {'at': str(instant), 'budget': '0.04'}
    assert storage.storage_metrics(root)['disk_free_bytes'] > 0


def test_transient_sidecar_disappearance_is_not_fatal(state, monkeypatch):
    sidecar = state / 'runtime.sqlite3-journal'
    sidecar.write_bytes(b'transient')
    sidecar.chmod(0o600)
    original = storage._private
    def private(path, **kwargs):
        if path == sidecar:
            sidecar.unlink()
        return original(path, **kwargs)
    monkeypatch.setattr(storage, '_private', private)
    assert storage.storage_metrics(state)['database_bytes'] > 0
    monkeypatch.setattr(storage, '_private', original)
    databases, files = storage._inventory(state)
    assert storage._storage_metrics(state, databases, files + [sidecar])['database_bytes'] > 0
    with pytest.raises(FileNotFoundError):
        storage._storage_metrics(state, databases, files + [state / 'lost.sqlite3'])


def test_binding_credentials_rejected(state, tmp_path):
    storage.atomic_json(state / 'binding.json', {'configuration': {'api_secret': 'secret'}})
    with pytest.raises(SafetyBlocked, match='credentials'):
        storage.backup_state(state, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()
    assert not list(tmp_path.glob('.backup-*'))


if os.name != 'nt':
    def test_private_tree_and_owner_enforcement(state, tmp_path, monkeypatch):
        for path in (state, state / 'runtime.sqlite3', state / 'journals'):
            original = path.stat().st_mode & 0o777
            path.chmod(original | 0o040)
            with pytest.raises(SafetyBlocked, match='private'):
                storage.validate_state(state, 0)
            path.chmod(original)
        owner = os.getuid()
        monkeypatch.setattr(storage.os, 'getuid', lambda: owner + 1)
        with pytest.raises(SafetyBlocked, match='owned'):
            storage.validate_state(state, 0)


if os.name != 'nt':
    def test_symlinks_and_hardlinks_rejected(state, tmp_path):
        alias = tmp_path / 'alias'
        alias.symlink_to(state, target_is_directory=True)
        with pytest.raises(SafetyBlocked):
            storage.validate_state(alias, 0)
        link = state / 'linked.sqlite3'
        link.symlink_to(state / 'runtime.sqlite3')
        with pytest.raises(SafetyBlocked):
            storage.backup_state(state, tmp_path / 'backup')
        link.unlink()
        os.link(state / 'runtime.sqlite3', link)
        with pytest.raises(SafetyBlocked, match='hard links'):
            storage.validate_state(state, 0)


if os.name != 'nt':
    def test_snapshots_and_all_staged_directories_are_private(state, tmp_path):
        backup = storage.backup_state(state, tmp_path / 'backup')
        restored = storage.restore_state(backup, tmp_path / 'restored')
        for root in (backup, restored):
            for path in (root, *root.rglob('*')):
                assert path.stat().st_mode & 0o077 == 0
