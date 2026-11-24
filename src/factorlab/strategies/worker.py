"""Private JSON stdin/stdout worker. Never started with account credentials."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys


def main():
    serve = len(sys.argv) >= 2 and sys.argv[1] == '--serve'
    argv_paths = sys.argv[2:] if serve else sys.argv[1:]
    source_root, package = (Path(p).resolve() for p in argv_paths)
    sys.path.insert(0, str(source_root))
    from factorlab.trading.storage import decode, dumps
    from factorlab.strategies.protocol import Decision, StrategySpec, decision_payload, spec_payload
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
    loaded_module = None
    loaded_entry = None

    def handle(raw_text):
        nonlocal loaded_module, loaded_entry
        try:
            request = decode(json.loads(raw_text))
            manifest = request['manifest']
            entry = package / manifest['file']
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                if loaded_module is None or loaded_entry != entry:
                    # A package entrypoint supports ordinary relative imports.
                    module_spec = importlib.util.spec_from_file_location('factorlab_selected_strategy', entry,
                        submodule_search_locations=[str(entry.parent)] if manifest.get('package') else None)
                    module = importlib.util.module_from_spec(module_spec)
                    sys.modules[module_spec.name] = module
                    module_spec.loader.exec_module(module)
                    loaded_module = module
                    loaded_entry = entry
                strategy = getattr(loaded_module, manifest['class'])(**manifest['parameters'])
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
            return dict(value=value)
        except Exception as error:
            return dict(error=type(error).__name__ + ': ' + str(error))

    if not serve:
        raw = sys.stdin.read(4 * 1024 * 1024)
        sys.stdout.write(dumps(handle(raw)))
        return

    max_frame = 4 * 1024 * 1024
    while True:
        line = sys.stdin.readline(max_frame + 2)
        if not line:
            break
        if len(line) > max_frame + 1:
            sys.stdout.write(dumps(dict(error='ValueError: strategy request exceeds 4 MiB limit')) + '\n')
            sys.stdout.flush()
            break
        sys.stdout.write(dumps(handle(line)) + '\n')
        sys.stdout.flush()


if __name__ == '__main__':
    main()
