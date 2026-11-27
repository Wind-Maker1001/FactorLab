"""Private one-shot JSON stdin/stdout worker. Never started with account credentials."""
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


def _warm_timezones(manifest: dict, *, pandas_module=None):
    from zoneinfo import ZoneInfo

    tz_names = ['UTC', 'America/New_York']
    calendar_tz = manifest.get('calendar_timezone')
    if isinstance(calendar_tz, str) and calendar_tz and calendar_tz not in tz_names:
        tz_names.append(calendar_tz)
    for tz_name in tz_names:
        try:
            ZoneInfo(tz_name)
        except Exception:
            pass
    if pandas_module is not None:
        for tz_name in tz_names:
            if tz_name == 'UTC':
                continue
            try:
                pandas_module.Timestamp(2000, 1, 1).tz_localize(tz_name)
            except Exception:
                pass


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


def _prepare_runtime(source_root: Path, package: Path, manifest: dict):
    # Preload the numerical libraries only for non-lightweight packages that
    # require them (or declare an America/New_York exchange calendar).
    # P10-11 lightweight and frozen-model packages use audited imports directly.
    lightweight = 'model' in manifest or manifest.get('runtime_profile') == 'lightweight'
    if not lightweight:
        import factorlab.strategies.adapters  # noqa: F401
        if _needs_numeric_preimport(package, manifest) or manifest.get('calendar_timezone') == 'America/New_York':
            try:
                import numpy  # noqa: F401
                import pandas as pd
                _warm_timezones(manifest, pandas_module=pd)
            except ModuleNotFoundError:
                _warm_timezones(manifest, pandas_module=None)
        else:
            _warm_timezones(manifest, pandas_module=None)
    else:
        _warm_timezones(manifest, pandas_module=None)
    _check_dependencies(manifest)
    allowed = (package, source_root, Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve())
    _install_guard(allowed)


def _load_strategy_module(package: Path, manifest: dict):
    entry = package / manifest['file']
    # A package entrypoint supports ordinary relative imports.
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


def _execute_request(
    module, package: Path, manifest: dict, request: dict, *,
    protocol_types, validate_event, strategy_event, LinearModel,
):
    Decision, StrategySpec, DecisionV2, StrategySpecV2, decision_payload, spec_payload, json_value = protocol_types
    parameters = manifest['parameters']
    if 'model' in manifest:
        declaration = manifest['model']
        model = LinearModel.read(declaration, (package / declaration['file']).read_bytes())
        if request['operation'] == 'decide':
            model.require_event_time(request['event']['event_time'])
        parameters = dict(parameters, model=model)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        strategy = getattr(module, manifest['class'])(**parameters)
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
            event = strategy_event(request['event'], spec) if v2 else request['event']
            decision = strategy.decide(event, request['state'])
            if not isinstance(decision, DecisionV2 if v2 else Decision):
                raise ValueError('decide must return a matching versioned Decision')
            value = decision_payload(decision.validate(spec, None if v2 else request['event']['symbol']))
        else:
            raise ValueError('unknown strategy operation')
    return dict(value=value)


def main():
    source_root, package = (Path(p).resolve() for p in sys.argv[1:3])
    sys.path.insert(0, str(source_root))
    from factorlab.trading.storage import decode, dumps
    from factorlab.strategies.protocol import (
        Decision, StrategySpec, DecisionV2, StrategySpecV2,
        decision_payload, spec_payload, json_value,
    )
    from factorlab.strategies.events import strategy_event, validate_event
    from factorlab.strategies.model import LinearModel
    protocol_types = (
        Decision, StrategySpec, DecisionV2, StrategySpecV2,
        decision_payload, spec_payload, json_value,
    )

    raw_request = sys.stdin.read(_MAX_MESSAGE_BYTES + 1)
    if len(raw_request) > _MAX_MESSAGE_BYTES:
        sys.stdout.write(dumps(dict(error='ValueError: strategy request exceeds size limit')))
        return
    try:
        request = decode(json.loads(raw_request))
        manifest = request['manifest']
        _prepare_runtime(source_root, package, manifest)
    except Exception as error:
        sys.stdout.write(dumps(dict(error=type(error).__name__ + ': ' + str(error))))
        return
    try:
        module = _load_strategy_module(package, manifest)
        response = _execute_request(
            module, package, manifest, request,
            protocol_types=protocol_types,
            validate_event=validate_event,
            strategy_event=strategy_event,
            LinearModel=LinearModel,
        )
    except Exception as error:
        response = dict(error=type(error).__name__ + ': ' + str(error))
    encoded = dumps(response)
    if len(encoded) > _MAX_MESSAGE_BYTES:
        sys.stdout.write(dumps(dict(error='ValueError: strategy response exceeds size limit')))
        return
    sys.stdout.write(encoded)


if __name__ == '__main__':
    main()
