"""Pinned, trusted Python files/packages loaded only by clean one-shot worker processes."""
import atexit
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import weakref

from factorlab.trading.storage import dumps, decode
from .protocol import (StrategySpecV2, V1, V2, json_value, read_spec, read_decision, spec_payload)

WORKER = Path(__file__).with_name('worker.py')
SOURCE_ROOT = Path(__file__).resolve().parents[2]
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_DEFAULT_HOST_POOL_MAX = 1
_DEFAULT_VM_POOL_MAX = 2

_IDENTITY_LOCK = threading.Lock()
_IDENTITY_CACHE = None  # (dir_mtimes, file_stats, digest)

_VM_LOCAL_LOCK = threading.Lock()
_VM_LOCAL_HELD: set[Path] = set()


def worker_vm_limit() -> int | None:
    """Optional VM-wide concurrency cap across co-located host processes."""
    raw = os.environ.get('FACTORLAB_STRATEGY_WORKER_VM_MAX')
    lock_dir = os.environ.get('FACTORLAB_STRATEGY_WORKER_LOCK_DIR')
    if raw is None or not raw.strip():
        if lock_dir and lock_dir.strip():
            return _DEFAULT_VM_POOL_MAX
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_VM_POOL_MAX


def worker_vm_lock_dir() -> Path | None:
    """Shared lock directory for VM-wide cross-process worker concurrency capping."""
    vm_limit = worker_vm_limit()
    if vm_limit is None:
        return None
    raw_dir = os.environ.get('FACTORLAB_STRATEGY_WORKER_LOCK_DIR')
    if raw_dir and raw_dir.strip():
        return Path(raw_dir).resolve()
    suffix = str(os.getuid()) if hasattr(os, 'getuid') else 'local'
    return (Path(tempfile.gettempdir()) / f'factorlab-worker-slots-{suffix}').resolve()


def worker_pool_limit() -> int:
    """Per-host-process concurrency cap (default 1 so Binance+Alpaca sum to <=2 on 2 vCPU/1 GiB VMs)."""
    raw = os.environ.get('FACTORLAB_STRATEGY_WORKER_POOL_MAX')
    if raw is None or not raw.strip():
        host_limit = _DEFAULT_HOST_POOL_MAX
    else:
        try:
            host_limit = max(1, int(raw))
        except ValueError:
            host_limit = _DEFAULT_HOST_POOL_MAX
    vm_limit = worker_vm_limit()
    if vm_limit is not None:
        return min(host_limit, vm_limit)
    return host_limit


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


def _terminate_and_reap(proc: subprocess.Popen | None):
    """Ensure child process has strictly exited before capacity is released."""
    if proc is None:
        return
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
                proc.wait(timeout=5.0)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)
    except OSError:
        pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


class _VmSlotLease:
    """Crash-safe OS file-lock lease enforcing VM-wide concurrency across host processes."""

    def __init__(self, path: Path, handle):
        self._path = path
        self._handle = handle
        self._released = False

    def release(self):
        if self._released:
            return
        self._released = True
        try:
            self._handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                self._handle.close()
            except OSError:
                pass
            with _VM_LOCAL_LOCK:
                _VM_LOCAL_HELD.discard(self._path)


def _try_acquire_vm_slot(slot_path: Path) -> _VmSlotLease | None:
    with _VM_LOCAL_LOCK:
        if slot_path in _VM_LOCAL_HELD:
            return None
        _VM_LOCAL_HELD.add(slot_path)
    handle = None
    try:
        handle = slot_path.open('a+b', buffering=0)
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _VmSlotLease(slot_path, handle)
    except BlockingIOError:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        with _VM_LOCAL_LOCK:
            _VM_LOCAL_HELD.discard(slot_path)
        return None
    except OSError as error:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        with _VM_LOCAL_LOCK:
            _VM_LOCAL_HELD.discard(slot_path)
        if os.name == 'nt' and error.errno in {13, 36}:
            return None
        raise


def _acquire_vm_slot(lock_dir: Path, vm_limit: int, *, deadline: float, cancel_check) -> _VmSlotLease:
    lock_dir.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt' and not os.environ.get('FACTORLAB_STRATEGY_WORKER_LOCK_DIR'):
        try:
            os.chmod(lock_dir, 0o700)
        except OSError:
            pass
    slot_paths = [lock_dir / f'worker-slot-{idx}.lock' for idx in range(vm_limit)]
    backoff = 0.005
    while True:
        if cancel_check():
            raise StrategyWorkerError('strategy loader is closed')
        for slot_path in slot_paths:
            lease = _try_acquire_vm_slot(slot_path)
            if lease is not None:
                return lease
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StrategyWorkerError('strategy worker timed out')
        time.sleep(min(backoff, remaining))
        backoff = min(0.05, backoff * 1.5)


class _ExecutionSlot:
    """Tracks one reserved execution capacity unit from reservation through confirmed reap."""

    def __init__(self, strategy: 'LoadedStrategy'):
        self.strategy = strategy
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.cancelled = False

    def cancel_and_terminate(self, *, force: bool = False):
        with self.lock:
            self.cancelled = True
            proc = self.proc
            if proc is not None and proc.poll() is None:
                try:
                    if force:
                        proc.kill()
                    else:
                        proc.terminate()
                except OSError:
                    pass


class _WorkerCapacityPool:
    """Atomically reserves, tracks, and releases bounded one-shot worker slots.

    Lock ordering invariant: never acquire _cond while holding slot.lock.
    Capacity is released ONLY after the OS child process has confirmed exit.
    """

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._reserved: set[_ExecutionSlot] = set()
        self._shutting_down = threading.Event()

    def is_shutting_down(self) -> bool:
        return self._shutting_down.is_set()

    def acquire(self, strategy: 'LoadedStrategy', *, limit: int, deadline: float) -> _ExecutionSlot:
        with self._cond:
            while (len(self._reserved) >= limit
                   and not strategy._closed
                   and not self._shutting_down.is_set()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StrategyWorkerError('strategy worker timed out')
                self._cond.wait(timeout=remaining)
            if strategy._closed or self._shutting_down.is_set():
                raise StrategyWorkerError('strategy loader is closed')
            slot = _ExecutionSlot(strategy)
            self._reserved.add(slot)
            strategy._active_slots.add(slot)
            return slot

    def release(self, slot: _ExecutionSlot):
        with self._cond:
            self._reserved.discard(slot)
            slot.strategy._active_slots.discard(slot)
            self._cond.notify_all()

    def close_strategy(self, strategy: 'LoadedStrategy'):
        with self._cond:
            strategy._closed = True
            slots = list(strategy._active_slots)
            self._cond.notify_all()
        for slot in slots:
            slot.cancel_and_terminate(force=False)
        soft_deadline = time.monotonic() + 1.0
        with self._cond:
            while strategy._active_slots:
                remaining = soft_deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            stubborn = list(strategy._active_slots)
        if stubborn:
            for slot in stubborn:
                slot.cancel_and_terminate(force=True)
            hard_deadline = time.monotonic() + 4.0
            with self._cond:
                while strategy._active_slots:
                    remaining = hard_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(timeout=remaining)

    def shutdown_all(self):
        with self._cond:
            self._shutting_down.set()
            slots = list(self._reserved)
            self._cond.notify_all()
        for slot in slots:
            slot.cancel_and_terminate(force=False)
        soft_deadline = time.monotonic() + 1.0
        with self._cond:
            while self._reserved:
                remaining = soft_deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            stragglers = list(self._reserved)
        if stragglers:
            for slot in stragglers:
                slot.cancel_and_terminate(force=True)
            hard_deadline = time.monotonic() + 4.0
            with self._cond:
                while self._reserved:
                    remaining = hard_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(timeout=remaining)
            for slot in list(stragglers):
                with slot.lock:
                    proc = slot.proc
                _terminate_and_reap(proc)

    def active_count(self) -> int:
        with self._cond:
            return len(self._reserved)

    def live_pids(self) -> tuple[int, ...]:
        with self._cond:
            slots = list(self._reserved)
        pids = []
        for slot in slots:
            with slot.lock:
                proc = slot.proc
                if proc is not None and proc.poll() is None:
                    pids.append(proc.pid)
        return tuple(pids)


_WORKER_POOL = _WorkerCapacityPool()
atexit.register(_WORKER_POOL.shutdown_all)


def active_worker_count() -> int:
    """Return the number of currently reserved worker slots in this host process."""
    return _WORKER_POOL.active_count()


def live_worker_pids() -> tuple[int, ...]:
    """Return PIDs of currently alive strategy worker subprocesses in this host process."""
    return _WORKER_POOL.live_pids()


def _communicate_bounded(proc: subprocess.Popen, payload_bytes: bytes, *, deadline: float) -> tuple[bytes, bool]:
    stdout_chunks: list[bytes] = []
    oversized = False

    def _read_stdout():
        nonlocal oversized
        total = 0
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    oversized = True
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    break
                stdout_chunks.append(chunk)
        except OSError:
            pass

    def _drain_stderr():
        total = 0
        try:
            while True:
                chunk = proc.stderr.read(8192)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_STDERR_BYTES:
                    # Keep draining without buffering to avoid pipe deadlock.
                    continue
        except OSError:
            pass

    def _write_stdin():
        try:
            if proc.stdin is not None:
                proc.stdin.write(payload_bytes)
                proc.stdin.flush()
        except OSError:
            pass
        finally:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

    t_out = threading.Thread(target=_read_stdout, name='factorlab-worker-stdout', daemon=True)
    t_err = threading.Thread(target=_drain_stderr, name='factorlab-worker-stderr', daemon=True)
    t_in = threading.Thread(target=_write_stdin, name='factorlab-worker-stdin', daemon=True)
    t_out.start()
    t_err.start()
    t_in.start()

    remaining = max(0.01, deadline - time.monotonic())
    try:
        proc.wait(timeout=remaining)
    except subprocess.TimeoutExpired as error:
        _terminate_and_reap(proc)
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
        t_in.join(timeout=1.0)
        raise StrategyWorkerError('strategy worker timed out') from error

    t_out.join(timeout=2.0)
    t_err.join(timeout=1.0)
    t_in.join(timeout=1.0)
    return b''.join(stdout_chunks), oversized


class LoadedStrategy:
    """No user module is imported into the caller, including during discovery.

    Packages are local, versioned directories with a strategy.json manifest and
    Python sources. No installation hooks, dependency downloads or entrypoints.
    Every request runs in a fresh, bounded one-shot worker subprocess so module
    globals, class attributes, submodules, and interpreter state never leak.
    """

    def __init__(self, path, *, trusted=False, timeout=15, class_name=None,
                 version=None, parameters=None):
        if trusted is not True:
            raise ValueError('explicit trusted=True is required for Python strategy code')
        self.timeout = timeout
        self._closed = False
        self._active_slots: set[_ExecutionSlot] = set()
        self._finalizer = weakref.finalize(self, _cleanup_slots, self._active_slots)
        path = Path(path).resolve()
        self.manifest_path = path / 'strategy.json' if path.is_dir() else path
        manifest = (dict(file=path.name, **{'class': class_name}, version=version,
                         parameters=parameters or {}) if path.suffix == '.py' else
                    json.loads(self.manifest_path.read_text(encoding='utf-8')))
        if set(manifest) - {'file', 'class', 'version', 'parameters', 'sha256', 'package',
                            'runtime_profile', 'calendar_timezone'} or not {
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
        instance._closed = False
        instance._active_slots = set()
        instance._finalizer = weakref.finalize(instance, _cleanup_slots, instance._active_slots)
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
        if 'runtime_profile' in installed:
            instance.manifest['runtime_profile'] = installed['runtime_profile']
        if installed['interface'] == V2:
            schedule = installed.get('requirements', {}).get('schedule', {})
            if schedule.get('kind') == 'calendar' and schedule.get('timezone'):
                instance.manifest['calendar_timezone'] = schedule['timezone']
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

    def _call(self, operation, event=None, state=None):
        deadline = time.monotonic() + float(self.timeout)
        pool_limit = worker_pool_limit()
        slot = _WORKER_POOL.acquire(self, limit=pool_limit, deadline=deadline)
        vm_lease = None
        proc = None
        try:
            vm_limit = worker_vm_limit()
            if vm_limit is not None:
                lock_dir = worker_vm_lock_dir()
                assert lock_dir is not None
                vm_lease = _acquire_vm_slot(
                    lock_dir,
                    vm_limit,
                    deadline=deadline,
                    cancel_check=lambda: self._closed or slot.cancelled or _WORKER_POOL.is_shutting_down(),
                )
            with tempfile.TemporaryDirectory(prefix='factorlab-strategy-') as directory:
                root = Path(directory)
                for name, source in self.sources.items():
                    dest = root / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(source, encoding='utf-8')
                request = dict(operation=operation, manifest=self.manifest, event=event, state=state)
                payload_bytes = dumps(request).encode('utf-8')
                environment = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR') if key in os.environ}

                with slot.lock:
                    if self._closed or slot.cancelled or _WORKER_POOL.is_shutting_down():
                        raise StrategyWorkerError('strategy loader is closed')
                    if time.monotonic() >= deadline:
                        raise StrategyWorkerError('strategy worker timed out')
                    proc = subprocess.Popen(
                        [sys.executable, '-I', '-B', str(WORKER), str(SOURCE_ROOT), str(root)],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        cwd=root,
                        env=environment,
                        close_fds=True,
                    )
                    slot.proc = proc

                stdout_bytes, oversized = _communicate_bounded(proc, payload_bytes, deadline=deadline)
                returncode = proc.returncode
                _terminate_and_reap(proc)
                proc = None

                if self._closed or slot.cancelled:
                    raise StrategyWorkerError('strategy loader is closed')
                if returncode or oversized or len(stdout_bytes) > _MAX_RESPONSE_BYTES:
                    raise StrategyWorkerError('strategy worker failed (no decision committed)')
                try:
                    response = decode(json.loads(stdout_bytes.decode('utf-8')))
                    if response.get('error'):
                        raise StrategyWorkerError(response['error'])
                    return response['value']
                except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise StrategyWorkerError('invalid strategy worker response') from error
        finally:
            if proc is not None:
                _terminate_and_reap(proc)
            with slot.lock:
                slot.proc = None
            if vm_lease is not None:
                vm_lease.release()
            _WORKER_POOL.release(slot)

    def close(self):
        _WORKER_POOL.close_strategy(self)
        finalizer = getattr(self, '_finalizer', None)
        if finalizer is not None:
            finalizer.detach()
            self._finalizer = None

    def decide(self, event, state):
        if self._closed:
            raise StrategyWorkerError('strategy loader is closed')
        if runtime_identity() != self.binding['runtime_sha256']:
            raise StrategyWorkerError('strategy runtime code identity changed')
        if isinstance(self.spec, StrategySpecV2):
            from .events import validate_event
            validate_event(event, self.spec)
        result = self._call('decide', event, json_value(state))
        return read_decision(result).validate(self.spec,
            None if isinstance(self.spec, StrategySpecV2) else event['symbol'])

    def migrate_state(self, state, from_version):
        if self._closed:
            raise StrategyWorkerError('strategy loader is closed')
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


def _cleanup_slots(slots: set[_ExecutionSlot]):
    for slot in list(slots):
        slot.cancel_and_terminate()
