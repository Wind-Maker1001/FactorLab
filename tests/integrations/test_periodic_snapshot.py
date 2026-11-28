from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from factorlab.integrations import disaster_recovery as dr
from factorlab.integrations.binance_futures_trading import demo_cloud_storage as storage
from factorlab.integrations.binance_futures_trading.demo_cloud_backup import SystemdRunner
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked


REVISION = 'a' * 40


def ledger(path, mode='WAL'):
    with closing(sqlite3.connect(path)) as db, db:
        os.chmod(path, 0o600)
        db.execute(f'PRAGMA journal_mode={mode}')
        db.execute('CREATE TABLE events(id PRIMARY KEY, cash, order_id, checkpoint)')
        db.execute("INSERT INTO events VALUES (1, '100', 'original-order', 'original-state')")


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'machine_id', lambda: '1' * 32)
    root = tmp_path / 'state'
    root.mkdir(mode=0o700)
    ledger(root / 'account.sqlite3')
    ledger(root / 'deployments.sqlite3')
    ledger(root / 'runtime.sqlite3', 'DELETE')
    storage.atomic_json(root / 'binding.json', {'identity': 'original'})
    return root


def change(root):
    for path in root.glob('*.sqlite3'):
        with closing(sqlite3.connect(path, timeout=0)) as db, db:
            db.execute("UPDATE events SET cash='25', checkpoint='new-state'")
            db.execute("INSERT INTO events VALUES (2, '75', 'new-order', 'new-state')")


def test_fixed_cut_survives_concurrent_writes_and_restore(state, tmp_path):
    with storage.periodic_recovery_snapshot(state, tmp_path / 'backup', 'binance', REVISION) as complete:
        change(state)
        storage.atomic_json(state / 'binding.json', {'identity': 'replacement'})
        backup = complete()
    manifest = storage.verify_snapshot(backup)
    restored = storage.restore_verified_state(backup, tmp_path / 'restored', manifest)
    for path in restored.glob('*.sqlite3'):
        with closing(sqlite3.connect(path)) as db:
            assert db.execute('SELECT * FROM events').fetchall() == [
                (1, '100', 'original-order', 'original-state')]
    assert storage._binding(restored / 'binding.json') == {'identity': 'original'}
    assert (restored / storage._QUARANTINE).exists()
    assert manifest['final_target'] is None


def test_external_mailbox_is_pinned_with_economic_ledgers(state, tmp_path):
    mailbox = tmp_path / 'tasks.sqlite3'
    ledger(mailbox)
    with storage.periodic_recovery_snapshot(state, tmp_path / 'backup', 'binance', REVISION,
                                           task_mailbox=mailbox) as complete:
        with closing(sqlite3.connect(mailbox)) as db, db:
            db.execute("UPDATE events SET checkpoint='acknowledged'")
        complete()
    storage.verify_snapshot(tmp_path / 'backup')
    with closing(sqlite3.connect(tmp_path / 'backup' / storage._TASK_MAILBOX)) as db:
        assert db.execute('SELECT checkpoint FROM events').fetchone() == ('original-state',)


def test_copy_failure_unpins_readers_and_never_publishes(state, tmp_path, monkeypatch):
    with storage.periodic_recovery_snapshot(state, tmp_path / 'backup', 'binance', REVISION) as complete:
        monkeypatch.setattr(storage, '_integrity', lambda *_: (_ for _ in ()).throw(ValueError('bad copy')))
        with pytest.raises(ValueError, match='bad copy'):
            complete()
    assert not (tmp_path / 'backup').exists()
    assert not list(tmp_path.glob('.backup-*'))
    change(state)
    with closing(sqlite3.connect(state / 'account.sqlite3')) as db:
        assert db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0


def test_sealed_source_cannot_become_a_periodic_snapshot(state, tmp_path):
    storage.atomic_json(state / storage._QUARANTINE, {'reason': 'sealed'})
    with pytest.raises(SafetyBlocked, match='unsealed'):
        with storage.periodic_recovery_snapshot(state, tmp_path / 'backup', 'binance', REVISION):
            pytest.fail('sealed source was captured')


@pytest.mark.skipif(sys.platform != 'linux', reason='systemd readiness transport uses inherited Unix FDs')
def test_real_worker_resumes_writers_before_online_copy(state, tmp_path):
    destination = tmp_path / 'backup'
    resumed = []

    def resume():
        resumed.append(True)
        change(state)

    result = SystemdRunner().run_python_module(dr.MODULE, [
        'snapshot', '--periodic', '--market', 'alpaca', '--state-root', str(state),
        '--destination', str(destination), '--revision', REVISION,
    ], on_snapshot_ready=resume, timeout=20)
    result.check_returncode()
    assert resumed == [True]
    storage.verify_snapshot(destination)
    with closing(sqlite3.connect(destination / 'account.sqlite3')) as db:
        assert db.execute('SELECT count(*) FROM events').fetchone() == (1,)


@pytest.mark.skipif(sys.platform != 'linux', reason='systemd readiness transport uses inherited Unix FDs')
def test_coordinator_failure_terminates_worker_and_releases_cut(state, tmp_path):
    def resume():
        raise RuntimeError('restart failed')

    with pytest.raises(RuntimeError, match='restart failed'):
        SystemdRunner().run_python_module(dr.MODULE, [
            'snapshot', '--periodic', '--market', 'alpaca', '--state-root', str(state),
            '--destination', str(tmp_path / 'backup'), '--revision', REVISION,
        ], on_snapshot_ready=resume, timeout=20)
    assert not (tmp_path / 'backup').exists()
    assert (tmp_path / '.backup.partial').is_dir()
    assert dr.main(['cleanup-periodic', '--state-root', str(state),
                    '--destination', str(tmp_path / 'backup')]) == 0
    assert not (tmp_path / '.backup.partial').exists()
    change(state)


def test_scheduled_backup_records_only_stop_to_resume_window(state, tmp_path):
    release = tmp_path / REVISION
    release.mkdir()
    backups = tmp_path / 'backups'
    backups.mkdir(mode=0o700)
    calls = []

    class Runner:
        def is_unit_active(self, unit):
            return True

        def stop_unit(self, unit):
            calls.append('stop')

        def start_unit(self, unit):
            calls.append('start')

        def run_python_module(self, module, args, *, on_snapshot_ready=None):
            if args[0] == 'cleanup-periodic':
                assert dr.main(args) == 0
                return subprocess.CompletedProcess(args, 0, '{}', '')
            if args[0] == 'describe':
                storage.verify_snapshot(Path(args[-1]))
                return subprocess.CompletedProcess(args, 0, '{}', '')
            assert calls == ['stop']
            destination = Path(args[args.index('--destination') + 1])
            with storage.periodic_recovery_snapshot(state, destination, 'binance', REVISION) as complete:
                on_snapshot_ready()
                assert calls == ['stop', 'start']
                change(state)
                complete()
            return subprocess.CompletedProcess(args, 0, '{}', '')

    result = dr.scheduled_alpaca_backup(state, backups, release, tmp_path / 'resume.json', 2, Runner())
    assert calls == ['stop', 'start']
    assert result['verified']
    assert result['maintenance_downtime_seconds'] >= 0
    assert not (tmp_path / 'resume.json').exists()


@pytest.mark.parametrize('restart', [False, True])
def test_recovery_cleans_only_recorded_attempt(state, tmp_path, restart):
    destination = tmp_path / 'backup'
    staging = tmp_path / '.backup.partial'
    staging.mkdir(mode=0o700)
    (staging / 'partial.sqlite3').write_bytes(b'incomplete')
    unrelated = tmp_path / '.another.partial'
    unrelated.mkdir(mode=0o700)
    marker = tmp_path / 'resume.json'
    storage.atomic_json(marker, {'restart': restart, 'destination': str(destination)})
    starts = []

    class Runner:
        def start_unit(self, unit):
            starts.append(unit)

        def run_python_module(self, module, args):
            assert dr.main(args) == 0
            return subprocess.CompletedProcess(args, 0, '{}', '')

    dr.finish_alpaca_backup(marker, state, Runner())
    assert starts == ([dr.ALPACA_UNIT] if restart else [])
    assert not staging.exists()
    assert unrelated.is_dir()
    assert not marker.exists()
