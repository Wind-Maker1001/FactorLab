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
from .protocol import (StrategySpecV2, V1, V2, json_value, read_spec, read_decision, spec_payload)

WORKER = Path(__file__).with_name('worker.py')
SOURCE_ROOT = Path(__file__).resolve().parents[2]


def runtime_identity():
    return sha256(''.join(sha256(p.read_bytes()).hexdigest()
        for p in sorted((SOURCE_ROOT / 'factorlab').rglob('*.py'))).encode()).hexdigest()


def _binding_spec(spec):
    if isinstance(spec, StrategySpecV2):
        return spec_payload(spec)
    payload = asdict(spec)
    if not spec.historical_observations:
        del payload['historical_observations']
    return payload


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
        self.spec = read_spec(self._call('describe'))
        if self.spec.version != manifest['version']:
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
