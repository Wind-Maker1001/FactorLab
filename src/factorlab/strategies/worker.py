"""Private JSON stdin/stdout worker. Never started with account credentials."""
import contextlib
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
    from factorlab.strategies.protocol import Decision, StrategySpec, decision_payload, spec_payload
    request = decode(json.loads(sys.stdin.read(4 * 1024 * 1024)))
    # Import library dependencies before restricting IO. Trusted strategy code
    # still runs with a guard against accidental file/network/process access.
    import factorlab.strategies.adapters  # noqa: F401
    allowed = (package, source_root, Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve())

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
        manifest = request['manifest']
        entry = package / manifest['file']
        # A package entrypoint supports ordinary relative imports.
        module_spec = importlib.util.spec_from_file_location('factorlab_selected_strategy', entry,
            submodule_search_locations=[str(entry.parent)] if manifest.get('package') else None)
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_spec.name] = module
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            module_spec.loader.exec_module(module)
            strategy = getattr(module, manifest['class'])(**manifest['parameters'])
            spec = strategy.describe()
            if not isinstance(spec, StrategySpec):
                raise ValueError('describe must return StrategySpec')
            if request['operation'] == 'describe':
                value = spec_payload(spec)
            else:
                decision = strategy.decide(request['event'], request['state'])
                if not isinstance(decision, Decision):
                    raise ValueError('decide must return Decision')
                value = decision_payload(decision.validate(spec, request['event']['symbol']))
        response = dict(value=value)
    except Exception as error:
        response = dict(error=type(error).__name__ + ': ' + str(error))
    sys.stdout.write(dumps(response))


if __name__ == '__main__':
    main()
