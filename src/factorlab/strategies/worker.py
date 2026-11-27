"""Private JSON stdin/stdout worker. Never started with account credentials."""
import contextlib
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import sys


def main():
    source_root, package = (Path(p).resolve() for p in sys.argv[1:])
    sys.path.insert(0, str(source_root))
    from factorlab.trading.storage import decode, dumps
    from factorlab.strategies.protocol import (Decision, StrategySpec, DecisionV2, StrategySpecV2,
                                               decision_payload, spec_payload, json_value)
    from factorlab.strategies.events import strategy_event, validate_event
    from factorlab.strategies.model import LinearModel
    request = decode(json.loads(sys.stdin.read(4 * 1024 * 1024)))
    manifest = request['manifest']
    # Preload the numerical libraries used by the built-in adapters on the
    # existing worker path. Lightweight packages use audited imports directly.
    if 'model' not in manifest and manifest.get('runtime_profile') != 'lightweight':
        import factorlab.strategies.adapters  # noqa: F401
        if manifest.get('calendar_timezone') == 'America/New_York':
            from pandas import Timestamp

            # Pandas resolves system timezone data on the first localization.
            # Initialize the calendar before restricting strategy file access.
            Timestamp(2000, 1, 1).tz_localize('America/New_York')
    allowed = (package, source_root, Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve())
    try:
        for requirement in manifest.get('dependencies', ()):
            name, version = requirement.split('==')
            if importlib.metadata.version(name) != version:
                raise ValueError('strategy dependency mismatch')
    except Exception as error:
        sys.stdout.write(dumps(dict(error=type(error).__name__ + ': ' + str(error))))
        return

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
    try:
        parameters = manifest['parameters']
        if 'model' in manifest:
            declaration = manifest['model']
            model = LinearModel.read(declaration, (package / declaration['file']).read_bytes())
            if request['operation'] == 'decide':
                model.require_event_time(request['event']['event_time'])
            parameters = dict(parameters, model=model)
        entry = package / manifest['file']
        # A package entrypoint supports ordinary relative imports.
        module_spec = importlib.util.spec_from_file_location('factorlab_selected_strategy', entry,
            submodule_search_locations=[str(entry.parent)] if manifest.get('package') else None)
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_spec.name] = module
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            module_spec.loader.exec_module(module)
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
        response = dict(value=value)
    except Exception as error:
        response = dict(error=type(error).__name__ + ': ' + str(error))
    sys.stdout.write(dumps(response))


if __name__ == '__main__':
    main()
