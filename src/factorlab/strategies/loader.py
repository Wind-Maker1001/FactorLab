"""Pinned, trusted Python files/packages loaded only by a clean worker process."""
import atexit
from collections import OrderedDict
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import weakref

from factorlab.trading.storage import dumps, decode
from .protocol import (StrategySpecV2, V1, V2, json_value, read_spec, read_decision, spec_payload)

WORKER = Path(__file__).with_name('worker.py')
SOURCE_ROOT = Path(__file__).resolve().parents[2]
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024

_IDENTITY_LOCK = threading.Lock()
_IDENTITY_CACHE = None  # (dir_mtimes, file_stats, digest)

_POOL_LOCK = threading.Lock()
_ACTIVE_WORKERS: OrderedDict[int, '_PersistentWorker'] = OrderedDict()


def _worker_pool_limit() -> int:
    raw = os.environ.get('FACTORLAB_STRATEGY_WORKER_POOL_MAX', '12')
    try:
        value = int(raw)
    except ValueError:
        return 12
    return max(0, value)


def _scan_runtime_tree():
    root = SOURCE_ROOT / 'factorlab'
    dir_mtimes = []
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != '__pycache__')
        try:
            d_st = os.stat(dirpath)
        except OSError:
            continue
        dir_mtimes.append((dirpath, d_st.st_mtime_ns))
        for name in sorted(filenames):
            if name.endswith('.py'):
                full = os.path.join(dirpath, name)
                files.append(full)
    files.sort()
    return dir_mtimes, files


def _stat_matches(dir_mtimes, file_stats) -> bool:
    for dirpath, expected_mtime in dir_mtimes:
        try:
            if os.stat(dirpath).st_mtime_ns != expected_mtime:
                return False
        except OSError:
            return False
    for path, expected_mtime, expected_size in file_stats:
        try:
            st = os.stat(path)
        except OSError:
            return False
        if st.st_mtime_ns != expected_mtime or st.st_size != expected_size:
            return False
    return True


def runtime_identity():
    global _IDENTITY_CACHE
    with _IDENTITY_LOCK:
        cached = _IDENTITY_CACHE
        if cached is not None and _stat_matches(cached[0], cached[1]):
            return cached[2]
        dir_mtimes, files = _scan_runtime_tree()
        file_stats = []
        parts = []
        for path in files:
            st = os.stat(path)
            with open(path, 'rb') as fh:
                data = fh.read()
            parts.append(sha256(data).hexdigest())
            file_stats.append((path, st.st_mtime_ns, st.st_size))
        digest = sha256(''.join(parts).encode()).hexdigest()
        _IDENTITY_CACHE = (tuple(dir_mtimes), tuple(file_stats), digest)
        return digest


def _binding_spec(spec):
    if isinstance(spec, StrategySpecV2):
        return spec_payload(spec)
    payload = asdict(spec)
    if not spec.historical_observations:
        del payload['historical_observations']
    return payload


class StrategyWorkerError(ValueError):
    pass


class _PersistentWorker:
    """Dedicated isolated subprocess for one LoadedStrategy, preserving all sandbox boundaries."""

    def __init__(self, sources: dict, manifest: dict):
        self._sources = sources
        self._manifest = manifest
        self._lock = threading.Lock()
        self._tempdir = None
        self._proc = None
        self._queue = None
        self._reader = None
        self._closed = False

    def _start_locked(self, timeout: float):
        self._stop_locked()
        tempdir = tempfile.TemporaryDirectory(prefix='factorlab-strategy-')
        root = Path(tempdir.name)
        for name, source in self._sources.items():
            dest = root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(source, encoding='utf-8')
        environment = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR') if key in os.environ}
        proc = subprocess.Popen(
            [sys.executable, '-I', '-B', str(WORKER), str(SOURCE_ROOT), str(root), '--serve'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=root,
            env=environment,
            close_fds=True,
            text=True,
            encoding='utf-8',
            bufsize=1,
        )
        line_queue: queue.Queue = queue.Queue(maxsize=4)

        def _pump():
            try:
                while True:
                    line = proc.stdout.readline(_MAX_RESPONSE_BYTES + 2)
                    if not line:
                        line_queue.put(None)
                        break
                    line_queue.put(line)
                    if len(line) > _MAX_RESPONSE_BYTES + 1:
                        break
            except Exception:
                line_queue.put(None)

        reader = threading.Thread(target=_pump, name='factorlab-strategy-reader', daemon=True)
        reader.start()
        self._tempdir = tempdir
        self._proc = proc
        self._queue = line_queue
        self._reader = reader

        init_reply = self._exchange_locked(dict(manifest=self._manifest), timeout=timeout)
        if not init_reply.get('ready'):
            err = init_reply.get('error')
            self._stop_locked()
            if err:
                raise StrategyWorkerError(err)
            raise StrategyWorkerError('strategy worker failed (no decision committed)')

    def _exchange_locked(self, payload: dict, *, timeout: float) -> dict:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise StrategyWorkerError('strategy worker failed (no decision committed)')
        try:
            proc.stdin.write(dumps(payload) + '\n')
            proc.stdin.flush()
        except OSError as error:
            self._stop_locked()
            raise StrategyWorkerError('strategy worker failed (no decision committed)') from error
        try:
            line = self._queue.get(timeout=timeout)
        except queue.Empty as error:
            self._stop_locked()
            raise StrategyWorkerError('strategy worker timed out') from error
        if line is None or len(line) > _MAX_RESPONSE_BYTES + 1:
            self._stop_locked()
            raise StrategyWorkerError('strategy worker failed (no decision committed)')
        try:
            return decode(json.loads(line))
        except json.JSONDecodeError as error:
            self._stop_locked()
            raise StrategyWorkerError('invalid strategy worker response') from error

    def call(self, operation: str, event=None, state=None, *, timeout: float):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start_locked(timeout=timeout)
            response = self._exchange_locked(
                dict(operation=operation, event=event, state=state),
                timeout=timeout,
            )
            if response.get('error'):
                raise StrategyWorkerError(response['error'])
            if 'value' not in response:
                self._stop_locked()
                raise StrategyWorkerError('invalid strategy worker response')
            return response['value']

    def _stop_locked(self):
        proc = self._proc
        self._proc = None
        self._queue = None
        self._reader = None
        if proc is not None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2.0)
            except OSError:
                pass
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        tempdir = self._tempdir
        self._tempdir = None
        if tempdir is not None:
            try:
                tempdir.cleanup()
            except OSError:
                pass

    def close(self):
        with self._lock:
            self._closed = True
            self._stop_locked()


def _register_pool_worker(worker: _PersistentWorker):
    limit = _worker_pool_limit()
    evicted = []
    with _POOL_LOCK:
        wid = id(worker)
        if wid in _ACTIVE_WORKERS:
            _ACTIVE_WORKERS.move_to_end(wid)
        else:
            _ACTIVE_WORKERS[wid] = worker
        while len(_ACTIVE_WORKERS) > max(1, limit):
            evicted_id, old_worker = _ACTIVE_WORKERS.popitem(last=False)
            if evicted_id != wid:
                evicted.append(old_worker)
            else:
                _ACTIVE_WORKERS[evicted_id] = old_worker
                break
    for old_worker in evicted:
        old_worker.close()


def _unregister_pool_worker(worker: _PersistentWorker):
    with _POOL_LOCK:
        _ACTIVE_WORKERS.pop(id(worker), None)


def _shutdown_all_workers():
    with _POOL_LOCK:
        workers = list(_ACTIVE_WORKERS.values())
        _ACTIVE_WORKERS.clear()
    for worker in workers:
        worker.close()


atexit.register(_shutdown_all_workers)


class LoadedStrategy:
    """No user module is imported into the caller, including during discovery.

    Packages are local, versioned directories with a strategy.json manifest and
    Python sources. No installation hooks, dependency downloads or entrypoints.
    """
    def __init__(self, path, *, trusted=False, timeout=15, class_name=None,
                 version=None, parameters=None):
        if trusted is not True:
            raise ValueError('explicit trusted=True is required for Python strategy code')
        self.timeout = timeout
        self._worker = None
        self._finalizer = None
        path = Path(path).resolve()
        self.manifest_path = path / 'strategy.json' if path.is_dir() else path
        manifest = (dict(file=path.name, **{'class': class_name}, version=version,
                         parameters=parameters or {}) if path.suffix == '.py' else
                    json.loads(self.manifest_path.read_text(encoding='utf-8')))
        if set(manifest) - {'file', 'class', 'version', 'parameters', 'sha256', 'package'} or not {
                'file', 'class', 'version', 'parameters'} <= set(manifest):
            raise ValueError('strategy manifest requires file, class, version, parameters')
        if (not isinstance(manifest['version'], str) or not manifest['version']
                or not isinstance(manifest['class'], str) or not manifest['class'].isidentifier()
                or not isinstance(manifest['parameters'], dict)):
            raise ValueError('invalid strategy manifest')
        self.manifest = json_value(manifest)
        root = self.manifest_path.parent
        source = (root / manifest['file']).resolve()
        if not source.is_relative_to(root) or source.suffix != '.py' or not source.is_file():
            raise ValueError('strategy source must be a local Python file below the manifest')
        paths = sorted(root.rglob('*.py')) if manifest.get('package', False) else [source]
        if any(p.is_symlink() or not p.resolve().is_relative_to(root) for p in paths):
            raise ValueError('strategy package cannot contain linked sources')
        self.sources = {p.relative_to(root).as_posix(): p.read_text(encoding='utf-8') for p in paths}
        self.code_sha256 = sha256(json.dumps(self.sources, sort_keys=True).encode()).hexdigest()
        if manifest.get('sha256', self.code_sha256) != self.code_sha256:
            raise ValueError('strategy code identity changed')
        self.spec = read_spec(self._call('describe'))
        if self.spec.version != manifest['version']:
            self.close()
            raise ValueError('strategy version differs from manifest')
        self.binding = dict(protocol=V2 if isinstance(self.spec, StrategySpecV2) else V1, spec=_binding_spec(self.spec),
            configuration=manifest['parameters'], code_sha256=self.code_sha256,
            entrypoint=manifest['file'] + ':' + manifest['class'],
            runtime_sha256=runtime_identity())

    @classmethod
    def from_strategy_package(cls, release_dir, manifest=None, *, trusted=False, timeout=15, qualify=None):
        """Load a verified S03 StrategyPackage using the S01 isolated worker process."""
        if trusted is not True:
            raise ValueError('explicit trusted=True is required for Python strategy code')
        from .package import digest, load_package, package_entrypoint_file, validate_manifest, manifest_spec
        root = Path(release_dir).resolve()
        installed = load_package(root, qualify=qualify)
        if manifest is not None:
            validate_manifest(manifest, qualify=qualify)
            if manifest != installed:
                raise ValueError('strategy package manifest differs from installed release')
        if installed['interface'] not in {V1, V2}:
            raise ValueError('decision StrategyPackage required')
        entry_file, class_name = package_entrypoint_file(installed)
        paths = [root / name for name in sorted(installed['files'])]
        if any(p.is_symlink() or not p.resolve().is_relative_to(root) or not p.is_file() for p in paths):
            raise ValueError('strategy package cannot contain linked or missing sources')
        instance = cls.__new__(cls)
        instance.timeout = timeout
        instance._worker = None
        instance._finalizer = None
        instance.manifest_path = root / 'manifest.json'
        instance.package_manifest = json_value(installed)
        instance.manifest = json_value(dict(
            file=entry_file,
            **{'class': class_name},
            version=installed['strategy_version'],
            parameters=installed['configuration'],
            package=bool(installed.get('package', False)),
            dependencies=list(installed['dependencies']),
        ))
        instance.sources = {name: (root / name).read_text(encoding='utf-8') for name in sorted(installed['files'])}
        instance.code_sha256 = sha256(json.dumps(instance.sources, sort_keys=True).encode()).hexdigest()
        instance.spec = read_spec(instance._call('describe'))
        try:
            if isinstance(instance.spec, StrategySpecV2) != (installed['interface'] == V2):
                raise ValueError('strategy/package protocol mismatch')
            if instance.spec.strategy_id != installed['strategy_id']:
                raise ValueError('strategy_id differs from StrategyPackage manifest')
            if instance.spec.version != installed['strategy_version']:
                raise ValueError('strategy_version differs from StrategyPackage manifest')
            if installed['interface'] == V2:
                if instance.spec != manifest_spec(installed):
                    raise ValueError('strategy requirements differ from StrategyPackage manifest')
            else:
                if instance.spec.state_version != str(installed['state_version']):
                    raise ValueError('state_version differs from StrategyPackage manifest')
                if installed.get('execution_mode') is not None and instance.spec.decision_kind != installed['execution_mode']:
                    raise ValueError('execution_mode differs from StrategySpec decision_kind')
                if installed['symbol'] not in instance.spec.symbols:
                    raise ValueError('strategy package symbol unsupported by StrategySpec')
                interval_seconds = installed.get('interval_seconds', 60)
                if interval_seconds not in instance.spec.intervals:
                    raise ValueError('strategy package interval unsupported by StrategySpec')
        except Exception:
            instance.close()
            raise
        instance.binding = dict(
            protocol=installed['interface'],
            package_id=installed['package_id'],
            code_commit=installed['code_commit'],
            spec=_binding_spec(instance.spec),
            configuration=installed['configuration'],
            code_sha256=instance.code_sha256,
            entrypoint=installed['entrypoint'],
            runtime_sha256=runtime_identity(),
            evidence_sha256=digest(installed['evidence']),
        )
        return instance

    def _call_oneshot(self, operation, event=None, state=None):
        with tempfile.TemporaryDirectory(prefix='factorlab-strategy-') as directory:
            root = Path(directory)
            for name, source in self.sources.items():
                dest = root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(source, encoding='utf-8')
            request = dict(operation=operation, manifest=self.manifest, event=event, state=state)
            environment = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR') if key in os.environ}
            try:
                result = subprocess.run([sys.executable, '-I', '-B', str(WORKER),
                    str(SOURCE_ROOT), str(root)], input=dumps(request), encoding='utf-8',
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=root,
                    env=environment, close_fds=True, timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired as error:
                raise StrategyWorkerError('strategy worker timed out') from error
            if result.returncode or len(result.stdout) > _MAX_RESPONSE_BYTES:
                raise StrategyWorkerError('strategy worker failed (no decision committed)')
            try:
                response = decode(json.loads(result.stdout))
                if response.get('error'):
                    raise StrategyWorkerError(response['error'])
                return response['value']
            except (KeyError, json.JSONDecodeError) as error:
                raise StrategyWorkerError('invalid strategy worker response') from error

    def _call(self, operation, event=None, state=None):
        if _worker_pool_limit() == 0:
            return self._call_oneshot(operation, event, state)
        if self._worker is None:
            worker = _PersistentWorker(self.sources, self.manifest)
            self._worker = worker
            self._finalizer = weakref.finalize(self, _cleanup_worker, worker)
        _register_pool_worker(self._worker)
        return self._worker.call(operation, event, state, timeout=self.timeout)

    def close(self):
        worker = getattr(self, '_worker', None)
        if worker is not None:
            self._worker = None
            _unregister_pool_worker(worker)
            worker.close()
        finalizer = getattr(self, '_finalizer', None)
        if finalizer is not None:
            finalizer.detach()
            self._finalizer = None

    def decide(self, event, state):
        if runtime_identity() != self.binding['runtime_sha256']:
            raise StrategyWorkerError('strategy runtime code identity changed')
        if isinstance(self.spec, StrategySpecV2):
            from .events import validate_event
            validate_event(event, self.spec)
        result = self._call('decide', event, json_value(state))
        return read_decision(result).validate(self.spec,
            None if isinstance(self.spec, StrategySpecV2) else event['symbol'])

    def migrate_state(self, state, from_version):
        if runtime_identity() != self.binding['runtime_sha256']:
            raise StrategyWorkerError('strategy runtime code identity changed')
        if not isinstance(self.spec, StrategySpecV2):
            raise ValueError('state migration requires V2')
        policy = self.spec.state
        if policy['migration'] == 'reset':
            return {}
        if policy['migration'] == 'preserve':
            if from_version != policy['version']:
                raise ValueError('preserve requires the same state schema')
            return json_value(state)
        if from_version not in policy['from_versions']:
            raise ValueError('source state schema not declared for migration')
        result = self._call('migrate', dict(from_version=from_version), json_value(state))
        if not isinstance(result, dict):
            raise ValueError('migrated state must be a JSON object')
        return json_value(result)


def _cleanup_worker(worker: _PersistentWorker):
    _unregister_pool_worker(worker)
    worker.close()
