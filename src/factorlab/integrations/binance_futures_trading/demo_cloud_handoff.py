"""Move a stopped Windows Demo Execute ledger to one prepared Linux state path."""

from contextlib import closing
from datetime import datetime, timezone
from hashlib import file_digest
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from uuid import uuid4

from . import demo_cloud_storage as storage
from .demo_cli import open_session, recover
from .demo_credentials import DemoCredentialStore
from .demo_file_credentials import DemoFileCredentialStore
from .demo_runtime import DemoRuntime, journal_id, operator_lock, private_binding
from .demo_strategy_supervisor import StrategyControl
from .observability import RequestCounts
from .protocol import SafetyBlocked


_RELEASE = 'execute-handoff.json'


def _target(machine_id, state_root):
    path = PurePosixPath(state_root)
    if (re.fullmatch(r'[0-9a-f]{32}', machine_id) is None or not path.is_absolute()
            or not path.name or '..' in path.parts or str(path) != state_root):
        raise SafetyBlocked('handoff requires a Linux machine ID and an absolute canonical state path')
    return dict(machine_id=machine_id, state_root=state_root)


def _payload(package):
    if any((package / name).exists() for name in (storage._QUARANTINE, storage._BACKUP_MANIFEST)):
        raise SafetyBlocked('ordinary backup and restore roots cannot transfer Execute')
    payload = storage._root(package / 'databases')
    databases, files = storage._inventory(payload)
    if set(files) != set(databases):
        raise SafetyBlocked('unexpected files in the handoff database inventory')
    binding_path = package / 'binding.json'
    storage._private(binding_path)
    binding = storage._binding(binding_path)
    account_journal = journal_id(binding['account_id'])
    required = {payload / 'runtime.sqlite3', payload / 'grid-supervisor.sqlite3',
                payload / account_journal / 'protocol.sqlite3',
                payload / account_journal / 'controller.sqlite3'}
    if binding['journal_id'] != account_journal or not required.issubset(databases):
        raise SafetyBlocked('handoff is missing the bound account databases')
    return payload, databases, binding_path


def _digests(root, paths):
    result = {}
    for path in paths:
        with path.open('rb') as stream:
            result[path.relative_to(root).as_posix()] = file_digest(stream, 'sha256').hexdigest()
    return result


def export_handoff(root, destination, *, machine_id, state_root):
    """Publish a release only after durably sealing the original under SQLite reservations."""
    target = _target(machine_id, state_root)
    root = storage._root(root)
    destination = Path(os.path.abspath(destination))
    seal_path = root / storage._QUARANTINE
    with operator_lock(root, allow_quarantined=True):
        resuming = seal_path.exists()
        if resuming:
            storage._private(seal_path)
            seal = json.loads(seal_path.read_text(encoding='utf-8'))
            if (seal.get('reason') != storage._HANDOFF_SOURCE
                    or seal['package'] != str(destination) or seal['release']['target'] != target):
                raise SafetyBlocked('source is quarantined or sealed for a different handoff')
        else:
            if (root / storage._BACKUP_MANIFEST).exists():
                raise SafetyBlocked('handoff requires original Execute state')
            destination = storage._destination(root, destination)
            store = DemoCredentialStore()
            binding = storage._validate_binding(private_binding(store))
            with storage._freeze_databases(root, destination) as databases:
                with tempfile.TemporaryDirectory(prefix='.handoff-', dir=destination.parent) as temporary:
                    staging = Path(temporary)
                    payload = storage._root(staging / 'databases', create=True)
                    storage._copy_databases(root, payload, databases)
                    storage.atomic_json(staging / 'binding.json', binding)
                    _, copied, binding_path = _payload(staging)
                    with closing(DemoRuntime(payload)) as runtime:
                        _preflight(payload, runtime, store)
                    release = dict(version=1, id=uuid4().hex, target=target,
                                   files=_digests(staging, [binding_path, *copied]))
                    storage._publish(staging, destination)
                    seal = dict(version=1, reason=storage._HANDOFF_SOURCE, package=str(destination), release=release)
                    storage.atomic_json(seal_path, seal, windows_write_through=True)
        # A crash after sealing can publish only the original frozen package.
        release = seal['release']
        manifest_path = storage._root(destination) / _RELEASE
        if manifest_path.exists():
            storage._private(manifest_path)
            if json.loads(manifest_path.read_text(encoding='utf-8')) != release:
                raise SafetyBlocked('handoff release differs from the sealed source')
            return release['id']
        if resuming:
            _, databases, binding = _payload(destination)
            if _digests(destination, [binding, *databases]) != release['files']:
                raise SafetyBlocked('sealed package changed before release')
        storage.atomic_json(manifest_path, release)
        return release['id']


def _preflight(root, runtime, store):
    from .demo_grid_supervisor import _preflight_local_state

    authorization = runtime.continuous_authorization()
    if authorization is None:
        raise SafetyBlocked('handoff requires existing continuous Demo authorization')
    with closing(StrategyControl(root / 'grid-supervisor.sqlite3')) as control:
        configuration = control.snapshot()['configuration']
    if configuration['mode'] != 'execute':
        raise SafetyBlocked('handoff requires the existing Grid Execute control database')
    _preflight_local_state(root, runtime, store, configuration['settings'],
                           execute=True, new_deployment=False, require_saved_grid=True)
    return authorization, configuration


def _reconcile(root):
    from .demo_cloud_runtime import durable_state

    store = DemoFileCredentialStore(root=root)
    requests = RequestCounts()
    with closing(DemoRuntime(root)) as runtime:
        authorization, configuration = _preflight(root, runtime, store)
        with open_session(root, store, runtime, requests, require_bound_configuration=True) as (session, _):
            account = recover(session)
        return dict(reconciled_at=datetime.now(timezone.utc).isoformat(), desired=durable_state(root, runtime),
                    configuration=configuration, requests=dict(requests), open_orders=len(account['open_orders']),
                    positions=len(account['positions']), funding=account['funding'],
                    consumed=runtime.consumption(authorization), unknown=len(account['unknown']))


def import_handoff(package, destination, *, min_free_bytes):
    """Validate and reconcile privately; make the bound state path usable in one rename."""
    package = storage._root(package)
    destination = storage._destination(package, destination)
    manifest = package / _RELEASE
    storage._private(manifest)
    release = json.loads(manifest.read_text(encoding='utf-8'))
    machine_id = Path('/etc/machine-id').read_text(encoding='ascii').strip()
    if release['version'] != 1 or release['target'] != _target(machine_id, destination.as_posix()):
        raise SafetyBlocked('handoff release is for another Linux host or state path')
    payload, databases, binding = _payload(package)
    expected_bytes = sum(path.stat().st_size for path in [binding, *databases])
    if shutil.disk_usage(destination.parent).free < expected_bytes + min_free_bytes:
        raise SafetyBlocked('insufficient space for the handoff and SQLite reserve')
    with tempfile.TemporaryDirectory(prefix='.handoff-', dir=destination.parent) as temporary:
        staging = Path(temporary)
        for path in [binding, *databases]:
            relative = path.relative_to(payload if path != binding else package)
            target = staging / relative
            storage._root(target.parent, create=True)
            with path.open('rb') as reader, target.open('xb') as writer:
                shutil.copyfileobj(reader, writer)
                os.fchmod(writer.fileno(), 0o600)
                writer.flush()
                os.fsync(writer.fileno())
            with target.open('rb') as stream:
                if file_digest(stream, 'sha256').hexdigest() != release['files'].get(path.relative_to(package).as_posix()):
                    raise SafetyBlocked('handoff file differs from the sealed snapshot')
        if len(release['files']) != 1 + len(databases):
            raise SafetyBlocked('handoff database inventory differs from the release')
        storage.validate_state(staging, min_free_bytes)
        result = _reconcile(staging)
        result.update(id=release['id'], snapshot_verified=True)
        storage.atomic_json(staging / 'execute-handoff-result.json', result)
        for directory, _, _ in os.walk(staging, topdown=False, onerror=storage._walk_error):
            storage._sync_directory(Path(directory))
        storage._publish(staging, destination)
    return result
