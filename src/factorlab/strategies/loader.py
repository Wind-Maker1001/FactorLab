"""Pinned, trusted Python files/packages loaded only by a clean worker process."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

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
                 version=None, parameters=None):
        if trusted is not True:
            raise ValueError('explicit trusted=True is required for Python strategy code')
        self.timeout = timeout
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
        self.spec = StrategySpec(**self._call('describe'))
        if self.spec.version != manifest['version']:
            raise ValueError('strategy version differs from manifest')
        self.binding = dict(protocol='FACTORLAB_DECISION_V1', spec=asdict(self.spec),
            configuration=manifest['parameters'], code_sha256=self.code_sha256,
            entrypoint=manifest['file'] + ':' + manifest['class'],
            runtime_sha256=runtime_identity())

    def _call(self, operation, event=None, state=None):
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
        if runtime_identity() != self.binding['runtime_sha256']:
            raise StrategyWorkerError('strategy runtime code identity changed')
        result = self._call('decide', event, json_value(state))
        return Decision(**result).validate(self.spec, event['symbol'])
