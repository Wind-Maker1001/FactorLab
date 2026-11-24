"""Pinned, trusted Python files/packages loaded only by a clean worker process."""
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

from factorlab.trading.storage import dumps, decode
from .protocol import Decision, StrategySpec, json_value

WORKER = Path(__file__).with_name('worker.py')
SOURCE_ROOT = Path(__file__).resolve().parents[2]


def runtime_identity():
    return sha256(''.join(sha256(p.read_bytes()).hexdigest()
        for p in sorted((SOURCE_ROOT / 'factorlab').rglob('*.py'))).encode()).hexdigest()


class StrategyWorkerError(ValueError):
    pass


class LoadedStrategy:
    """No user module is imported into the caller, including during discovery.

    Packages are local, versioned directories with a strategy.json manifest and
    Python sources. No installation hooks, dependency downloads or entrypoints.
    """
    def __init__(self, path, *, trusted=False, timeout=15, class_name=None,
                 version=None, parameters=None, persistent_worker=False,
                 verify_runtime_each_call=True):
        if trusted is not True:
            raise ValueError('explicit trusted=True is required for Python strategy code')
        if type(persistent_worker) is not bool or type(verify_runtime_each_call) is not bool:
            raise ValueError('persistent_worker and verify_runtime_each_call must be booleans')
        self.timeout = timeout
        self.persistent_worker = persistent_worker
        self.verify_runtime_each_call = verify_runtime_each_call
        self._worker_tempdir = None
        self._worker_proc = None
        self._worker_queue = None
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
        try:
            self.spec = StrategySpec(**self._call('describe'))
            if self.spec.version != manifest['version']:
                raise ValueError('strategy version differs from manifest')
        except Exception:
            self.close()
            raise
        self.binding = dict(protocol='FACTORLAB_DECISION_V1', spec=asdict(self.spec),
            configuration=manifest['parameters'], code_sha256=self.code_sha256,
            entrypoint=manifest['file'] + ':' + manifest['class'],
            runtime_sha256=runtime_identity())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def close(self):
        proc = getattr(self, '_worker_proc', None)
        self._worker_proc = None
        self._worker_queue = None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
        tempdir = getattr(self, '_worker_tempdir', None)
        self._worker_tempdir = None
        if tempdir is not None:
            try:
                tempdir.cleanup()
            except Exception:
                pass

    def _ensure_worker(self):
        if self._worker_proc is not None and self._worker_proc.poll() is None:
            return
        self.close()
        tempdir = tempfile.TemporaryDirectory(prefix='factorlab-strategy-')
        root = Path(tempdir.name)
        for name, source in self.sources.items():
            dest = root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(source, encoding='utf-8')
        environment = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR') if key in os.environ}
        proc = subprocess.Popen(
            [sys.executable, '-I', '-B', str(WORKER), '--serve', str(SOURCE_ROOT), str(root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=root, env=environment, close_fds=True, text=True, encoding='utf-8', bufsize=1,
        )
        out_q: queue.Queue[str | None] = queue.Queue()

        def _reader(stream, q):
            try:
                while True:
                    line = stream.readline(4 * 1024 * 1024 + 2)
                    if not line:
                        q.put(None)
                        break
                    q.put(line)
            except Exception:
                q.put(None)

        t = threading.Thread(target=_reader, args=(proc.stdout, out_q), daemon=True)
        t.start()
        self._worker_tempdir = tempdir
        self._worker_proc = proc
        self._worker_queue = out_q

    def _call_persistent(self, operation, event=None, state=None):
        self._ensure_worker()
        request = dict(operation=operation, manifest=self.manifest, event=event, state=state)
        payload = dumps(request)
        if len(payload) > 4 * 1024 * 1024:
            raise StrategyWorkerError('strategy request exceeds 4 MiB limit')
        try:
            assert self._worker_proc is not None and self._worker_proc.stdin is not None
            self._worker_proc.stdin.write(payload + '\n')
            self._worker_proc.stdin.flush()
        except OSError as error:
            self.close()
            raise StrategyWorkerError('strategy worker failed (no decision committed)') from error
        try:
            assert self._worker_queue is not None
            raw = self._worker_queue.get(timeout=self.timeout)
        except queue.Empty as error:
            self.close()
            raise StrategyWorkerError('strategy worker timed out') from error
        if raw is None or len(raw) > 4 * 1024 * 1024 + 1:
            self.close()
            raise StrategyWorkerError('strategy worker failed (no decision committed)')
        try:
            response = decode(json.loads(raw))
            if response.get('error'):
                raise StrategyWorkerError(response['error'])
            return response['value']
        except (KeyError, json.JSONDecodeError) as error:
            self.close()
            raise StrategyWorkerError('invalid strategy worker response') from error

    def _call(self, operation, event=None, state=None):
        if self.persistent_worker:
            return self._call_persistent(operation, event, state)
        with tempfile.TemporaryDirectory(prefix='factorlab-strategy-') as directory:
            root = Path(directory)
            for name, source in self.sources.items():
                dest = root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(source, encoding='utf-8')
            request = dict(operation=operation, manifest=self.manifest, event=event, state=state)
            # No inherited environment, user HOME, current directory, handles,
            # PYTHONPATH, cloud credentials or Demo credential store location.
            environment = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR') if key in os.environ}
            try:
                result = subprocess.run([sys.executable, '-I', '-B', str(WORKER),
                    str(SOURCE_ROOT), str(root)], input=dumps(request), encoding='utf-8',
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=root,
                    env=environment, close_fds=True, timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired as error:
                raise StrategyWorkerError('strategy worker timed out') from error
            if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
                raise StrategyWorkerError('strategy worker failed (no decision committed)')
            try:
                response = decode(json.loads(result.stdout))
                if response.get('error'):
                    raise StrategyWorkerError(response['error'])
                return response['value']
            except (KeyError, json.JSONDecodeError) as error:
                raise StrategyWorkerError('invalid strategy worker response') from error

    def decide(self, event, state):
        if self.verify_runtime_each_call and runtime_identity() != self.binding['runtime_sha256']:
            raise StrategyWorkerError('strategy runtime code identity changed')
        result = self._call('decide', event, json_value(state))
        return Decision(**result).validate(self.spec, event['symbol'])
