"""Private offline maintenance of the Demo journals (never credential storage)."""

from contextlib import ExitStack, closing, contextmanager
import ctypes
from datetime import datetime, timezone
import errno
from hashlib import file_digest
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile

from .protocol import SafetyBlocked


_DB_SUFFIXES = {'.sqlite', '.sqlite3', '.db'}
_SIDECARS = ('-wal', '-shm', '-journal')
_QUARANTINE = 'restore-quarantine.json'
_BACKUP_MANIFEST = 'backup-manifest.json'
_RECOVERY_MANIFEST = 'recovery-manifest.json'
_HANDOFF_SOURCE = 'windows-execute-source-sealed'
_DR_SOURCE = 'disaster-recovery-source-sealed'
_TASK_MAILBOX = Path('external/task-mailbox/tasks.sqlite3')


def _private(path, *, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise SafetyBlocked(f'private storage requires a regular {"directory" if directory else "file"}: {path}')
    if os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise SafetyBlocked(f'storage must be owned by the current user and private: {path}')
    if not directory and info.st_nlink != 1:
        raise SafetyBlocked(f'storage hard links are not allowed: {path}')


def _no_symlinks(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise SafetyBlocked(f'storage symlinks are not allowed: {part}')


def _root(root, *, create=False):
    root = Path(os.path.abspath(root))
    _no_symlinks(root)
    if create and not root.exists():
        missing = []
        parent = root
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
    _private(root, directory=True)
    return root


def _walk_error(error):
    raise error


def _inventory(root):
    databases, files = [], []
    for directory, directories, names in os.walk(root, followlinks=False, onerror=_walk_error):
        for name in directories:
            _private(Path(directory) / name, directory=True)
        for name in names:
            path = Path(directory) / name
            try:
                _private(path)
            except FileNotFoundError:
                if name.endswith(_SIDECARS):
                    continue
                raise
            files.append(path)
            if name == 'session.lock' or name.endswith(_SIDECARS):
                continue
            with path.open('rb') as stream:
                header = stream.read(16)
            if path.suffix in _DB_SUFFIXES or header == b'SQLite format 3\x00':
                databases.append(path)
    return sorted(databases), sorted(files)


def _connect(path, *, writable=False, timeout=0):
    return sqlite3.connect(path.as_uri() + ('?mode=rw' if writable else '?mode=ro'),
                           uri=True, timeout=timeout, isolation_level=None)


def _integrity(path, *, recover=False):
    # The worker lock excludes other sessions, but control commands can still commit.
    with closing(_connect(path, writable=recover, timeout=5 if recover else 0)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise SafetyBlocked(f'SQLite integrity check failed: {path}')


def storage_metrics(root):
    """Return disk capacity and per-database main/WAL/SHM/journal byte counts."""
    root = _root(root)
    return _storage_metrics(root, *_inventory(root))


def _storage_metrics(root, databases, files):
    sizes = {}
    for path in files:
        try:
            sizes[path] = path.stat().st_size
        except FileNotFoundError:
            if not path.name.endswith(_SIDECARS):
                raise
    records = {}
    for path in databases:
        record = {'database_bytes': sizes[path]}
        for suffix in _SIDECARS:
            record[suffix[1:] + '_bytes'] = sizes.get(Path(str(path) + suffix), 0)
        record['total_bytes'] = sum(record.values())
        records[path.relative_to(root).as_posix()] = record
    usage = shutil.disk_usage(root)
    return {'databases': records, 'database_bytes': sum(r['database_bytes'] for r in records.values()),
            'sidecar_bytes': sum(r['total_bytes'] - r['database_bytes'] for r in records.values()),
            'state_bytes': sum(sizes.values()), 'disk_free_bytes': usage.free, 'disk_bytes': usage.total}


def validate_state(root, min_free_bytes, *, recover=False):
    """Check existing state; recover hot journals only under the caller's worker lock.

    Inspection defaults to read-only and never initializes a database.
    """
    if min_free_bytes < 0:
        raise ValueError('minimum free bytes must be nonnegative')
    root = _root(root)
    databases, files = _inventory(root)
    metrics = _storage_metrics(root, databases, files)
    if metrics['disk_free_bytes'] < min_free_bytes:
        raise SafetyBlocked('insufficient free space for Demo storage')
    for path in databases:
        _integrity(path, recover=recover)
    return metrics


def _sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_json(path, payload, *, windows_write_through=False):
    """Durably replace a JSON snapshot with a private same-directory temporary file."""
    path = Path(os.path.abspath(path))
    _root(path.parent)
    _no_symlinks(path)
    if path.exists():
        _private(path)
    encoded = (json.dumps(payload, allow_nan=False, sort_keys=True, separators=(',', ':'), default=str) + '\n').encode('utf-8')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if windows_write_through and os.name == 'nt':
            move = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
            move.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
            move.restype = ctypes.c_int
            if not move(str(temporary), str(path), 0x8):  # MOVEFILE_WRITE_THROUGH
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _binding(path):
    return _validate_binding(json.loads(path.read_text(encoding='utf-8')))


def _validate_binding(value):
    def check(item):
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = key.lower().replace('-', '').replace('_', '')
                if any(word in normalized for word in ('secret', 'password', 'token', 'apikey', 'privatekey')):
                    raise SafetyBlocked('credentials must not be stored in binding.json')
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
    if not isinstance(value, dict):
        raise SafetyBlocked('binding.json must be a JSON object')
    check(value)
    return value


def assert_source_controls_available(root):
    marker = root / _QUARANTINE
    if marker.exists() and json.loads(marker.read_text(encoding='utf-8')).get('reason') in (_HANDOFF_SOURCE, _DR_SOURCE):
        raise SafetyBlocked('Demo Execute source is sealed; send controls to the current owner')


def _publish(staging, destination):
    # POSIX rename can replace an empty directory; use the kernel's no-replace operation.
    if os.name == 'nt':
        os.rename(staging, destination)
    else:
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(staging), -100, os.fsencode(destination), 1):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(destination))
    _sync_directory(destination.parent)


def _destination(source, destination):
    destination = Path(os.path.abspath(destination))
    _no_symlinks(destination)
    _root(destination.parent)
    if destination.exists():
        raise FileExistsError(errno.EEXIST, 'destination already exists', str(destination))
    if source == destination or source in destination.parents:
        raise SafetyBlocked('destination must be outside the source tree')
    return destination


def _copy_databases(root, staging, databases, *, task_mailbox=None):
    for source in databases:
        relative = _TASK_MAILBOX if source == task_mailbox else source.relative_to(root)
        target = staging / relative
        _root(target.parent, create=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        # The writer-freezing connection must not be the backup source: SQLite's
        # backup API waits for a source write transaction to finish.
        with closing(_connect(source)) as reader, closing(sqlite3.connect(target)) as writer:
            reader.backup(writer)
        _integrity(target)
        with target.open('r+b') as stream:
            os.fsync(stream.fileno())
    for directory, _, _ in os.walk(staging, topdown=False, onerror=_walk_error):
        _sync_directory(Path(directory))


def _strategy_files(root):
    from factorlab.strategies.package import load_package

    releases = root / 'strategy-releases'
    if not releases.exists():
        return []
    files = []
    for release in sorted(releases.iterdir()):
        # Package installation publishes an immutable directory by rename.
        if release.name.startswith('.staging-'):
            continue
        manifest = load_package(release)
        if manifest['package_id'] != release.name:
            raise SafetyBlocked('strategy release directory differs from its package identity')
        files.extend(release / name for name in ['manifest.json', *manifest['files']])
    return files


def file_sha256(path):
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def verify_snapshot(root):
    """Verify the complete offsite inventory and SQLite content without opening writers."""
    root = _root(root)
    manifest_path = root / _RECOVERY_MANIFEST
    _private(manifest_path)
    manifest = json.loads(manifest_path.read_bytes())
    databases, files = _inventory(root)
    inventory = set(databases) | set(_strategy_files(root)) | {root / _BACKUP_MANIFEST}
    if (root / 'binding.json').exists():
        _binding(root / 'binding.json')
        inventory.add(root / 'binding.json')
    if (root / _QUARANTINE).exists():
        inventory.add(root / _QUARANTINE)
    if (manifest['version'] != 1 or manifest['market'] not in ('binance', 'alpaca')
            or not databases or set(files) != inventory | {manifest_path}
            or manifest['files'] != {p.relative_to(root).as_posix(): file_sha256(p) for p in inventory}):
        raise SafetyBlocked('offsite snapshot inventory or digest mismatch')
    _verify_database_manifest(root, databases)
    for path in databases:
        _integrity(path)
    return manifest


def _verify_database_manifest(root, databases):
    manifest = json.loads((root / _BACKUP_MANIFEST).read_bytes())
    expected = dict(version=1, databases=[path.relative_to(root).as_posix() for path in databases],
                    binding=(root / 'binding.json').exists())
    if manifest != expected:
        raise SafetyBlocked('incomplete backup inventory')
    return manifest


def _snapshot(root, destination, databases, *, quarantine, task_mailbox=None, metadata=None):
    staging = Path(tempfile.mkdtemp(prefix='.' + destination.name + '-', dir=destination.parent))
    try:
        _copy_databases(root, staging, databases, task_mailbox=task_mailbox)
        for path in _strategy_files(root):
            relative = path.relative_to(root)
            target = staging / relative
            _root(target.parent, create=True)
            with path.open('rb') as reader, target.open('xb') as writer:
                os.chmod(target, 0o600)
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
        binding = root / 'binding.json'
        if binding.exists():
            atomic_json(staging / 'binding.json', _binding(binding))
        if quarantine or ((root / _QUARANTINE).exists() and not (metadata and metadata['final_target'])):
            atomic_json(staging / _QUARANTINE, {'version': 1, 'reason': 'restored-state-requires-GET-only-recovery'})
        atomic_json(staging / _BACKUP_MANIFEST, dict(version=1,
            databases=sorted((_TASK_MAILBOX if path == task_mailbox else path.relative_to(root)).as_posix()
                             for path in databases), binding=binding.exists()))
        if metadata is not None and not quarantine:
            _, files = _inventory(staging)
            atomic_json(staging / _RECOVERY_MANIFEST, dict(metadata, version=1,
                files={path.relative_to(staging).as_posix(): file_sha256(path) for path in files}))
        for directory, _, _ in os.walk(staging, topdown=False, onerror=_walk_error):
            _sync_directory(Path(directory))
        _publish(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


@contextmanager
def _freeze_databases(root, destination, *, task_mailbox=None):
    # The caller owns session.lock. SQLite reservations also exclude concurrent STOP.
    with ExitStack() as stack:
        databases, _ = _inventory(root)
        if task_mailbox is not None:
            _root(task_mailbox.parent)
            _no_symlinks(task_mailbox)
            _private(task_mailbox)
            if task_mailbox == root or root in task_mailbox.parents:
                raise SafetyBlocked('external TaskMailbox must be outside the state root')
            if (root / _TASK_MAILBOX).exists():
                raise SafetyBlocked('external TaskMailbox snapshot path already exists')
            databases.append(task_mailbox)
        required = sum(path.stat().st_size for path in databases)
        if shutil.disk_usage(destination.parent).free < required + 256 * 1024 * 1024:
            raise SafetyBlocked('insufficient backup destination space')
        for path in databases:
            connection = stack.enter_context(closing(_connect(path, writable=True)))
            connection.execute('BEGIN IMMEDIATE')
        for path in databases:
            _integrity(path)
        yield databases


def _frozen_snapshot(root, destination, *, quarantine, task_mailbox=None):
    from .demo_runtime import operator_lock

    root = _root(root)
    destination = _destination(root, destination)
    if task_mailbox is not None:
        task_mailbox = Path(os.path.abspath(task_mailbox))
    with operator_lock(root, allow_quarantined=True), _freeze_databases(
        root, destination, task_mailbox=task_mailbox,
    ) as databases:
        if quarantine:
            _verify_database_manifest(root, databases)
        if not quarantine and os.environ.get('FACTORLAB_DR_REVISION'):
            metadata = recovery_metadata('binance', os.environ['FACTORLAB_DR_REVISION'])
            return _snapshot(root, destination, databases, quarantine=False, task_mailbox=task_mailbox,
                             metadata=metadata)
        return _snapshot(root, destination, databases, quarantine=quarantine, task_mailbox=task_mailbox)


def backup_state(root, dest, *, task_mailbox=None):
    """Publish an offline, coherent SQLite + binding backup; never copy credentials.

    All operators must be stopped. Active SQLite writers or an operator lock cause
    immediate failure. The destination must not exist, including an empty directory.
    An external TaskMailbox is frozen with the journals and stored at
    external/task-mailbox/tasks.sqlite3; restore never writes to its live path.
    """
    return _frozen_snapshot(root, dest, quarantine=False, task_mailbox=task_mailbox)


def restore_state(source, dest):
    """Restore without resetting budgets; persist GET-only recovery quarantine."""
    return _frozen_snapshot(source, dest, quarantine=True)


def recovery_metadata(market, revision, *, final_target=None, configuration_sha256=None):
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise SafetyBlocked('the deployed Git revision must be a full commit ID')
    return dict(market=market, revision=revision, source_machine=machine_id(),
                captured_at=datetime.now(timezone.utc).isoformat(), final_target=final_target,
                configuration_sha256=configuration_sha256)


def machine_id():
    identity = Path('/etc/machine-id').read_text(encoding='ascii').strip()
    if not re.fullmatch('[0-9a-f]{32}', identity):
        raise SafetyBlocked('a unique Linux machine ID is required')
    return identity


def recovery_lock(root, market):
    if market == 'alpaca':
        from factorlab.integrations.alpaca.cloud_runtime import owner_lock
        return owner_lock(root, allow_quarantined=True)
    from .demo_runtime import operator_lock
    return operator_lock(root, allow_quarantined=True)


def recovery_snapshot(root, destination, market, revision, *, task_mailbox=None,
                      final_target=None, configuration_sha256=None):
    """Capture the frozen internal ledger. A final snapshot durably seals its source."""
    root = _root(root)
    destination = _destination(root, destination)
    if task_mailbox is not None:
        task_mailbox = Path(os.path.abspath(task_mailbox))
    if market == 'alpaca' and task_mailbox is not None:
        raise SafetyBlocked('the external TaskMailbox belongs to Binance')
    with recovery_lock(root, market):
        metadata = recovery_metadata(market, revision, final_target=final_target,
                                     configuration_sha256=configuration_sha256)
        marker = root / _QUARANTINE
        if marker.exists():
            _private(marker)
            seal = json.loads(marker.read_bytes())
            if (not final_target or seal['reason'] != _DR_SOURCE or seal['destination'] != str(destination)
                    or any(seal['metadata'][key] != metadata[key] for key in metadata if key != 'captured_at')):
                raise SafetyBlocked('source is quarantined or sealed for another transfer')
            metadata = seal['metadata']
        with _freeze_databases(root, destination, task_mailbox=task_mailbox) as databases:
            if not databases:
                raise SafetyBlocked('the source contains no account databases')
            if final_target and not marker.exists():
                atomic_json(marker, dict(version=1, reason=_DR_SOURCE,
                                         destination=str(destination), metadata=metadata))
            return _snapshot(root, destination, databases, quarantine=False,
                             task_mailbox=task_mailbox, metadata=metadata)


def restore_verified_state(source, dest, manifest):
    """Copy a verified, published snapshot without locking or changing the backup itself."""
    source = _root(source)
    destination = _destination(source, dest)
    captured = json.loads((source / _BACKUP_MANIFEST).read_bytes())
    databases = [source / name for name in captured['databases']]
    if any(name not in manifest['files'] for name in captured['databases']):
        raise SafetyBlocked('database inventory differs from the verified manifest')
    return _snapshot(source, destination, databases, quarantine=True)
