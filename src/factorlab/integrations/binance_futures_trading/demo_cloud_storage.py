"""Private offline maintenance of the Demo journals (never credential storage)."""

from contextlib import ExitStack, closing
import ctypes
import errno
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile

from .protocol import SafetyBlocked


_DB_SUFFIXES = {'.sqlite', '.sqlite3', '.db'}
_SIDECARS = ('-wal', '-shm', '-journal')
_QUARANTINE = 'restore-quarantine.json'


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


def _connect(path, *, writable=False):
    return sqlite3.connect(path.as_uri() + ('?mode=rw' if writable else '?mode=ro'),
                           uri=True, timeout=0, isolation_level=None)


def _integrity(path, *, recover=False):
    with closing(_connect(path, writable=recover)) as connection:
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


def atomic_json(path, payload):
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
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _binding(path):
    value = json.loads(path.read_text(encoding='utf-8'))
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


def _snapshot(root, destination, databases, *, quarantine):
    staging = Path(tempfile.mkdtemp(prefix='.' + destination.name + '-', dir=destination.parent))
    try:
        for source in databases:
            target = staging / source.relative_to(root)
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
        binding = root / 'binding.json'
        if binding.exists():
            atomic_json(staging / 'binding.json', _binding(binding))
        if quarantine or (root / _QUARANTINE).exists():
            atomic_json(staging / _QUARANTINE, {'version': 1, 'reason': 'restored-state-requires-GET-only-recovery'})
        atomic_json(staging / 'backup-manifest.json', dict(version=1,
            databases=[path.relative_to(root).as_posix() for path in databases], binding=binding.exists()))
        for directory, _, _ in os.walk(staging, topdown=False, onerror=_walk_error):
            _sync_directory(Path(directory))
        _publish(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def _frozen_snapshot(root, destination, *, quarantine):
    from .demo_runtime import operator_lock

    root = _root(root)
    destination = _destination(root, destination)
    # Offline maintenance only: cooperating operators are excluded by session.lock;
    # BEGIN IMMEDIATE also excludes STOP and other writers bypassing that lock.
    with operator_lock(root, allow_quarantined=True), ExitStack() as stack:
        databases, _ = _inventory(root)
        required = sum(path.stat().st_size for path in databases)
        if shutil.disk_usage(destination.parent).free < required + 256 * 1024 * 1024:
            raise SafetyBlocked('insufficient backup destination space')
        if quarantine:
            manifest = json.loads((root / 'backup-manifest.json').read_text())
            expected = dict(version=1, databases=[path.relative_to(root).as_posix() for path in databases],
                            binding=(root / 'binding.json').exists())
            if manifest != expected:
                raise SafetyBlocked('incomplete backup inventory')
        for path in databases:
            connection = stack.enter_context(closing(_connect(path, writable=True)))
            connection.execute('BEGIN IMMEDIATE')
        for path in databases:
            _integrity(path)
        return _snapshot(root, destination, databases, quarantine=quarantine)


def backup_state(root, dest):
    """Publish an offline, coherent SQLite + binding backup; never copy credentials.

    All operators must be stopped. Active SQLite writers or an operator lock cause
    immediate failure. The destination must not exist, including an empty directory.
    """
    return _frozen_snapshot(root, dest, quarantine=False)


def restore_state(source, dest):
    """Restore without resetting budgets; persist GET-only recovery quarantine."""
    return _frozen_snapshot(source, dest, quarantine=True)
