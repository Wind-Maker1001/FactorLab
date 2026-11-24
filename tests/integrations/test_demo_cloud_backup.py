"""Offline tests for P8-C07 scheduled coherent backup, retention, and isolated DR drill."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from factorlab.integrations.binance_futures_trading import demo_cloud_backup as backup_mod
from factorlab.integrations.binance_futures_trading import demo_cloud_handoff as handoff
from factorlab.integrations.binance_futures_trading import demo_cloud_storage as storage
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, journal_id, operator_lock
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked


@pytest.mark.parametrize('account_ok,market_ok,unknown,accepted', [
    (True, True, 0, True), (False, True, 0, False),
    (True, False, 0, False), (True, True, 1, False),
])
def test_budget_paused_maintenance_requires_fresh_readonly_recovery(account_ok, market_ok, unknown, accepted):
    pre = dict(pid=1, status='PAUSED', desired='RUNNING', remaining_posts=0,
        consumed_posts=100, consumed_submitted_notional='635.974')
    post = dict(pre, pid=2, execute_healthy=False, account_recovered=account_ok,
        market_fresh=market_ok, unknown_count=unknown)
    matched, _ = backup_mod._reconcile_matches(pre, post,
        unit='factorlab-demo-execute.service', require_new_pid=True)
    assert matched is accepted
    if accepted:
        assert backup_mod._reconcile_matches(pre, dict(post, consumed_posts=0),
            unit='factorlab-demo-execute.service', require_new_pid=True) == (False, 'BUDGET_POSTS_REGRESSED')


def test_maintenance_reconcile_timeout_restores_auxiliary_writers(monkeypatch):
    starts = []

    class Runner:
        def is_unit_active(self, unit):
            return False

        def start_unit(self, unit):
            starts.append(unit)

    monkeypatch.setattr(backup_mod, '_restore_execute', lambda *args, **kwargs: 'RECONCILE_TIMEOUT')
    window = dict(interrupted_unit='factorlab-demo-execute.service', auxiliary_units=[
        'factorlab-demo-strategy-deploy.timer', 'factorlab-demo-tradfi-observer.service'])
    assert backup_mod._restore_maintenance(Runner(), Path('unused'), window) == 'RECONCILE_TIMEOUT'
    assert starts == ['factorlab-demo-tradfi-observer.service', 'factorlab-demo-strategy-deploy.timer']


ACCOUNT_ID = 'demo-account-c07-test'
JOURNAL_ID = journal_id(ACCOUNT_ID)
SECRET_SENTINEL = 'SUPER_SECRET_NEVER_COPY_C07_9999'


def _seed_execute_state(root, *, control_desired='RUNNING', auth_state='RUNNING',
                        funding_complete=True, include_secret_decoy=True):
    root.mkdir(parents=True, exist_ok=True)
    if storage.os.name != 'nt':
        root.chmod(0o700)

    binding = {
        'version': 1,
        'environment': 'DEMO',
        'host': 'demo-fapi.binance.com',
        'credential_reference': 'systemd-demo-secret',
        'account_id': ACCOUNT_ID,
        'journal_id': JOURNAL_ID,
        'symbols': ['NVDAUSDT'],
        'configuration': {'margin_mode': 'CROSSED', 'position_mode': 'ONE_WAY'},
    }
    storage.atomic_json(root / 'binding.json', binding)
    if include_secret_decoy:
        storage.atomic_json(root / 'secrets.json', {'api_secret': SECRET_SENTINEL})

    with closing(DemoRuntime(root)) as runtime:
        auth = runtime.start_continuous(
            binding,
            opening_equity=Decimal('5000.00'),
            stop_epoch=runtime.stop_epoch(),
            total_orders=100,
            total_notional=Decimal('700'),
            orders=4,
            max_loss=Decimal('1'),
        )
        campaign = {
            'id': 'window-c07-001',
            'authorization': auth['id'],
            'journal_id': JOURNAL_ID,
            'prefix': auth['prefix'],
            'expires_at': (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(),
            'seconds': 10800,
            'orders': 4,
            'opening_equity': '5000.00',
            'max_loss': '1',
            'smoke': False,
            'policy': auth['policy'],
            'account_configuration': binding['configuration'],
        }
        with runtime.db:
            runtime.db.execute('INSERT INTO campaigns VALUES (?, ?)', (campaign['id'], json.dumps(campaign)))
            runtime.db.execute(
                'INSERT INTO dispatches VALUES (?, ?, ?, ?, ?)',
                (
                    campaign['id'],
                    'POST',
                    f"{auth['prefix']}-001",
                    json.dumps({'quantity': '0.03', 'price': '195.50'}),
                    json.dumps({'http_status': 200}),
                ),
            )
            runtime.db.execute(
                'INSERT INTO dispatches VALUES (?, ?, ?, ?, ?)',
                (
                    campaign['id'],
                    'POST',
                    f"{auth['prefix']}-002",
                    json.dumps({'quantity': '0.02', 'price': '196.00'}),
                    json.dumps({'http_status': 200}),
                ),
            )
            runtime._set('continuous_window', campaign['id'])
        if auth_state == 'STOPPED':
            runtime.stop()
        elif auth_state == 'HALTED':
            runtime.halt('test-risk-halt')

    grid_db_path = root / 'grid-supervisor.sqlite3'
    with closing(sqlite3.connect(grid_db_path)) as db:
        db.execute(
            'CREATE TABLE strategy_control (id INTEGER PRIMARY KEY CHECK(id=1), desired TEXT NOT NULL, configuration TEXT)'
        )
        db.execute(
            'INSERT INTO strategy_control VALUES (1, ?, ?)',
            (control_desired, json.dumps({'mode': 'execute', 'settings': {'symbol': 'NVDAUSDT'}})),
        )
        db.commit()

    journal_dir = root / JOURNAL_ID
    journal_dir.mkdir(parents=True, exist_ok=True)
    if storage.os.name != 'nt':
        journal_dir.chmod(0o700)

    protocol_path = journal_dir / 'protocol.sqlite3'
    with closing(sqlite3.connect(protocol_path)) as db:
        db.executescript('''
            CREATE TABLE futures_meta (id INTEGER PRIMARY KEY CHECK(id=1), account TEXT NOT NULL, reconciled INTEGER NOT NULL);
            CREATE TABLE futures_orders (id TEXT PRIMARY KEY, payload TEXT NOT NULL, quote TEXT NOT NULL);
            CREATE TABLE futures_events (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE futures_receipts (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE futures_funding (
                id TEXT PRIMARY KEY, tran_id TEXT NOT NULL, event_time_ms INTEGER NOT NULL,
                payload TEXT NOT NULL, raw_payload TEXT NOT NULL, observed_at TEXT NOT NULL
            );
            CREATE TABLE futures_income_coverage (
                scope TEXT PRIMARY KEY, covered_from_ms INTEGER NOT NULL, covered_through_ms INTEGER NOT NULL,
                complete INTEGER NOT NULL, updated_at TEXT NOT NULL, detail TEXT NOT NULL
            );
        ''')
        db.execute('INSERT INTO futures_meta VALUES (1, ?, 1)', (ACCOUNT_ID,))
        db.execute(
            'INSERT INTO futures_orders VALUES (?, ?, ?)',
            ('ord-1', json.dumps({'snapshot': {'status': 'FILLED'}, 'reconcile_required': False}), '5.865'),
        )
        db.execute(
            'INSERT INTO futures_orders VALUES (?, ?, ?)',
            ('ord-2', json.dumps({'snapshot': {'status': 'NEW'}, 'reconcile_required': False}), '3.920'),
        )
        db.execute('INSERT INTO futures_events VALUES (?, ?)', ('ev-1', json.dumps({'kind': 'ORDER_FILLED'})))
        db.execute(
            'INSERT INTO futures_receipts VALUES (?, ?, ?, ?)',
            ('rcpt-1', 'ord-1', '2026-11-22T12:00:00+00:00', json.dumps({'status': 'FILLED'})),
        )
        db.execute(
            'INSERT INTO futures_funding VALUES (?, ?, ?, ?, ?, ?)',
            ('f-1', 'tran-1001', 1795338000000, '{}', '{}', '2026-11-22T09:00:00+00:00'),
        )
        db.execute(
            'INSERT INTO futures_funding VALUES (?, ?, ?, ?, ?, ?)',
            ('f-2', 'tran-1002', 1795363200000, '{}', '{}', '2026-11-22T16:00:00+00:00'),
        )
        db.execute(
            'INSERT INTO futures_income_coverage VALUES (?, ?, ?, ?, ?, ?)',
            (
                'FUNDING_FEE',
                1795300000000,
                1795366000000,
                1 if funding_complete else 0,
                '2026-11-22T16:10:00+00:00',
                '{}',
            ),
        )
        db.commit()

    controller_path = journal_dir / 'controller.sqlite3'
    with closing(sqlite3.connect(controller_path)) as db:
        db.execute('CREATE TABLE events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        db.execute('CREATE TABLE snapshot (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        db.execute("INSERT INTO events VALUES (1, 'bar-1')")
        db.execute("INSERT INTO events VALUES (2, 'bar-2')")
        db.execute("INSERT INTO snapshot VALUES (1, 'snap-1')")
        db.commit()

    if storage.os.name != 'nt':
        for item in root.rglob('*'):
            item.chmod(0o700 if item.is_dir() else 0o600)
    return root


def _make_private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    if storage.os.name != 'nt':
        path.chmod(0o700)
    return path


def test_backup_verify_and_isolated_restore_drill(tmp_path):
    state = _seed_execute_state(tmp_path / 'execute-state')
    backup_root = _make_private_dir(tmp_path / 'backups')

    backup_dir = backup_mod.create_backup(
        state,
        backup_root,
        now_utc=datetime(2026, 11, 22, 17, 0, 0, tzinfo=timezone.utc),
    )
    assert backup_dir.name == 'backup-20261122T170000Z'
    assert not (backup_dir / 'secrets.json').exists()

    verified = backup_mod.verify_backup(backup_dir)
    assert verified['valid'] is True
    assert verified['database_count'] == 4
    assert verified['integrity_all_ok'] is True
    assert verified['binding']['journal_id'] == JOURNAL_ID
    assert verified['binding']['journal_id_matches_account'] is True
    assert verified['protocol']['meta_account_matches_binding'] is True
    assert verified['protocol']['orders_count'] == 2
    assert verified['protocol']['unknown_orders_count'] == 0
    assert verified['protocol']['funding_events_count'] == 2
    assert verified['protocol']['funding_continuity_ok'] is True
    assert verified['control']['durable_control_state'] == 'RUNNING'
    assert verified['control']['grid_control_desired'] == 'RUNNING'
    assert verified['runtime']['consumed_posts'] == 2
    assert Decimal(verified['runtime']['consumed_submitted_notional']) == Decimal('9.7850')
    assert verified['runtime']['remaining_posts'] == 98
    assert verified['controller']['events_count'] == 2
    assert SECRET_SENTINEL not in json.dumps(verified)
    assert ACCOUNT_ID not in json.dumps(verified)

    drill = backup_mod.drill_restore(backup_dir)
    assert drill['passed'] is True
    assert all(drill['checks'].values())
    assert drill['quarantine']['quarantine_file_present'] is True
    assert drill['quarantine']['execute_lock_blocked'] is True
    assert drill['quarantine']['get_only_lock_allowed'] is True
    assert drill['quarantine']['c05_handoff_blocks_restored'] is True
    assert drill['quarantine']['c05_handoff_blocks_backup'] is True
    assert drill['quarantine']['backup_manifest_present_in_restored'] is True
    # Drill workspace must be cleaned automatically when keep_restored=False.
    assert not Path(drill['isolated_restore_path']).exists()
    # Original state must remain untouched (no restore-quarantine added to live state).
    assert not (state / 'restore-quarantine.json').exists()


def test_restore_quarantine_blocks_direct_execute_and_c05_bypass(tmp_path):
    state = _seed_execute_state(tmp_path / 'execute-state')
    backup_root = _make_private_dir(tmp_path / 'backups')
    backup_dir = backup_mod.create_backup(
        state,
        backup_root,
        now_utc=datetime(2026, 11, 22, 17, 5, 0, tzinfo=timezone.utc),
    )

    drill = backup_mod.drill_restore(backup_dir, keep_restored=True)
    restored_dir = Path(drill['isolated_restore_path'])
    assert (restored_dir / 'restore-quarantine.json').is_file()

    with pytest.raises(SafetyBlocked, match='GET-only'):
        with operator_lock(restored_dir):
            pytest.fail('quarantined snapshot must refuse Execute lock')

    with operator_lock(restored_dir, allow_quarantined=True):
        pass

    with pytest.raises(SafetyBlocked, match='ordinary backup and restore roots cannot transfer Execute'):
        handoff._payload(restored_dir)

    with pytest.raises(SafetyBlocked, match='ordinary backup and restore roots cannot transfer Execute'):
        handoff._payload(backup_dir)

    # export_handoff must also refuse quarantined or manifest-bearing restored snapshots.
    with pytest.raises(SafetyBlocked):
        handoff.export_handoff(
            restored_dir,
            tmp_path / 'attempted-handoff',
            machine_id='a' * 32,
            state_root='/var/lib/factorlab-demo/execute-state',
        )


def test_prune_respects_retention_and_never_deletes_sole_recoverable_copy(tmp_path):
    state = _seed_execute_state(tmp_path / 'execute-state')
    backup_root = _make_private_dir(tmp_path / 'backups')

    old_only = backup_mod.create_backup(
        state,
        backup_root,
        now_utc=datetime(2026, 10, 1, 3, 15, 0, tzinfo=timezone.utc),
    )
    now = datetime(2026, 11, 22, 3, 15, 0, tzinfo=timezone.utc)

    # Even though the sole backup is 52 days old (> 14 days), prune MUST NOT delete it.
    first_prune = backup_mod.prune_backups(backup_root, retention_days=14, min_valid_copies=1, now_utc=now)
    assert first_prune['pruned_count'] == 0
    assert first_prune['kept_valid_count'] == 1
    assert old_only.is_dir()

    # Add an expired corrupted directory: still keep sole valid copy and clean expired corrupt dir.
    corrupt_old = backup_root / 'backup-20260915T031500Z'
    corrupt_old.mkdir(mode=0o700)
    (corrupt_old / 'runtime.sqlite3').write_bytes(b'corrupted')
    if storage.os.name != 'nt':
        (corrupt_old / 'runtime.sqlite3').chmod(0o600)

    # Add one fresh valid backup within 14 days and another expired valid backup.
    mid_old = backup_mod.create_backup(
        state,
        backup_root,
        now_utc=datetime(2026, 11, 1, 3, 15, 0, tzinfo=timezone.utc),
    )
    fresh = backup_mod.create_backup(
        state,
        backup_root,
        now_utc=datetime(2026, 11, 22, 3, 15, 0, tzinfo=timezone.utc),
    )
    unmanaged = _make_private_dir(backup_root / 'manual-archive-keep')

    second_prune = backup_mod.prune_backups(backup_root, retention_days=14, min_valid_copies=1, now_utc=now)
    assert second_prune['kept_valid_count'] == 1
    assert [item['name'] for item in second_prune['kept_valid']] == [fresh.name]
    assert fresh.is_dir()
    assert not old_only.exists()
    assert not mid_old.exists()
    assert not corrupt_old.exists()
    assert unmanaged.is_dir()

    with pytest.raises(ValueError, match='min_valid_copies'):
        backup_mod.prune_backups(backup_root, retention_days=14, min_valid_copies=0, now_utc=now)
    with pytest.raises(ValueError, match='retention_days'):
        backup_mod.prune_backups(backup_root, retention_days=0, min_valid_copies=1, now_utc=now)


def test_verify_rejects_broken_funding_continuity_and_secret_files(tmp_path):
    state = _seed_execute_state(tmp_path / 'execute-state', funding_complete=False)
    backup_root = _make_private_dir(tmp_path / 'backups')
    backup_dir = backup_mod.create_backup(
        state,
        backup_root,
        now_utc=datetime(2026, 11, 22, 17, 10, 0, tzinfo=timezone.utc),
    )
    with pytest.raises(SafetyBlocked, match='funding ledger continuity'):
        backup_mod.verify_backup(backup_dir)

    clean_state = _seed_execute_state(tmp_path / 'clean-state', funding_complete=True)
    clean_backup = backup_mod.create_backup(
        clean_state,
        backup_root,
        now_utc=datetime(2026, 11, 22, 17, 11, 0, tzinfo=timezone.utc),
    )
    secret_file = clean_backup / 'demo-secret.json'
    secret_file.write_text('{"secret":"bad"}', encoding='utf-8')
    if storage.os.name != 'nt':
        secret_file.chmod(0o600)
    with pytest.raises(SafetyBlocked, match='forbidden credential'):
        backup_mod.verify_backup(clean_backup)


class _FakeRunner:
    def __init__(self, *, active_unit='factorlab-demo-execute.service', pre_health=None,
                 post_health_sequence=None, fail_backup=False,
                 stop_error=None, backup_error=None, start_error=None):
        self.active_unit = active_unit
        self.currently_active = active_unit
        self.pre_health = pre_health
        self.post_health_sequence = list(post_health_sequence or [])
        self.fail_backup = fail_backup
        self.stop_error = stop_error
        self.backup_error = backup_error
        self.start_error = start_error
        self.events = []

    def is_unit_active(self, unit):
        return unit == self.currently_active

    def stop_unit(self, unit):
        self.events.append(('stop', unit))
        if self.currently_active == unit:
            self.currently_active = None
        if self.stop_error is not None:
            raise self.stop_error

    def start_unit(self, unit):
        self.events.append(('start', unit))
        if self.start_error is not None:
            raise self.start_error
        self.currently_active = unit

    def read_health(self, state_root, *, unit=None):
        self.events.append(('health', unit))
        if self.currently_active is None:
            return None
        if self.events.count(('health', unit)) == 1:
            return self.pre_health
        if self.post_health_sequence:
            return self.post_health_sequence.pop(0)
        return self.pre_health

    def run_python_module(self, module, args, *, state_root=None, timeout=300, stdin_text=None):
        self.events.append(('module', module.rsplit('.', 1)[-1], args[0]))
        if args[0] == 'backup' and self.fail_backup:
            return subprocess.CompletedProcess(args, 78, stdout='', stderr='simulated backup failure')
        if args[0] == 'backup' and self.backup_error is not None:
            raise self.backup_error
        cmd = [sys.executable, '-m', module, *args]
        env = dict(storage.os.environ)
        src_root = str(Path(__file__).resolve().parents[2] / 'src')
        env['PYTHONPATH'] = src_root + (storage.os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        if state_root is not None:
            env['FACTORLAB_DEMO_STATE_ROOT'] = str(state_root)
        return subprocess.run(cmd, input=stdin_text, capture_output=True, text=True, timeout=timeout, env=env)


def _health_fixture(*, pid, desired='RUNNING', consumed_posts=2, consumed_notional='9.7850',
                    execute_healthy=True, account_recovered=True, market_fresh=True, unknown_count=0):
    return {
        'mode': 'EXECUTE',
        'status': 'RUNNING' if desired == 'RUNNING' else desired,
        'desired': desired,
        'pid': pid,
        'alive': True,
        'ready': execute_healthy,
        'execute_healthy': execute_healthy,
        'account_recovered': account_recovered,
        'market_fresh': market_fresh,
        'unknown_count': unknown_count,
        'open_orders': [{'client_order_id': 'ord-2'}],
        'positions': [],
        'grid': {'deployment_id': 'fl-test-g-00001', 'state': desired},
        'global_budget': {
            'consumed': {'posts': consumed_posts, 'submitted_notional': consumed_notional},
            'remaining_posts': 100 - consumed_posts,
            'remaining_notional': str(Decimal('700') - Decimal(consumed_notional)),
        },
        'systemd': {
            'active_state': 'active',
            'sub_state': 'running',
            'main_pid': pid,
            'n_restarts': 0,
        },
    }


def test_scheduled_backup_stops_restarts_reconciles_verifies_and_prunes(tmp_path):
    state = _seed_execute_state(tmp_path / 'execute-state')
    backup_root = _make_private_dir(tmp_path / 'backups')
    expired = backup_mod.create_backup(
        state,
        backup_root,
        now_utc=datetime(2026, 10, 1, 3, 15, 0, tzinfo=timezone.utc),
    )

    runner = _FakeRunner(
        active_unit='factorlab-demo-execute.service',
        pre_health=_health_fixture(pid=1001),
        post_health_sequence=[
            _health_fixture(pid=1001),  # old heartbeat still present briefly
            _health_fixture(pid=2002, execute_healthy=False, account_recovered=False),
            _health_fixture(pid=2002, execute_healthy=True, account_recovered=True),
        ],
    )
    now_dt = datetime(2026, 11, 22, 3, 15, 0, tzinfo=timezone.utc)
    ticks = iter([10.0, 12.5, 13.0, 14.0, 15.0, 16.0])

    result = backup_mod.run_scheduled_backup(
        state,
        backup_root,
        retention_days=14,
        min_valid_copies=1,
        min_free_bytes=0,
        drill=True,
        runner=runner,
        now_fn=lambda: now_dt,
        sleep_fn=lambda _: None,
        monotonic_fn=lambda: next(ticks),
    )

    assert result['status'] == 'SUCCEEDED'
    assert result['error_category'] is None
    assert result['verified'] is True
    assert result['database_count'] == 4
    assert result['drill_restore']['passed'] is True
    assert result['maintenance_window']['interrupted_unit'] == 'factorlab-demo-execute.service'
    assert result['maintenance_window']['restart_succeeded'] is True
    assert result['maintenance_window']['downtime_seconds'] == 2.5
    assert result['maintenance_window']['reconcile_verified'] is True
    assert result['retention']['pruned_count'] == 1
    assert result['retention']['kept_valid_count'] == 1
    assert not expired.exists()

    # Verify stop -> backup -> start order (verify/drill/prune happen AFTER restart).
    stop_idx = runner.events.index(('stop', 'factorlab-demo-execute.service'))
    backup_idx = runner.events.index(('module', 'demo_cloud_runtime', 'backup'))
    start_idx = runner.events.index(('start', 'factorlab-demo-execute.service'))
    verify_idx = runner.events.index(('module', 'demo_cloud_backup', 'verify'))
    prune_idx = runner.events.index(('module', 'demo_cloud_backup', 'prune'))
    assert stop_idx < backup_idx < start_idx < verify_idx < prune_idx

    # Status and history files must be written and contain zero secrets.
    status_file = backup_root / backup_mod.STATUS_FILENAME
    history_file = backup_root / backup_mod.HISTORY_FILENAME
    assert status_file.is_file()
    assert history_file.is_file()
    assert SECRET_SENTINEL not in status_file.read_text(encoding='utf-8')
    assert SECRET_SENTINEL not in history_file.read_text(encoding='utf-8')
    assert len(history_file.read_text(encoding='utf-8').splitlines()) == 1


def test_scheduled_backup_failure_restarts_service_and_skips_prune(tmp_path):
    state = _seed_execute_state(tmp_path / 'execute-state')
    backup_root = _make_private_dir(tmp_path / 'backups')
    expired = backup_mod.create_backup(
        state,
        backup_root,
        now_utc=datetime(2026, 10, 1, 3, 15, 0, tzinfo=timezone.utc),
    )
    runner = _FakeRunner(
        active_unit='factorlab-demo-execute.service',
        pre_health=_health_fixture(pid=1001),
        post_health_sequence=[_health_fixture(pid=2002)],
        fail_backup=True,
    )

    result = backup_mod.run_scheduled_backup(
        state,
        backup_root,
        retention_days=14,
        min_valid_copies=1,
        min_free_bytes=0,
        runner=runner,
        sleep_fn=lambda _: None,
    )

    assert result['status'] == 'FAILED'
    assert result['verified'] is False
    assert result['maintenance_window']['restart_succeeded'] is True
    assert runner.currently_active == 'factorlab-demo-execute.service'
    assert ('start', 'factorlab-demo-execute.service') in runner.events
    assert ('module', 'demo_cloud_backup', 'prune') not in runner.events
    # Existing backup MUST remain intact after failed backup!
    assert expired.is_dir()


@pytest.mark.parametrize('stop_error', [
    subprocess.TimeoutExpired('systemctl stop', 120),
    subprocess.CalledProcessError(1, 'systemctl stop'),
])
def test_partial_stop_failure_restores_and_reconciles_before_publishing(tmp_path, stop_error):
    state = _seed_execute_state(tmp_path / 'execute-state')
    backup_root = _make_private_dir(tmp_path / 'backups')
    runner = _FakeRunner(
        pre_health=_health_fixture(pid=1001),
        post_health_sequence=[_health_fixture(pid=2002)],
        stop_error=stop_error,
    )

    result = backup_mod.run_scheduled_backup(state, backup_root, min_free_bytes=0, runner=runner)

    assert result['error_category'] == 'SERVICE_STOP_FAILED'
    assert result['maintenance_window']['reconcile_verified'] is True
    assert result['maintenance_window']['post_health']['pid'] == 2002
    assert ('module', 'demo_cloud_runtime', 'backup') not in runner.events
    assert runner.currently_active == 'factorlab-demo-execute.service'


def test_backup_child_timeout_reconciles_and_never_prunes(tmp_path):
    state = _seed_execute_state(tmp_path / 'execute-state')
    backup_root = _make_private_dir(tmp_path / 'backups')
    runner = _FakeRunner(
        pre_health=_health_fixture(pid=1001),
        post_health_sequence=[
            _health_fixture(pid=2002, unknown_count=1),
            _health_fixture(pid=2002),
        ],
        backup_error=subprocess.TimeoutExpired('backup', 300),
    )

    result = backup_mod.run_scheduled_backup(
        state, backup_root, min_free_bytes=0, runner=runner, sleep_fn=lambda _: None,
    )

    assert result['error_category'] == 'BACKUP_TIMEOUT'
    assert result['maintenance_window']['reconcile_verified'] is True
    assert result['maintenance_window']['post_health']['unknown_count'] == 0
    assert ('module', 'demo_cloud_backup', 'prune') not in runner.events


@pytest.mark.parametrize('retry_works', [False, True])
def test_start_failure_leaves_recovery_for_stop_hook(tmp_path, retry_works):
    state = _seed_execute_state(tmp_path / 'execute-state')
    backup_root = _make_private_dir(tmp_path / 'backups')
    recovery_file = _make_private_dir(tmp_path / 'run') / 'invocation'
    runner = _FakeRunner(
        pre_health=_health_fixture(pid=1001),
        post_health_sequence=[_health_fixture(pid=2002)],
        fail_backup=True,
        start_error=subprocess.CalledProcessError(1, 'systemctl start'),
    )
    result = backup_mod.run_scheduled_backup(
        state, backup_root, min_free_bytes=0, runner=runner, recovery_file=recovery_file,
    )
    assert result['error_category'] == 'SERVICE_RESTART_FAILED'
    assert runner.currently_active is None

    if retry_works:
        runner.start_error = None
    backup_mod.recover_scheduled_backup(state, backup_root, recovery_file, runner=runner)
    published = json.loads((backup_root / backup_mod.STATUS_FILENAME).read_text(encoding='utf-8'))
    assert published['status'] == 'FAILED'
    assert published['error_category'] == 'SERVICE_RESTART_FAILED'
    assert published['maintenance_window']['reconcile_verified'] is retry_works
    assert (runner.currently_active is not None) is retry_works


@pytest.mark.parametrize('control', ['STOPPED', 'HALTED'])
@pytest.mark.parametrize('active', [False, True])
def test_stopped_or_halted_state_never_arms_recovery(tmp_path, control, active):
    state = _seed_execute_state(tmp_path / 'execute-state', control_desired=control, auth_state=control)
    backup_root = _make_private_dir(tmp_path / 'backups')
    recovery_file = _make_private_dir(tmp_path / 'run') / 'invocation'
    unit = 'factorlab-demo-execute.service'
    runner = _FakeRunner(active_unit=unit if active else None, pre_health=_health_fixture(pid=1001))

    result = backup_mod.run_scheduled_backup(
        state, backup_root, min_free_bytes=0, runner=runner, recovery_file=recovery_file,
    )
    backup_mod.recover_scheduled_backup(state, backup_root, recovery_file, runner=runner)

    assert result['error_category'] == ('CONTROL_STATE_BLOCKED' if active else None)
    assert ('stop', unit) not in runner.events
    assert ('start', unit) not in runner.events


def test_scheduled_backup_low_disk_refuses_before_stopping_execute(tmp_path):
    state = _seed_execute_state(tmp_path / 'execute-state')
    backup_root = _make_private_dir(tmp_path / 'backups')
    runner = _FakeRunner(
        active_unit='factorlab-demo-execute.service',
        pre_health=_health_fixture(pid=1001),
    )

    result = backup_mod.run_scheduled_backup(
        state,
        backup_root,
        min_free_bytes=10**18,  # impossibly large reserve
        runner=runner,
        sleep_fn=lambda _: None,
    )

    assert result['status'] == 'FAILED'
    assert result['error_category'] == 'DISK_SPACE_LOW'
    assert ('stop', 'factorlab-demo-execute.service') not in runner.events
    assert runner.currently_active == 'factorlab-demo-execute.service'


def test_scheduled_backup_preserves_stopped_and_paused_controls(tmp_path):
    stopped_state = _seed_execute_state(
        tmp_path / 'stopped-state',
        control_desired='STOPPED',
        auth_state='STOPPED',
    )
    backup_root = _make_private_dir(tmp_path / 'backups-stopped')
    runner = _FakeRunner(active_unit=None)

    result = backup_mod.run_scheduled_backup(
        stopped_state,
        backup_root,
        min_free_bytes=0,
        runner=runner,
        sleep_fn=lambda _: None,
    )

    assert result['status'] == 'SUCCEEDED'
    assert result['verification']['control']['durable_control_state'] == 'STOPPED'
    assert result['verification']['runtime']['consumed_posts'] == 2
    assert ('start', 'factorlab-demo-execute.service') not in runner.events
    assert runner.currently_active is None

    paused_state = _seed_execute_state(
        tmp_path / 'paused-state',
        control_desired='PAUSED',
        auth_state='RUNNING',
    )
    backup_root_paused = _make_private_dir(tmp_path / 'backups-paused')
    paused_runner = _FakeRunner(
        active_unit='factorlab-demo-execute.service',
        pre_health=_health_fixture(pid=3001, desired='PAUSED', execute_healthy=False),
        post_health_sequence=[_health_fixture(pid=3002, desired='PAUSED', execute_healthy=False)],
    )
    paused_result = backup_mod.run_scheduled_backup(
        paused_state,
        backup_root_paused,
        min_free_bytes=0,
        runner=paused_runner,
        sleep_fn=lambda _: None,
    )
    assert paused_result['status'] == 'SUCCEEDED'
    assert paused_result['maintenance_window']['reconcile_verified'] is True
    assert paused_result['verification']['control']['durable_control_state'] == 'PAUSED'


def test_systemd_backup_units_follow_security_and_schedule_contracts():
    repo_root = Path(__file__).resolve().parents[2]
    service_text = (repo_root / 'deploy' / 'systemd' / 'factorlab-demo-backup.service').read_text(encoding='utf-8')
    timer_text = (repo_root / 'deploy' / 'systemd' / 'factorlab-demo-backup.timer').read_text(encoding='utf-8')

    assert 'Type=oneshot' in service_text
    assert 'demo_cloud_backup run-scheduled' in service_text
    assert 'LoadCredential' not in service_text
    assert 'demo-secret.json' not in service_text
    assert 'InaccessiblePaths=/etc/factorlab-demo' in service_text
    assert 'factorlab-demo stop' not in service_text
    assert 'UMask=0077' in service_text

    assert 'Unit=factorlab-demo-backup.service' in timer_text
    assert 'OnCalendar=*-*-* 03:15:00 UTC' in timer_text
    assert 'Persistent=true' in timer_text


def _seed_external_mailbox(tmp_path):
    from factorlab.integrations.binance_futures_trading.tradfi_tasks import TaskMailbox
    directory = _make_private_dir(tmp_path / 'observer')
    path = directory / 'tasks.sqlite3'
    with closing(TaskMailbox(path)) as mailbox:
        mailbox.db.execute("INSERT INTO portfolio_tasks VALUES ('task-1', '{}', 'RECONCILING')")
        mailbox.db.execute("INSERT INTO portfolio_receipts VALUES (1, 'task-1', '2026-11-22', 'RECONCILING', '{}')")
        mailbox.db.commit()
    path.chmod(0o600)
    return path


def test_external_mailbox_backup_and_isolated_restore(tmp_path):
    state = _seed_execute_state(tmp_path / 'state')
    mailbox = _seed_external_mailbox(tmp_path)
    root = _make_private_dir(tmp_path / 'backups')
    snapshot = backup_mod.create_backup(state, root, task_mailbox=mailbox)
    summary = backup_mod.verify_backup(snapshot)['task_mailbox']
    assert summary['rows'] == {'portfolio_tasks': 1, 'portfolio_receipts': 1}
    with closing(sqlite3.connect(mailbox)) as db:
        db.execute("UPDATE portfolio_tasks SET status='COMPLETED'")
        db.commit()
    drill = backup_mod.drill_restore(snapshot, keep_restored=True)
    assert drill['checks']['task_mailbox_match']
    restored = Path(drill['isolated_restore_path']) / storage._TASK_MAILBOX
    with closing(sqlite3.connect(restored)) as db:
        assert db.execute('SELECT status FROM portfolio_tasks').fetchone() == ('RECONCILING',)
    with closing(sqlite3.connect(mailbox)) as db:
        assert db.execute('SELECT status FROM portfolio_tasks').fetchone() == ('COMPLETED',)


def test_external_mailbox_writer_contention_refuses_snapshot(tmp_path):
    state = _seed_execute_state(tmp_path / 'state')
    mailbox = _seed_external_mailbox(tmp_path)
    root = _make_private_dir(tmp_path / 'backups')
    with closing(sqlite3.connect(mailbox)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            backup_mod.create_backup(state, root, task_mailbox=mailbox)
    assert not list(root.iterdir())


def test_scheduled_external_mailbox_stops_all_writers(tmp_path):
    state = _seed_execute_state(tmp_path / 'state')
    mailbox = _seed_external_mailbox(tmp_path)
    root = _make_private_dir(tmp_path / 'backups')

    class Runner(_FakeRunner):
        def __init__(self):
            super().__init__(pre_health=_health_fixture(pid=1001),
                             post_health_sequence=[_health_fixture(pid=2002)])
            self.auxiliary = set(backup_mod.MAILBOX_WRITER_UNITS)

        def is_unit_active(self, unit):
            return unit in self.auxiliary or super().is_unit_active(unit)

        def stop_unit(self, unit):
            self.auxiliary.discard(unit)
            super().stop_unit(unit)

        def start_unit(self, unit):
            if unit in backup_mod.MAILBOX_WRITER_UNITS:
                self.events.append(('start', unit))
                self.auxiliary.add(unit)
            else:
                super().start_unit(unit)

    runner = Runner()
    result = backup_mod.run_scheduled_backup(state, root, task_mailbox=mailbox,
                                             runner=runner, drill=True)
    assert result['status'] == 'SUCCEEDED'
    stops = [event[1] for event in runner.events if event[0] == 'stop']
    assert stops == [*backup_mod.MAILBOX_WRITER_UNITS, 'factorlab-demo-execute.service']
    starts = [event[1] for event in runner.events if event[0] == 'start']
    assert starts == ['factorlab-demo-execute.service', *reversed(backup_mod.MAILBOX_WRITER_UNITS)]
    assert result['verification']['task_mailbox']['present']
    assert result['drill_restore']['checks']['task_mailbox_match']


def test_auxiliary_only_recovery_restarts_timer_last():
    class Runner:
        def __init__(self):
            self.started = []
        def is_unit_active(self, unit):
            return False
        def start_unit(self, unit):
            self.started.append(unit)
    runner = Runner()
    window = dict(interrupted_unit=None, auxiliary_units=list(backup_mod.MAILBOX_WRITER_UNITS))
    assert backup_mod._restore_maintenance(runner, None, window, reconcile_timeout=1) is None
    assert runner.started == list(reversed(backup_mod.MAILBOX_WRITER_UNITS))
