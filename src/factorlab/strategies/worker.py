"""Private JSON stdin/stdout worker. Never started with account credentials."""
import ast
import contextlib
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import sys

_MAX_MESSAGE_BYTES = 4 * 1024 * 1024
_NUMERIC_TRIGGER_PREFIXES = (
    'numpy',
    'pandas',
    'pyarrow',
    'factorlab.strategies.v69',
    'factorlab.strategies.dual_ma',
    'factorlab.strategies.adaptive_grid_v2',
    'factorlab.strategies.grid_restore',
    'factorlab.experiments',
    'factorlab.integrations.alpaca',
)


_NUMERIC_SYMBOL_NAMES = frozenset({'DualMAAdapter', 'DualMAStrategy'})


def _needs_numeric_preimport(package: Path, manifest: dict) -> bool:
    for req in manifest.get('dependencies', ()):
        name = req.split('==', 1)[0].strip().lower()
        if name in {'numpy', 'pandas', 'pyarrow'}:
            return True
    if manifest.get('class') in _NUMERIC_SYMBOL_NAMES:
        return True
    for py_file in sorted(package.rglob('*.py')):
        try:
            tree = ast.parse(py_file.read_text(encoding='utf-8-sig'))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(_NUMERIC_TRIGGER_PREFIXES):
                        return True
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith(_NUMERIC_TRIGGER_PREFIXES):
                    return True
                for alias in node.names:
                    if alias.name in _NUMERIC_SYMBOL_NAMES:
                        return True
    return False


def _install_guard(allowed):
    def guard(event, args):
        if event == 'open':
            path, mode, flags = args
            if isinstance(path, int):
                raise PermissionError('strategy file descriptor access denied')
            resolved = Path(os.fsdecode(path)).resolve()
            if (not any(resolved.is_relative_to(root) for root in allowed)
                    or (isinstance(mode, str) and any(c in mode for c in 'wax+'))
                    or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
                raise PermissionError('strategy filesystem access denied')
        if (event.startswith(('socket.', 'subprocess.', 'ctypes.', 'winreg.'))
                or event in {'os.system', 'os.exec', 'os.posix_spawn', 'os.spawn', 'os.fork',
                             'os.remove', 'os.rename', 'os.mkdir', 'os.rmdir', 'os.putenv'}):
            raise PermissionError('strategy external IO denied')

    sys.addaudithook(guard)


def _check_dependencies(manifest):
    for requirement in manifest.get('dependencies', ()):
        name, version = requirement.split('==')
        if importlib.metadata.version(name) != version:
            raise ValueError('strategy dependency mismatch')


def _load_strategy_module(package: Path, manifest: dict):
    entry = package / manifest['file']
    module_spec = importlib.util.spec_from_file_location(
        'factorlab_selected_strategy',
        entry,
        submodule_search_locations=[str(entry.parent)] if manifest.get('package') else None,
    )
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        module_spec.loader.exec_module(module)
    return module


def _execute_request(module, manifest, request, *, protocol_types, validate_event):
    Decision, StrategySpec, DecisionV2, StrategySpecV2, decision_payload, spec_payload, json_value = protocol_types
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        strategy = getattr(module, manifest['class'])(**manifest['parameters'])
        spec = strategy.describe()
        if not isinstance(spec, (StrategySpec, StrategySpecV2)):
            raise ValueError('describe must return a versioned StrategySpec')
        if request['operation'] == 'describe':
            value = spec_payload(spec)
        elif request['operation'] == 'migrate':
            if not isinstance(spec, StrategySpecV2):
                raise ValueError('migration requires V2')
            value = strategy.migrate_state(request['state'], request['event']['from_version'])
            if not isinstance(value, dict):
                raise ValueError('migrated state must be a JSON object')
            value = json_value(value)
        elif request['operation'] == 'decide':
            v2 = isinstance(spec, StrategySpecV2)
            if v2:
                validate_event(request['event'], spec)
            decision = strategy.decide(request['event'], request['state'])
            if not isinstance(decision, DecisionV2 if v2 else Decision):
                raise ValueError('decide must return a matching versioned Decision')
            value = decision_payload(decision.validate(spec, None if v2 else request['event']['symbol']))
        else:
            raise ValueError('unknown strategy operation')
    return dict(value=value)


def _prepare_runtime(source_root: Path, package: Path, manifest: dict):
    import factorlab.strategies.adapters  # noqa: F401
    if _needs_numeric_preimport(package, manifest):
        try:
            import numpy  # noqa: F401
            import pandas as pd
            from zoneinfo import ZoneInfo
            for tz_name in ('UTC', 'America/New_York'):
                try:
                    ZoneInfo(tz_name)
                except Exception:
                    pass
            try:
                pd.Timestamp('2026-01-01', tz='America/New_York')
            except Exception:
                pass
        except ModuleNotFoundError:
            pass
    _check_dependencies(manifest)
    allowed = (package, source_root, Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve())
    _install_guard(allowed)


def _run_oneshot(source_root: Path, package: Path, *, decode, dumps, protocol_types, validate_event):
    request = decode(json.loads(sys.stdin.read(_MAX_MESSAGE_BYTES)))
    manifest = request['manifest']
    try:
        _prepare_runtime(source_root, package, manifest)
    except Exception as error:
        sys.stdout.write(dumps(dict(error=type(error).__name__ + ': ' + str(error))))
        return
    try:
        module = _load_strategy_module(package, manifest)
        response = _execute_request(
            module, manifest, request, protocol_types=protocol_types, validate_event=validate_event,
        )
    except Exception as error:
        response = dict(error=type(error).__name__ + ': ' + str(error))
    sys.stdout.write(dumps(response))


def _run_serve(source_root: Path, package: Path, *, decode, dumps, protocol_types, validate_event):
    out = sys.stdout
    raw_init = sys.stdin.readline(_MAX_MESSAGE_BYTES + 1)
    if not raw_init or len(raw_init) > _MAX_MESSAGE_BYTES:
        return
    init = decode(json.loads(raw_init))
    manifest = init['manifest']
    try:
        _prepare_runtime(source_root, package, manifest)
        module = _load_strategy_module(package, manifest)
    except Exception as error:
        out.write(dumps(dict(error=type(error).__name__ + ': ' + str(error))) + '\n')
        out.flush()
        return
    out.write(dumps(dict(ready=True)) + '\n')
    out.flush()

    while True:
        line = sys.stdin.readline(_MAX_MESSAGE_BYTES + 1)
        if not line:
            break
        if len(line) > _MAX_MESSAGE_BYTES:
            out.write(dumps(dict(error='ValueError: strategy request exceeds size limit')) + '\n')
            out.flush()
            break
        try:
            request = decode(json.loads(line))
            response = _execute_request(
                module, manifest, request, protocol_types=protocol_types, validate_event=validate_event,
            )
        except Exception as error:
            response = dict(error=type(error).__name__ + ': ' + str(error))
        encoded = dumps(response)
        if len(encoded) > _MAX_MESSAGE_BYTES:
            out.write(dumps(dict(error='ValueError: strategy response exceeds size limit')) + '\n')
            out.flush()
            break
        out.write(encoded + '\n')
        out.flush()


def main():
    serve = '--serve' in sys.argv[3:]
    source_root, package = (Path(p).resolve() for p in sys.argv[1:3])
    sys.path.insert(0, str(source_root))
    from factorlab.trading.storage import decode, dumps
    from factorlab.strategies.protocol import (
        Decision, StrategySpec, DecisionV2, StrategySpecV2,
        decision_payload, spec_payload, json_value,
    )
    from factorlab.strategies.events import validate_event
    protocol_types = (
        Decision, StrategySpec, DecisionV2, StrategySpecV2,
        decision_payload, spec_payload, json_value,
    )
    if serve:
        _run_serve(
            source_root, package, decode=decode, dumps=dumps,
            protocol_types=protocol_types, validate_event=validate_event,
        )
    else:
        _run_oneshot(
            source_root, package, decode=decode, dumps=dumps,
            protocol_types=protocol_types, validate_event=validate_event,
        )


if __name__ == '__main__':
    main()
