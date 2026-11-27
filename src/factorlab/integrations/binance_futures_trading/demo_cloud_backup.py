"""NO LIVE MONEY: Scheduled coherent backup, retention, and isolated DR verification."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

from . import demo_cloud_handoff as handoff
from . import demo_cloud_storage as storage
from .demo_runtime import journal_id, operator_lock
from .protocol import SafetyBlocked


CONFIG_ERROR = 78
MIN_FREE_BYTES = 256 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 14
DEFAULT_MIN_VALID_COPIES = 1
DEFAULT_RECONCILE_TIMEOUT = 90.0
DEFAULT_UNITS = ('factorlab-demo-execute.service', 'factorlab-demo.service')
MAILBOX_WRITER_UNITS = (
    'factorlab-demo-strategy-deploy.timer',
    'factorlab-demo-strategy-deploy.service',
    'factorlab-demo-tradfi-observer.service',
)

BACKUP_PREFIX = 'backup-'
BACKUP_TIMESTAMP_FORMAT = '%Y%m%dT%H%M%SZ'
_BACKUP_NAME_RE = re.compile(r'^backup-(\d{8}T\d{6}Z)(?:-(\d+))?$')

STATUS_FILENAME = 'backup-status.json'
HISTORY_FILENAME = 'backup-history.jsonl'

_FORBIDDEN_FILENAMES = frozenset({
    'demo-secret.json',
    'secrets.json',
    'secret.json',
    'credentials.json',
    '.env',
})
_FORBIDDEN_SUFFIXES = ('.pem', '.key', '.p12', '.pfx')


def utc_now():
    return datetime.now(timezone.utc)


def classify_backup_error(exc):
    message = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError) and ('locked' in message or 'busy' in message):
        return 'LOCK_CONTENTION'
    if isinstance(exc, sqlite3.DatabaseError) or 'integrity check failed' in message:
        return 'SQLITE_INTEGRITY'
    if 'disk_space_low' in message or 'insufficient' in message or 'no space left' in message:
        return 'DISK_SPACE_LOW'
    if 'another demo session' in message or 'locked' in message:
        return 'LOCK_CONTENTION'
    if 'incomplete backup' in message or 'manifest' in message:
        return 'BACKUP_INCOMPLETE'
    if 'quarantine' in message or 'sealed' in message or 'cannot transfer execute' in message:
        return 'RESTORE_QUARANTINE'
    if 'credentials' in message or 'secret' in message:
        return 'SECRET_POLICY_VIOLATION'
    if isinstance(exc, PermissionError) or any(
        token in message for token in ('private', 'owned', 'symlink', 'hard link')
    ):
        return 'PERMISSION_OR_PATH_ERROR'
    return 'BACKUP_OPERATION_ERROR'


def parse_backup_timestamp(name):
    match = _BACKUP_NAME_RE.fullmatch(name)
    if match is None:
        return None
    try:
        parsed = datetime.strptime(match.group(1), BACKUP_TIMESTAMP_FORMAT)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def generate_backup_name(backup_root, *, now_utc=None):
    backup_root = Path(backup_root)
    stamp = (now_utc or utc_now()).astimezone(timezone.utc).strftime(BACKUP_TIMESTAMP_FORMAT)
    base = f'{BACKUP_PREFIX}{stamp}'
    if not (backup_root / base).exists():
        return base
    for index in range(1, 1000):
        candidate = f'{base}-{index:02d}'
        if not (backup_root / candidate).exists():
            return candidate
    raise SafetyBlocked('unable to allocate unique backup directory name')


def _readonly_sqlite(path):
    return sqlite3.connect(
        Path(path).resolve().as_uri() + '?mode=ro',
        uri=True,
        timeout=0,
        isolation_level=None,
    )


def _table_exists(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _account_fingerprint(account_id):
    return hashlib.sha256(str(account_id).encode('utf-8')).hexdigest()[:12]


def _check_no_forbidden_secret_files(root, files):
    for path in files:
        lower = path.name.lower()
        if lower in _FORBIDDEN_FILENAMES or any(lower.endswith(suffix) for suffix in _FORBIDDEN_SUFFIXES):
            raise SafetyBlocked(f'forbidden credential or key file found in snapshot: {path.relative_to(root).as_posix()}')


def _read_runtime_summary(runtime_path):
    summary = dict(
        present=runtime_path.exists(),
        durable_control_state=None,
        continuous_authorization_present=False,
        authorization_state=None,
        diagnostic_present=False,
        campaigns_count=0,
        dispatches_count=0,
        consumed_posts=None,
        consumed_submitted_notional=None,
        remaining_posts=None,
        remaining_notional=None,
        total_orders=None,
        total_notional=None,
        opening_equity=None,
        max_loss=None,
        active_window_id=None,
        authorization_journal_id=None,
    )
    if not runtime_path.exists():
        return summary
    with closing(_readonly_sqlite(runtime_path)) as db:
        if not _table_exists(db, 'runtime'):
            return summary
        kv = dict(db.execute('SELECT key, value FROM runtime').fetchall())
        if _table_exists(db, 'campaigns'):
            summary['campaigns_count'] = db.execute('SELECT COUNT(*) FROM campaigns').fetchone()[0]
        if _table_exists(db, 'dispatches'):
            summary['dispatches_count'] = db.execute('SELECT COUNT(*) FROM dispatches').fetchone()[0]
        raw_auth = kv.get('continuous')
        diagnostic = kv.get('continuous_diagnostic')
        summary['diagnostic_present'] = diagnostic is not None and diagnostic != ''
        authorization = json.loads(raw_auth) if raw_auth else None
        if authorization is not None:
            summary['continuous_authorization_present'] = True
            summary['authorization_state'] = authorization.get('state')
            summary['authorization_journal_id'] = authorization.get('journal_id')
            summary['total_orders'] = authorization.get('total_orders')
            summary['total_notional'] = authorization.get('total_notional')
            summary['opening_equity'] = authorization.get('opening_equity')
            summary['max_loss'] = authorization.get('max_loss')
            summary['active_window_id'] = kv.get('continuous_window') or None
            # Replicate DemoRuntime.consumption(authorization) strictly read-only.
            auth_journal = authorization.get('journal_id')
            owner_campaigns = set()
            if _table_exists(db, 'campaigns'):
                for (payload,) in db.execute('SELECT payload FROM campaigns ORDER BY rowid'):
                    campaign = json.loads(payload)
                    if campaign.get('journal_id') == auth_journal and not campaign.get('smoke'):
                        owner_campaigns.add(campaign['id'])
            posts = 0
            notional = Decimal(0)
            if _table_exists(db, 'dispatches'):
                for camp_id, raw_params in db.execute(
                    "SELECT campaign, parameters FROM dispatches WHERE method='POST'"
                ):
                    if camp_id in owner_campaigns:
                        params = json.loads(raw_params)
                        posts += 1
                        notional += Decimal(params['quantity']) * Decimal(params['price'])
            summary['consumed_posts'] = posts
            summary['consumed_submitted_notional'] = str(notional)
            total_orders = authorization.get('total_orders')
            total_notional = authorization.get('total_notional')
            if isinstance(total_orders, int):
                summary['remaining_posts'] = max(0, total_orders - posts)
            if total_notional is not None:
                summary['remaining_notional'] = str(max(Decimal(0), Decimal(total_notional) - notional))
            if authorization.get('state') == 'HALTED' or summary['diagnostic_present']:
                summary['durable_control_state'] = 'HALTED'
            elif authorization.get('state') == 'STOPPED':
                summary['durable_control_state'] = 'STOPPED'
        elif kv.get('stop_epoch') is not None and not kv.get('active'):
            summary['durable_control_state'] = 'STOPPED'
    return summary


def _read_grid_control_summary(root, runtime_durable_state):
    result = dict(
        grid_supervisor_present=(root / 'grid-supervisor.sqlite3').exists(),
        grid_control_desired=None,
        grid_mode=None,
        durable_control_state=runtime_durable_state,
    )
    paused = False
    stopped = False
    for filename in ('grid-supervisor.sqlite3', 'strategy-execute.sqlite3'):
        path = root / filename
        if not path.exists():
            continue
        with closing(_readonly_sqlite(path)) as db:
            if not _table_exists(db, 'strategy_control'):
                continue
            columns = {row[1] for row in db.execute('PRAGMA table_info(strategy_control)').fetchall()}
            row = db.execute('SELECT desired FROM strategy_control WHERE id=1').fetchone()
            desired = row[0] if row else None
            if filename == 'grid-supervisor.sqlite3':
                result['grid_control_desired'] = desired
                if 'configuration' in columns:
                    cfg_row = db.execute('SELECT configuration FROM strategy_control WHERE id=1').fetchone()
                    if cfg_row and cfg_row[0]:
                        cfg = json.loads(cfg_row[0])
                        result['grid_mode'] = cfg.get('mode')
            if desired == 'STOPPED':
                stopped = True
            elif desired == 'PAUSED':
                paused = True
    if result['durable_control_state'] is None:
        if stopped:
            result['durable_control_state'] = 'STOPPED'
        elif paused:
            result['durable_control_state'] = 'PAUSED'
        elif result['grid_control_desired'] is not None:
            result['durable_control_state'] = result['grid_control_desired']
        else:
            result['durable_control_state'] = 'READ_ONLY'
    return result


def _read_protocol_summary(protocol_path, expected_account_id):
    summary = dict(
        present=protocol_path.exists(),
        meta_account_matches_binding=None,
        orders_count=0,
        events_count=0,
        receipts_count=0,
        rejections_count=0,
        order_recovery_count=0,
        unknown_orders_count=0,
        funding_events_count=0,
        funding_tran_ids_unique=True,
        funding_time_monotonic=True,
        funding_earliest_ms=None,
        funding_latest_ms=None,
        funding_coverage=None,
        funding_continuity_ok=True,
    )
    if not protocol_path.exists():
        return summary
    with closing(_readonly_sqlite(protocol_path)) as db:
        if _table_exists(db, 'futures_meta'):
            row = db.execute('SELECT account FROM futures_meta WHERE id=1').fetchone()
            if row is not None and expected_account_id is not None:
                summary['meta_account_matches_binding'] = (row[0] == expected_account_id)
        if _table_exists(db, 'futures_orders'):
            from factorlab.trading.storage import decode

            rows = db.execute('SELECT id, payload FROM futures_orders').fetchall()
            summary['orders_count'] = len(rows)
            unknown_count = 0
            status_counts = {}
            for _, raw in rows:
                try:
                    raw_obj = json.loads(raw)
                except ValueError:
                    continue
                status = None
                reconcile_required = False
                if isinstance(raw_obj, dict) and 'type' in raw_obj and 'fields' in raw_obj:
                    try:
                        record = decode(raw_obj)
                        snap = getattr(record, 'snapshot', None)
                        raw_status = getattr(snap, 'status', None)
                        status = raw_status.value if hasattr(raw_status, 'value') else (
                            str(raw_status) if raw_status is not None else None
                        )
                        reconcile_required = bool(getattr(record, 'reconcile_required', False))
                    except Exception:
                        fields = raw_obj.get('fields') or {}
                        snap = fields.get('snapshot') or {}
                        if isinstance(snap, dict) and 'fields' in snap:
                            snap = snap['fields']
                        status = snap.get('status') if isinstance(snap, dict) else None
                        reconcile_required = bool(fields.get('reconcile_required', False))
                elif isinstance(raw_obj, dict):
                    snap = raw_obj.get('snapshot')
                    status = snap.get('status') if isinstance(snap, dict) else raw_obj.get('status')
                    reconcile_required = bool(raw_obj.get('reconcile_required', False))
                if status is not None:
                    status_counts[str(status)] = status_counts.get(str(status), 0) + 1
                if status == 'UNKNOWN' or reconcile_required:
                    unknown_count += 1
            summary['unknown_orders_count'] = unknown_count
            summary['orders_by_status'] = status_counts
        if _table_exists(db, 'futures_events'):
            summary['events_count'] = db.execute('SELECT COUNT(*) FROM futures_events').fetchone()[0]
        if _table_exists(db, 'futures_receipts'):
            summary['receipts_count'] = db.execute('SELECT COUNT(*) FROM futures_receipts').fetchone()[0]
        if _table_exists(db, 'futures_rejections'):
            summary['rejections_count'] = db.execute('SELECT COUNT(*) FROM futures_rejections').fetchone()[0]
        if _table_exists(db, 'futures_order_recovery'):
            summary['order_recovery_count'] = db.execute('SELECT COUNT(*) FROM futures_order_recovery').fetchone()[0]
        if _table_exists(db, 'futures_funding'):
            funding_rows = db.execute(
                'SELECT tran_id, event_time_ms FROM futures_funding ORDER BY event_time_ms, tran_id'
            ).fetchall()
            summary['funding_events_count'] = len(funding_rows)
            tran_ids = [row[0] for row in funding_rows]
            times = [row[1] for row in funding_rows]
            summary['funding_tran_ids_unique'] = len(tran_ids) == len(set(tran_ids))
            summary['funding_time_monotonic'] = all(
                times[idx] <= times[idx + 1] for idx in range(len(times) - 1)
            )
            if times:
                summary['funding_earliest_ms'] = times[0]
                summary['funding_latest_ms'] = times[-1]
        if _table_exists(db, 'futures_income_coverage'):
            cov = db.execute(
                "SELECT covered_from_ms, covered_through_ms, complete, updated_at "
                "FROM futures_income_coverage WHERE scope='FUNDING_FEE'"
            ).fetchone()
            if cov is not None:
                coverage = dict(
                    covered_from_ms=cov[0],
                    covered_through_ms=cov[1],
                    complete=bool(cov[2]),
                    updated_at=cov[3],
                )
                summary['funding_coverage'] = coverage
                bounds_ok = (
                    coverage['complete']
                    and coverage['covered_from_ms'] <= coverage['covered_through_ms']
                )
                if summary['funding_earliest_ms'] is not None:
                    bounds_ok = (
                        bounds_ok
                        and coverage['covered_from_ms'] <= summary['funding_earliest_ms']
                        and summary['funding_latest_ms'] <= coverage['covered_through_ms']
                    )
                summary['funding_continuity_ok'] = (
                    bounds_ok and summary['funding_tran_ids_unique'] and summary['funding_time_monotonic']
                )
            else:
                summary['funding_continuity_ok'] = (
                    summary['funding_tran_ids_unique'] and summary['funding_time_monotonic']
                )
    return summary


def _read_controller_summary(controller_path):
    summary = dict(
        present=controller_path.exists(),
        events_count=0,
        snapshot_count=0,
    )
    if not controller_path.exists():
        return summary
    with closing(_readonly_sqlite(controller_path)) as db:
        if _table_exists(db, 'events'):
            summary['events_count'] = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        if _table_exists(db, 'snapshot'):
            summary['snapshot_count'] = db.execute('SELECT COUNT(*) FROM snapshot').fetchone()[0]
    return summary


def _read_task_mailbox_summary(root):
    path = root / storage._TASK_MAILBOX
    if not path.exists():
        return dict(present=False)
    digest = hashlib.sha256()
    counts = {}
    with closing(_readonly_sqlite(path)) as db:
        for table, order in (('portfolio_tasks', 'task_id'), ('portfolio_receipts', 'sequence')):
            count = 0
            for row in db.execute(f'SELECT * FROM {table} ORDER BY {order}'):
                digest.update(json.dumps([table, list(row)], separators=(',', ':')).encode('utf-8'))
                digest.update(bytes([10]))
                count += 1
            counts[table] = count
    return dict(present=True, rows=counts, content_sha256=digest.hexdigest())


def inspect_state_snapshot(root, *, expect_backup_manifest=False, expect_quarantine=False):
    """Read-only deep verification of a state or backup directory without touching live locks."""
    root = storage._root(root)
    databases, files = storage._inventory(root)
    _check_no_forbidden_secret_files(root, files)
    rel_databases = [path.relative_to(root).as_posix() for path in databases]

    manifest_path = root / storage._BACKUP_MANIFEST
    manifest_present = manifest_path.exists()
    if expect_backup_manifest:
        if not manifest_present:
            raise SafetyBlocked('backup manifest missing')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        expected_manifest = dict(
            version=1,
            databases=rel_databases,
            binding=(root / 'binding.json').exists(),
        )
        if manifest != expected_manifest:
            raise SafetyBlocked('incomplete backup inventory')

    quarantine_path = root / storage._QUARANTINE
    quarantine_present = quarantine_path.exists()
    quarantine_reason = None
    if quarantine_present:
        quarantine_data = json.loads(quarantine_path.read_text(encoding='utf-8'))
        quarantine_reason = quarantine_data.get('reason')
    if expect_quarantine:
        if not quarantine_present or quarantine_reason != 'restored-state-requires-GET-only-recovery':
            raise SafetyBlocked('restore quarantine marker missing or invalid')

    integrity = {}
    for db_path in databases:
        storage._integrity(db_path, recover=False)
        integrity[db_path.relative_to(root).as_posix()] = 'ok'

    metrics = storage._storage_metrics(root, databases, files)
    binding_path = root / 'binding.json'
    binding_summary = dict(
        present=binding_path.exists(),
        environment=None,
        host=None,
        journal_id=None,
        account_id_sha256_prefix=None,
        journal_id_matches_account=None,
    )
    expected_account_id = None
    bound_journal_id = None
    if binding_path.exists():
        binding = storage._binding(binding_path)
        expected_account_id = binding.get('account_id')
        bound_journal_id = binding.get('journal_id')
        binding_summary.update(
            environment=binding.get('environment'),
            host=binding.get('host'),
            journal_id=bound_journal_id,
            account_id_sha256_prefix=_account_fingerprint(expected_account_id) if expected_account_id else None,
            journal_id_matches_account=(
                journal_id(expected_account_id) == bound_journal_id
                if expected_account_id and bound_journal_id else None
            ),
        )
        if binding_summary['journal_id_matches_account'] is False:
            raise SafetyBlocked('binding journal_id does not match bound account_id')

    runtime_summary = _read_runtime_summary(root / 'runtime.sqlite3')
    control_summary = _read_grid_control_summary(root, runtime_summary['durable_control_state'])
    if bound_journal_id and runtime_summary['authorization_journal_id']:
        if bound_journal_id != runtime_summary['authorization_journal_id']:
            raise SafetyBlocked('continuous authorization journal_id does not match binding')

    protocol_path = (root / bound_journal_id / 'protocol.sqlite3') if bound_journal_id else (root / 'protocol.sqlite3')
    controller_path = (root / bound_journal_id / 'controller.sqlite3') if bound_journal_id else (root / 'controller.sqlite3')
    protocol_summary = _read_protocol_summary(protocol_path, expected_account_id)
    if protocol_summary['meta_account_matches_binding'] is False:
        raise SafetyBlocked('protocol futures_meta account does not match binding')
    if not protocol_summary['funding_continuity_ok']:
        raise SafetyBlocked('funding ledger continuity check failed')
    controller_summary = _read_controller_summary(controller_path)

    return dict(
        valid=True,
        path=str(root),
        database_count=len(databases),
        databases=rel_databases,
        integrity_all_ok=True,
        database_bytes=metrics['database_bytes'],
        sidecar_bytes=metrics['sidecar_bytes'],
        total_bytes=metrics['state_bytes'],
        backup_manifest_present=manifest_present,
        quarantine_present=quarantine_present,
        quarantine_reason=quarantine_reason,
        secret_files_excluded=True,
        binding=binding_summary,
        runtime=runtime_summary,
        control=control_summary,
        protocol=protocol_summary,
        controller=controller_summary,
        task_mailbox=_read_task_mailbox_summary(root),
    )


def verify_backup(backup_dir):
    """Validate a completed backup snapshot directory."""
    return inspect_state_snapshot(
        backup_dir,
        expect_backup_manifest=True,
        expect_quarantine=False,
    )


def create_backup(state_root, backup_root, *, name=None, now_utc=None, task_mailbox=None):
    """Create one coherent offline snapshot in backup_root using demo_cloud_storage.backup_state."""
    backup_root = storage._root(backup_root)
    backup_name = name or generate_backup_name(backup_root, now_utc=now_utc)
    if parse_backup_timestamp(backup_name) is None:
        raise SafetyBlocked(f'backup name must match {BACKUP_PREFIX}YYYYMMDDTHHMMSSZ: {backup_name}')
    destination = backup_root / backup_name
    return storage.backup_state(state_root, destination, task_mailbox=task_mailbox)


def _compare_snapshot_invariants(source_report, restored_report):
    checks = {}
    checks['databases_match'] = source_report['databases'] == restored_report['databases']
    checks['integrity_all_ok'] = bool(restored_report['integrity_all_ok'])
    checks['binding_match'] = source_report['binding'] == restored_report['binding']
    checks['orders_match'] = (
        source_report['protocol']['orders_count'] == restored_report['protocol']['orders_count']
        and source_report['protocol']['events_count'] == restored_report['protocol']['events_count']
        and source_report['protocol']['receipts_count'] == restored_report['protocol']['receipts_count']
        and source_report['protocol']['unknown_orders_count'] == restored_report['protocol']['unknown_orders_count']
    )
    checks['funding_match'] = (
        source_report['protocol']['funding_events_count'] == restored_report['protocol']['funding_events_count']
        and source_report['protocol']['funding_coverage'] == restored_report['protocol']['funding_coverage']
        and bool(restored_report['protocol']['funding_continuity_ok'])
    )
    checks['control_match'] = (
        source_report['control']['durable_control_state'] == restored_report['control']['durable_control_state']
        and source_report['control']['grid_control_desired'] == restored_report['control']['grid_control_desired']
        and source_report['control']['grid_mode'] == restored_report['control']['grid_mode']
    )
    checks['budget_match'] = (
        source_report['runtime']['continuous_authorization_present']
        == restored_report['runtime']['continuous_authorization_present']
        and source_report['runtime']['authorization_state'] == restored_report['runtime']['authorization_state']
        and source_report['runtime']['campaigns_count'] == restored_report['runtime']['campaigns_count']
        and source_report['runtime']['dispatches_count'] == restored_report['runtime']['dispatches_count']
        and source_report['runtime']['consumed_posts'] == restored_report['runtime']['consumed_posts']
        and source_report['runtime']['consumed_submitted_notional']
        == restored_report['runtime']['consumed_submitted_notional']
        and source_report['runtime']['remaining_posts'] == restored_report['runtime']['remaining_posts']
        and source_report['runtime']['remaining_notional'] == restored_report['runtime']['remaining_notional']
    )
    checks['controller_match'] = source_report['controller'] == restored_report['controller']
    checks['task_mailbox_match'] = source_report['task_mailbox'] == restored_report['task_mailbox']
    checks['secret_files_excluded'] = bool(restored_report['secret_files_excluded'])
    return checks


def _verify_restore_quarantine(backup_dir, restored_dir):
    """Prove restored snapshots stay quarantined and cannot bypass C05 handoff to become Execute."""
    quarantine_file = restored_dir / storage._QUARANTINE
    quarantine_payload = json.loads(quarantine_file.read_text(encoding='utf-8'))
    if quarantine_payload.get('reason') != 'restored-state-requires-GET-only-recovery':
        raise SafetyBlocked('unexpected restore quarantine reason')

    execute_lock_blocked = False
    execute_lock_error = None
    try:
        with operator_lock(restored_dir):
            pass
    except SafetyBlocked as exc:
        execute_lock_blocked = True
        execute_lock_error = str(exc)
    if not execute_lock_blocked:
        raise SafetyBlocked('restore quarantine failed: ordinary operator_lock was not refused')

    get_only_lock_allowed = False
    with operator_lock(restored_dir, allow_quarantined=True):
        get_only_lock_allowed = True

    c05_payload_blocks_restored = False
    c05_payload_blocks_backup = False
    for label, target in (('restored', restored_dir), ('backup', backup_dir)):
        try:
            handoff._payload(target)
        except SafetyBlocked:
            if label == 'restored':
                c05_payload_blocks_restored = True
            else:
                c05_payload_blocks_backup = True
    if not (c05_payload_blocks_restored and c05_payload_blocks_backup):
        raise SafetyBlocked('restore quarantine failed: C05 handoff payload accepted backup/restored directory')

    manifest_present = (restored_dir / storage._BACKUP_MANIFEST).is_file()
    if not manifest_present:
        raise SafetyBlocked('restore quarantine failed: backup-manifest.json missing in restored directory')

    return dict(
        quarantine_file_present=True,
        quarantine_reason=quarantine_payload['reason'],
        backup_manifest_present_in_restored=manifest_present,
        execute_lock_blocked=execute_lock_blocked,
        execute_lock_error=execute_lock_error,
        get_only_lock_allowed=get_only_lock_allowed,
        c05_handoff_blocks_restored=c05_payload_blocks_restored,
        c05_handoff_blocks_backup=c05_payload_blocks_backup,
    )


def drill_restore(backup_dir, *, work_parent=None, keep_restored=False):
    """Restore a backup into an isolated private directory and verify data + quarantine invariants."""
    backup_dir = storage._root(backup_dir)
    source_report = verify_backup(backup_dir)
    parent = storage._root(work_parent if work_parent is not None else backup_dir.parent)
    drill_base = Path(tempfile.mkdtemp(prefix='.restore-drill-', dir=parent))
    if os.name != 'nt':
        drill_base.chmod(0o700)
    restored_dir = drill_base / 'restored-state'
    try:
        storage.restore_state(backup_dir, restored_dir)
        restored_report = inspect_state_snapshot(
            restored_dir,
            expect_backup_manifest=True,
            expect_quarantine=True,
        )
        checks = _compare_snapshot_invariants(source_report, restored_report)
        if not all(checks.values()):
            failed = [key for key, ok in checks.items() if not ok]
            raise SafetyBlocked(f'isolated restore verification failed checks: {", ".join(failed)}')
        quarantine_checks = _verify_restore_quarantine(backup_dir, restored_dir)
        result = dict(
            drilled_at=utc_now().isoformat(),
            backup_path=str(backup_dir),
            isolated_restore_path=str(restored_dir),
            kept_restored=bool(keep_restored),
            passed=True,
            checks=checks,
            quarantine=quarantine_checks,
            snapshot=dict(
                database_count=restored_report['database_count'],
                databases=restored_report['databases'],
                database_bytes=restored_report['database_bytes'],
                total_bytes=restored_report['total_bytes'],
                binding=restored_report['binding'],
                runtime=restored_report['runtime'],
                control=restored_report['control'],
                protocol=restored_report['protocol'],
                controller=restored_report['controller'],
                task_mailbox=restored_report['task_mailbox'],
            ),
        )
        return result
    finally:
        if not keep_restored and drill_base.exists():
            shutil.rmtree(drill_base)


def prune_backups(backup_root, *, retention_days=DEFAULT_RETENTION_DAYS,
                  min_valid_copies=DEFAULT_MIN_VALID_COPIES, now_utc=None):
    """Remove expired backups while strictly preserving at least min_valid_copies verified snapshots."""
    if type(retention_days) is not int or retention_days < 1:
        raise ValueError('retention_days must be an integer >= 1')
    if type(min_valid_copies) is not int or min_valid_copies < 1:
        raise ValueError('min_valid_copies must be an integer >= 1')

    backup_root = storage._root(backup_root)
    now_dt = (now_utc or utc_now()).astimezone(timezone.utc)
    cutoff = now_dt - timedelta(days=retention_days)

    entries = []
    for child in sorted(backup_root.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.is_symlink():
            continue
        created_at = parse_backup_timestamp(child.name)
        if created_at is None:
            continue
        try:
            report = verify_backup(child)
            entries.append(dict(
                name=child.name,
                path=child,
                created_at=created_at,
                valid=True,
                bytes=report['total_bytes'],
                error=None,
            ))
        except Exception as exc:
            entries.append(dict(
                name=child.name,
                path=child,
                created_at=created_at,
                valid=False,
                bytes=0,
                error=classify_backup_error(exc),
            ))

    # Sort newest first.
    entries.sort(key=lambda item: (item['created_at'], item['name']), reverse=True)
    valid_entries = [item for item in entries if item['valid']]
    invalid_entries = [item for item in entries if not item['valid']]

    protected_names = {item['name'] for item in valid_entries[:min_valid_copies]}
    pruned = []
    kept_valid = []
    kept_invalid = []
    remaining_valid = len(valid_entries)

    for item in valid_entries:
        expired = item['created_at'] < cutoff
        if expired and item['name'] not in protected_names and remaining_valid > min_valid_copies:
            shutil.rmtree(item['path'])
            remaining_valid -= 1
            pruned.append(dict(
                name=item['name'],
                created_at=item['created_at'].isoformat(),
                valid=True,
                reason='expired',
            ))
        else:
            kept_valid.append(dict(
                name=item['name'],
                created_at=item['created_at'].isoformat(),
                bytes=item['bytes'],
                protected_min_copy=(item['name'] in protected_names),
            ))

    # Only prune expired invalid directories if we still hold enough verified valid backups.
    for item in invalid_entries:
        if item['created_at'] < cutoff and remaining_valid >= min_valid_copies:
            shutil.rmtree(item['path'])
            pruned.append(dict(
                name=item['name'],
                created_at=item['created_at'].isoformat(),
                valid=False,
                reason='expired_invalid',
                error_category=item['error'],
            ))
        else:
            kept_invalid.append(dict(
                name=item['name'],
                created_at=item['created_at'].isoformat(),
                error_category=item['error'],
            ))

    return dict(
        evaluated_at=now_dt.isoformat(),
        retention_days=retention_days,
        min_valid_copies=min_valid_copies,
        pruned_count=len(pruned),
        pruned=pruned,
        kept_valid_count=len(kept_valid),
        kept_valid=kept_valid,
        kept_invalid_count=len(kept_invalid),
        kept_invalid=kept_invalid,
        total_kept_bytes=sum(item['bytes'] for item in kept_valid),
    )


def _append_history_line(path, record):
    path = Path(os.path.abspath(path))
    storage._root(path.parent)
    storage._no_symlinks(path)
    if path.exists():
        storage._private(path)
    line = (json.dumps(record, allow_nan=False, sort_keys=True, separators=(',', ':'), default=str) + '\n').encode('utf-8')
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, 'ab') as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        storage._sync_directory(path.parent)


def record_backup_metadata(backup_root, status_payload):
    """Persist sanitized latest backup status and append one audit history line."""
    backup_root = storage._root(backup_root)
    storage.atomic_json(backup_root / STATUS_FILENAME, status_payload)
    window = status_payload.get('maintenance_window') or {}
    history_entry = dict(
        started_at=status_payload.get('started_at'),
        completed_at=status_payload.get('completed_at'),
        status=status_payload.get('status'),
        error_category=status_payload.get('error_category'),
        backup_name=status_payload.get('backup_name'),
        database_count=status_payload.get('database_count'),
        backup_bytes=status_payload.get('backup_bytes'),
        verified=status_payload.get('verified'),
        downtime_seconds=window.get('downtime_seconds'),
        interrupted_unit=window.get('interrupted_unit'),
        stopped_at=window.get('stopped_at'),
        restarted_at=window.get('restarted_at'),
        pre_pid=(window.get('pre_health') or {}).get('pid'),
        post_pid=(window.get('post_health') or {}).get('pid'),
        restart_succeeded=window.get('restart_succeeded'),
        reconcile_verified=window.get('reconcile_verified'),
        kept_valid_count=(status_payload.get('retention') or {}).get('kept_valid_count'),
        pruned_count=(status_payload.get('retention') or {}).get('pruned_count'),
    )
    _append_history_line(backup_root / HISTORY_FILENAME, history_entry)


def _extract_health_summary(raw_health):
    if not isinstance(raw_health, dict):
        return None
    global_budget = raw_health.get('global_budget') or {}
    consumed = global_budget.get('consumed') or {}
    grid = raw_health.get('grid') or {}
    systemd = raw_health.get('systemd') or {}
    open_orders = raw_health.get('open_orders')
    positions = raw_health.get('positions')
    return dict(
        mode=raw_health.get('mode'),
        status=raw_health.get('status'),
        desired=raw_health.get('desired'),
        pid=raw_health.get('pid'),
        alive=raw_health.get('alive'),
        ready=raw_health.get('ready'),
        execute_healthy=raw_health.get('execute_healthy'),
        get_only_healthy=raw_health.get('get_only_healthy'),
        account_recovered=raw_health.get('account_recovered'),
        market_fresh=raw_health.get('market_fresh'),
        unknown_count=raw_health.get('unknown_count'),
        open_orders_count=len(open_orders) if isinstance(open_orders, list) else None,
        positions_count=len(positions) if isinstance(positions, list) else None,
        deployment_id=grid.get('deployment_id'),
        grid_state=grid.get('state'),
        consumed_posts=consumed.get('posts'),
        consumed_submitted_notional=consumed.get('submitted_notional'),
        remaining_posts=global_budget.get('remaining_posts'),
        remaining_notional=global_budget.get('remaining_notional'),
        systemd_active_state=systemd.get('active_state'),
        systemd_sub_state=systemd.get('sub_state'),
        systemd_main_pid=systemd.get('main_pid'),
        systemd_n_restarts=systemd.get('n_restarts'),
    )


def _reconcile_matches(pre_health, post_health, *, unit, require_new_pid):
    if not isinstance(post_health, dict):
        return False, 'POST_HEALTH_UNAVAILABLE'
    if post_health.get('systemd_active_state') not in (None, 'active'):
        return False, 'SERVICE_NOT_ACTIVE'
    if post_health.get('systemd_sub_state') not in (None, 'running'):
        return False, 'SERVICE_NOT_RUNNING'
    if (require_new_pid and pre_health and pre_health.get('pid') is not None
            and post_health.get('pid') == pre_health.get('pid')):
        return False, 'WAITING_FOR_NEW_HEARTBEAT'
    if post_health.get('unknown_count') not in (0, None):
        return False, 'ORDER_UNKNOWN'
    if pre_health and pre_health.get('desired') == 'PAUSED':
        if post_health.get('desired') != 'PAUSED':
            return False, 'CONTROL_STATE_DRIFT'
        if not post_health.get('account_recovered'):
            return False, 'WAITING_FOR_RECONCILIATION'
    elif unit == 'factorlab-demo-execute.service':
        budget_paused = bool(pre_health and pre_health.get('status') == 'PAUSED'
            and (pre_health.get('remaining_posts') == 0
                 or pre_health.get('remaining_notional') is not None
                 and Decimal(str(pre_health['remaining_notional'])) == 0)
            and post_health.get('status') == 'PAUSED')
        if not ((post_health.get('execute_healthy') or budget_paused)
                and post_health.get('account_recovered')
                and post_health.get('market_fresh') and post_health.get('unknown_count') == 0):
            return False, 'WAITING_FOR_EXECUTE_READY'
    elif not post_health.get('ready'):
        return False, 'WAITING_FOR_READY'

    if pre_health:
        pre_posts = pre_health.get('consumed_posts')
        post_posts = post_health.get('consumed_posts')
        if pre_posts is not None and (post_posts is None or post_posts < pre_posts):
            return False, 'BUDGET_POSTS_REGRESSED'
        pre_notional = pre_health.get('consumed_submitted_notional')
        post_notional = post_health.get('consumed_submitted_notional')
        if pre_notional is not None:
            try:
                if post_notional is None or Decimal(str(post_notional)) < Decimal(str(pre_notional)):
                    return False, 'BUDGET_NOTIONAL_REGRESSED'
            except InvalidOperation:
                return False, 'BUDGET_NOTIONAL_INVALID'
        if pre_health.get('desired') is not None and post_health.get('desired') != pre_health.get('desired'):
            return False, 'CONTROL_STATE_DRIFT'
    return True, None


class SystemdRunner:
    """Thin OS command wrapper for systemctl and unprivileged service-user execution."""

    def __init__(self, *, service_user=None, python_executable=None):
        self.service_user = service_user
        self.python_executable = python_executable or sys.executable

    def is_unit_active(self, unit):
        result = subprocess.run(
            ['systemctl', 'show', '--property=ActiveState,Job', unit],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        properties = dict(line.split('=', 1) for line in result.stdout.splitlines())
        # A timed-out systemctl client does not cancel the manager's stop job.
        return properties['ActiveState'] == 'active' and not properties['Job']

    def stop_unit(self, unit):
        subprocess.run(
            ['systemctl', 'stop', unit],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )

    def start_unit(self, unit):
        subprocess.run(
            ['systemctl', 'start', unit],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )

    def run_python_module(self, module, args, *, state_root=None, timeout=300, stdin_text=None):
        cmd = [self.python_executable, '-m', module, *args]
        env = dict(os.environ)
        if state_root is not None:
            env['FACTORLAB_DEMO_STATE_ROOT'] = str(state_root)
        identity = {}
        if os.name != 'nt' and self.service_user:
            import pwd
            account = pwd.getpwnam(self.service_user)
            if account.pw_uid != os.getuid():
                identity = dict(user=account.pw_uid, group=account.pw_gid,
                                extra_groups=os.getgrouplist(account.pw_name, account.pw_gid))
                env.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name)
        # The timed child is the Python worker itself, so no privilege wrapper
        # can leave the SQLite backup worker holding the session lock.
        return subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
            **identity,
        )

    def read_health(self, state_root, *, unit=None):
        args = ['health', '--state-root', str(state_root)]
        if unit:
            args.extend(['--unit', unit])
        proc = self.run_python_module(
            'factorlab.integrations.binance_futures_trading.demo_cloud_runtime',
            args,
            state_root=state_root,
            timeout=30,
        )
        if not proc.stdout.strip():
            return None
        try:
            return json.loads(proc.stdout)
        except ValueError:
            return None


def _preflight_backup_space(state_root, backup_root, *, min_free_bytes=MIN_FREE_BYTES, task_mailbox=None):
    state_root = Path(state_root)
    backup_root = Path(backup_root)
    if not state_root.is_dir():
        raise SafetyBlocked(f'state root does not exist: {state_root}')
    if not backup_root.is_dir():
        raise SafetyBlocked(f'backup root does not exist: {backup_root}')
    state_bytes = 0
    for directory, _, names in os.walk(state_root, followlinks=False):
        for name in names:
            path = Path(directory) / name
            try:
                state_bytes += path.lstat().st_size
            except FileNotFoundError:
                continue
    if task_mailbox is not None:
        mailbox = Path(os.path.abspath(task_mailbox))
        storage._no_symlinks(mailbox)
        state_bytes += mailbox.stat().st_size
    free_bytes = shutil.disk_usage(backup_root).free
    if free_bytes < state_bytes + min_free_bytes:
        raise SafetyBlocked('insufficient backup destination space')
    return state_bytes, free_bytes


def _control_blocks_restart(state_root, health=None):
    blocked = {'STOPPED', 'HALTED'}
    if health and (health['status'] in blocked or health['desired'] in blocked):
        return True
    runtime = _read_runtime_summary(state_root / 'runtime.sqlite3')
    control = _read_grid_control_summary(state_root, runtime['durable_control_state'])
    return control['durable_control_state'] in blocked or control['grid_control_desired'] in blocked


def _restore_execute(runner, state_root, window, *, reconcile_timeout,
                         now_fn=utc_now, sleep_fn=time.sleep, monotonic_fn=time.monotonic,
                         stopped_mono=None):
    unit = window['interrupted_unit']
    window['restart_succeeded'] = False
    window['reconcile_verified'] = False
    try:
        if _control_blocks_restart(state_root):
            window['reconcile_reason'] = 'CONTROL_STATE_BLOCKED'
            return 'CONTROL_STATE_BLOCKED'
        needs_start = not runner.is_unit_active(unit)
        if needs_start:
            runner.start_unit(unit)
        window['restart_succeeded'] = True
        restarted_at = now_fn()
        window['restarted_at'] = restarted_at.isoformat()
        if stopped_mono is not None:
            elapsed = monotonic_fn() - stopped_mono
        else:
            elapsed = (restarted_at - datetime.fromisoformat(window['stopped_at'])).total_seconds()
        window['downtime_seconds'] = round(max(0.0, elapsed), 3)
    except Exception:
        window['reconcile_reason'] = 'SERVICE_RESTART_FAILED'
        return 'SERVICE_RESTART_FAILED'

    deadline = monotonic_fn() + reconcile_timeout
    try:
        while True:
            post_health = _extract_health_summary(runner.read_health(state_root, unit=unit))
            reconciled, reason = _reconcile_matches(
                window['pre_health'], post_health, unit=unit, require_new_pid=needs_start,
            )
            window['post_health'] = post_health
            window['reconcile_verified'] = reconciled
            window['reconcile_reason'] = reason
            if reconciled:
                return None
            remaining = deadline - monotonic_fn()
            if remaining <= 0:
                return 'RECONCILE_TIMEOUT'
            sleep_fn(min(2.0, remaining))
    except Exception:
        window['reconcile_reason'] = 'POST_HEALTH_UNAVAILABLE'
        return 'RECONCILE_FAILED'


def _restore_maintenance(runner, state_root, window, **kwargs):
    error = None
    if window['interrupted_unit'] is not None:
        error = _restore_execute(runner, state_root, window, **kwargs)
    try:
        for unit in reversed(window.get('auxiliary_units', [])):
            if not runner.is_unit_active(unit):
                runner.start_unit(unit)
    except Exception:
        return 'SERVICE_RESTART_FAILED'
    return error


def _publish_status(runner, backup_root, payload):
    runner.run_python_module(
        'factorlab.integrations.binance_futures_trading.demo_cloud_backup',
        ['record-status', '--backup-root', str(backup_root), '--payload-stdin'],
        timeout=30,
        stdin_text=json.dumps(payload, default=str),
    ).check_returncode()


def _checkpoint_recovery(path, payload, pending):
    if path is not None:
        storage.atomic_json(path, dict(status=payload, recovery_pending=pending))


def recover_scheduled_backup(
    state_root, backup_root, recovery_file, *, runner=None, service_user='factorlab-demo',
    reconcile_timeout=DEFAULT_RECONCILE_TIMEOUT, termination=None,
):
    """Complete recovery and publish the result after systemd stops the backup cgroup."""
    try:
        checkpoint = json.loads(recovery_file.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    payload = checkpoint['status']
    window = payload['maintenance_window']
    runner = runner or SystemdRunner(service_user=service_user)
    error = payload['error_category']
    pending = checkpoint['recovery_pending']
    if pending:
        recovery_error = _restore_maintenance(runner, state_root, window, reconcile_timeout=reconcile_timeout)
        error = recovery_error or error
    if pending or payload['completed_at'] is None or (termination and termination['SERVICE_RESULT'] != 'success'):
        payload.update(status='FAILED', error_category=error or 'BACKUP_INTERRUPTED',
                       systemd_termination=termination)
    if pending or payload['completed_at'] is None:
        payload['completed_at'] = utc_now().isoformat()
    _publish_status(runner, backup_root, payload)
    recovery_file.unlink()
    return payload


def run_scheduled_backup(
    state_root,
    backup_root,
    *,
    units=DEFAULT_UNITS,
    task_mailbox=None,
    service_user='factorlab-demo',
    retention_days=DEFAULT_RETENTION_DAYS,
    min_valid_copies=DEFAULT_MIN_VALID_COPIES,
    min_free_bytes=MIN_FREE_BYTES,
    reconcile_timeout=DEFAULT_RECONCILE_TIMEOUT,
    drill=False,
    skip_service_control=False,
    recovery_file=None,
    runner=None,
    now_fn=utc_now,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
):
    """Run one controlled maintenance-window backup, restart service first, then verify & prune."""
    state_root = Path(os.path.abspath(state_root))
    backup_root = Path(os.path.abspath(backup_root))
    runner = runner or SystemdRunner(service_user=service_user)

    started_dt = now_fn()
    status_payload = dict(
        version=1,
        started_at=started_dt.isoformat(),
        completed_at=None,
        status='FAILED',
        error_category=None,
        backup_name=None,
        backup_path=None,
        database_count=None,
        backup_bytes=None,
        verified=False,
        verification=None,
        drill_restore=None,
        maintenance_window=dict(
            service_control_enabled=not skip_service_control,
            interrupted_unit=None,
            auxiliary_units=[],
            stopped_at=None,
            restarted_at=None,
            downtime_seconds=0.0,
            restart_succeeded=None,
            reconcile_verified=None,
            reconcile_reason=None,
            pre_health=None,
            post_health=None,
        ),
        retention=dict(
            retention_days=retention_days,
            min_valid_copies=min_valid_copies,
            pruned_count=0,
            kept_valid_count=None,
            skipped_reason='backup_not_verified',
        ),
        offsite_enabled=False,
    )

    active_unit = None
    window = status_payload['maintenance_window']
    backup_succeeded = False
    recovery_pending = False
    operation_error = None

    _checkpoint_recovery(recovery_file, status_payload, recovery_pending)
    try:
        # 1. Inspect currently active unit and capture pre-backup health baseline without stopping anything.
        if not skip_service_control:
            for unit in units:
                if runner.is_unit_active(unit):
                    active_unit = unit
                    break

        raw_pre = runner.read_health(state_root, unit=active_unit)
        pre_health = _extract_health_summary(raw_pre)
        window['pre_health'] = pre_health
        if active_unit is not None and _control_blocks_restart(state_root, pre_health):
            operation_error = 'CONTROL_STATE_BLOCKED'
            raise SafetyBlocked('active unit has STOPPED or HALTED control state')

        # 2. Check free disk space BEFORE stopping Execute to avoid pointless downtime.
        _preflight_backup_space(state_root, backup_root, min_free_bytes=min_free_bytes,
                                task_mailbox=task_mailbox)

        backup_name = generate_backup_name(backup_root, now_utc=started_dt)
        backup_dest = backup_root / backup_name
        status_payload['backup_name'] = backup_name
        status_payload['backup_path'] = str(backup_dest)

        # Persist recovery intent before the stop request reaches systemd.
        stopped_mono = None
        try:
            has_task_mailbox = task_mailbox is not None or (state_root / storage._TASK_MAILBOX).is_file()
            if has_task_mailbox and not skip_service_control:
                for unit in MAILBOX_WRITER_UNITS:
                    if runner.is_unit_active(unit):
                        window['auxiliary_units'].append(unit)
                        _checkpoint_recovery(recovery_file, status_payload, True)
                        recovery_pending = True
                        operation_error = 'SERVICE_STOP_FAILED'
                        runner.stop_unit(unit)
                        operation_error = None
            if active_unit is not None:
                window['interrupted_unit'] = active_unit
                window['stopped_at'] = now_fn().isoformat()
                stopped_mono = monotonic_fn()
                _checkpoint_recovery(recovery_file, status_payload, True)
                recovery_pending = True
                operation_error = 'SERVICE_STOP_FAILED'
                runner.stop_unit(active_unit)
                operation_error = None

            proc = runner.run_python_module(
                'factorlab.integrations.binance_futures_trading.demo_cloud_runtime',
                ['backup', '--state-root', str(state_root), '--destination', str(backup_dest)]
                + (['--task-mailbox', str(task_mailbox)] if task_mailbox is not None else []),
                state_root=state_root,
                timeout=300,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or '').strip()
                raise SafetyBlocked(f'backup command failed (rc={proc.returncode}): {detail}')
            backup_succeeded = True
        except subprocess.TimeoutExpired:
            operation_error = operation_error or 'BACKUP_TIMEOUT'
            raise
        finally:
            if recovery_pending:
                recovery_error = _restore_maintenance(
                    runner, state_root, window, reconcile_timeout=reconcile_timeout,
                    now_fn=now_fn, sleep_fn=sleep_fn, monotonic_fn=monotonic_fn,
                    stopped_mono=stopped_mono,
                )
                recovery_pending = recovery_error not in (None, 'CONTROL_STATE_BLOCKED')
                if recovery_error is not None:
                    status_payload['operation_error_category'] = operation_error
                    operation_error = recovery_error
                _checkpoint_recovery(recovery_file, status_payload, recovery_pending)

        if operation_error is not None:
            raise SafetyBlocked('maintenance recovery failed')

        # 5. Deep-verify the newly created backup snapshot while service is already back online.
        verify_proc = runner.run_python_module(
            'factorlab.integrations.binance_futures_trading.demo_cloud_backup',
            ['verify', '--backup', str(backup_dest)],
            timeout=120,
        )
        if verify_proc.returncode != 0:
            detail = (verify_proc.stderr or verify_proc.stdout or '').strip()
            raise SafetyBlocked(f'backup verification failed (rc={verify_proc.returncode}): {detail}')
        verification = json.loads(verify_proc.stdout)
        status_payload['verified'] = bool(verification.get('valid'))
        status_payload['verification'] = verification
        status_payload['database_count'] = verification.get('database_count')
        status_payload['backup_bytes'] = verification.get('total_bytes')

        if drill:
            drill_proc = runner.run_python_module(
                'factorlab.integrations.binance_futures_trading.demo_cloud_backup',
                ['drill-restore', '--backup', str(backup_dest)],
                timeout=180,
            )
            if drill_proc.returncode != 0:
                detail = (drill_proc.stderr or drill_proc.stdout or '').strip()
                raise SafetyBlocked(f'isolated restore drill failed (rc={drill_proc.returncode}): {detail}')
            status_payload['drill_restore'] = json.loads(drill_proc.stdout)

        # 6. Prune expired backups ONLY after the new backup has passed verification.
        if status_payload['verified']:
            prune_proc = runner.run_python_module(
                'factorlab.integrations.binance_futures_trading.demo_cloud_backup',
                [
                    'prune',
                    '--backup-root', str(backup_root),
                    '--retention-days', str(retention_days),
                    '--min-valid-copies', str(min_valid_copies),
                ],
                timeout=180,
            )
            if prune_proc.returncode != 0:
                detail = (prune_proc.stderr or prune_proc.stdout or '').strip()
                raise SafetyBlocked(f'backup retention prune failed (rc={prune_proc.returncode}): {detail}')
            retention_result = json.loads(prune_proc.stdout)
            retention_result['skipped_reason'] = None
            status_payload['retention'] = retention_result

        status_payload['status'] = 'SUCCEEDED'
    except Exception as exc:
        status_payload['error_category'] = operation_error or classify_backup_error(exc)
        if not backup_succeeded:
            status_payload['retention']['skipped_reason'] = 'backup_failed'

    status_payload['completed_at'] = now_fn().isoformat()
    if recovery_file is None:
        _publish_status(runner, backup_root, status_payload)
    else:
        _checkpoint_recovery(recovery_file, status_payload, recovery_pending)

    return status_payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='action', required=True)

    p_verify = subparsers.add_parser('verify', help='Deep read-only verification of a backup snapshot')
    p_verify.add_argument('--backup', type=Path, required=True)

    p_drill = subparsers.add_parser('drill-restore', help='Isolated restore and quarantine drill')
    p_drill.add_argument('--backup', type=Path, required=True)
    p_drill.add_argument('--work-parent', type=Path)
    p_drill.add_argument('--keep-restored', action='store_true')

    p_prune = subparsers.add_parser('prune', help='Prune expired backups while preserving >= 1 valid copy')
    p_prune.add_argument('--backup-root', type=Path, required=True)
    p_prune.add_argument('--retention-days', type=int, default=DEFAULT_RETENTION_DAYS)
    p_prune.add_argument('--min-valid-copies', type=int, default=DEFAULT_MIN_VALID_COPIES)

    p_record = subparsers.add_parser('record-status', help='Record sanitized backup status and history')
    p_record.add_argument('--backup-root', type=Path, required=True)
    p_record_group = p_record.add_mutually_exclusive_group(required=True)
    p_record_group.add_argument('--payload-json')
    p_record_group.add_argument('--payload-stdin', action='store_true')

    p_sched = subparsers.add_parser('run-scheduled', help='Run controlled maintenance-window backup')
    p_sched.add_argument('--state-root', type=Path, required=True)
    p_sched.add_argument('--backup-root', type=Path, required=True)
    p_sched.add_argument('--task-mailbox', type=Path)
    p_sched.add_argument('--service-user', default='factorlab-demo')
    p_sched.add_argument('--units', nargs='+', default=list(DEFAULT_UNITS))
    p_sched.add_argument('--retention-days', type=int, default=DEFAULT_RETENTION_DAYS)
    p_sched.add_argument('--min-valid-copies', type=int, default=DEFAULT_MIN_VALID_COPIES)
    p_sched.add_argument('--min-free-bytes', type=int, default=MIN_FREE_BYTES)
    p_sched.add_argument('--reconcile-timeout', type=float, default=DEFAULT_RECONCILE_TIMEOUT)
    p_sched.add_argument('--drill-restore', action='store_true')
    p_sched.add_argument('--skip-service-control', action='store_true')
    p_sched.add_argument('--recovery-directory', type=Path)

    p_recover = subparsers.add_parser('recover-scheduled', help='Recover a backup interrupted by systemd')
    p_recover.add_argument('--state-root', type=Path, required=True)
    p_recover.add_argument('--backup-root', type=Path, required=True)
    p_recover.add_argument('--recovery-directory', type=Path, required=True)
    p_recover.add_argument('--service-user', default='factorlab-demo')
    p_recover.add_argument('--reconcile-timeout', type=float, default=DEFAULT_RECONCILE_TIMEOUT)

    args = parser.parse_args(argv)
    if os.name != 'nt':
        os.umask(0o077)

    try:
        if args.action == 'verify':
            result = verify_backup(args.backup)
            print(json.dumps(result, default=str), flush=True)
            return 0
        if args.action == 'drill-restore':
            result = drill_restore(args.backup, work_parent=args.work_parent, keep_restored=args.keep_restored)
            print(json.dumps(result, default=str), flush=True)
            return 0
        if args.action == 'prune':
            result = prune_backups(
                args.backup_root,
                retention_days=args.retention_days,
                min_valid_copies=args.min_valid_copies,
            )
            print(json.dumps(result, default=str), flush=True)
            return 0
        if args.action == 'record-status':
            raw_payload = sys.stdin.read() if args.payload_stdin else args.payload_json
            record_backup_metadata(args.backup_root, json.loads(raw_payload))
            return 0
        if args.action == 'run-scheduled':
            result = run_scheduled_backup(
                args.state_root,
                args.backup_root,
                units=tuple(args.units),
                task_mailbox=args.task_mailbox,
                service_user=args.service_user,
                retention_days=args.retention_days,
                min_valid_copies=args.min_valid_copies,
                min_free_bytes=args.min_free_bytes,
                reconcile_timeout=args.reconcile_timeout,
                drill=args.drill_restore,
                skip_service_control=args.skip_service_control,
                recovery_file=(args.recovery_directory / os.environ['INVOCATION_ID']
                               if args.recovery_directory is not None else None),
            )
            print(json.dumps(result, default=str), flush=True)
            return 0 if result['status'] == 'SUCCEEDED' else 1
        if args.action == 'recover-scheduled':
            result = recover_scheduled_backup(
                args.state_root, args.backup_root, args.recovery_directory / os.environ['INVOCATION_ID'],
                service_user=args.service_user, reconcile_timeout=args.reconcile_timeout,
                termination={key: os.environ.get(key) for key in ('SERVICE_RESULT', 'EXIT_CODE', 'EXIT_STATUS')},
            )
            if result is not None:
                print(json.dumps(result, default=str), flush=True)
                return 1 if result['maintenance_window']['reconcile_verified'] is False else 0
    except (SafetyBlocked, ValueError, KeyError, TypeError, OSError, sqlite3.DatabaseError,
            subprocess.SubprocessError) as exc:
        print(json.dumps(dict(
            event='cloud_backup_error',
            error_category=classify_backup_error(exc),
        )), flush=True)
        return CONFIG_ERROR
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
