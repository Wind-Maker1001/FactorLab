"""Fail-closed same-package executor admission under the market owner lock."""
import argparse
from contextlib import closing
from copy import copy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from .deployment import DeploymentStore
from .identity import content_identity
from factorlab.trading.storage import decode, dumps


# Execute this identical probe with both interpreters; the old release need not
# contain this module. No credentials, live data, or strategy decisions are used.
_PROBE = r'''
import ast
from hashlib import sha256, file_digest
from importlib.metadata import distributions
from importlib.util import resolve_name
import json
from pathlib import Path
import platform
import os
import sys
import sysconfig
from factorlab.strategies.loader import LoadedStrategy, SOURCE_ROOT
from factorlab.trading.storage import dumps

root = SOURCE_ROOT / 'factorlab'
package = Path(sys.argv[1])
strategy = LoadedStrategy.from_strategy_package(package, trusted=True)
modules = {}
for path in root.rglob('*.py'):
    relative = path.relative_to(SOURCE_ROOT).with_suffix('')
    parts = list(relative.parts)
    if parts[-1] == '__init__':
        parts.pop()
    modules['.'.join(parts)] = path
sources = {}
visited = set()
framework = {'factorlab.strategies.loader', 'factorlab.strategies.worker',
             'factorlab.strategies.package'}

def ast_identity(tree):
    # ast.dump presentation and optional fields change between Python releases.
    # Encode populated semantic fields ourselves; locations and empty optional
    # fields carry no executable meaning. New populated fields remain identity.
    def canonical(value):
        if isinstance(value, ast.AST):
            return [type(value).__name__, [[name, canonical(item)]
                for name, item in sorted(ast.iter_fields(value))
                if item is not None and item != []]]
        if isinstance(value, list):
            return [canonical(item) for item in value]
        return [type(value).__name__, repr(value)]
    return sha256(json.dumps(canonical(tree), separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def visit(name, path=None, external=False):
    if name in visited:
        return
    visited.add(name)
    path = path or modules[name]
    tree = ast.parse(path.read_bytes(), filename=str(path))
    sources[name] = ast_identity(tree)
    base = name if path.name == '__init__.py' else name.rpartition('.')[0]
    for node in ast.walk(tree):
        if name not in framework:
            if isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id in
                    {'__import__', 'eval', 'exec', 'compile'}
                or isinstance(node.func, ast.Attribute) and node.func.attr in
                    {'import_module', 'exec_module', 'load_module', 'spec_from_file_location'}):
                raise ValueError('unprovable dynamic code loading in ' + name)
            if external:
                reflective = {'importlib', 'builtins', 'inspect', 'runpy', 'types'}
                if (isinstance(node, ast.Import) and any(
                        alias.name.split('.')[0] in reflective for alias in node.names)
                        or isinstance(node, ast.ImportFrom) and node.module
                        and node.module.split('.')[0] in reflective):
                    raise ValueError('unprovable reflective imports in ' + name)
                if isinstance(node, ast.Name) and node.id in {'__builtins__', '__import__', 'eval', 'exec', 'compile'}:
                    raise ValueError('unprovable dynamic code loading in ' + name)
        targets = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            target = resolve_name('.' * node.level + (node.module or ''), base) if node.level else node.module
            targets = [target] + [target + '.' + alias.name for alias in node.names]
        for target in targets:
            if target in modules:
                # Importing a submodule executes each parent __init__ too.
                parts = target.split('.')
                for i in range(1, len(parts) + 1):
                    parent = '.'.join(parts[:i])
                    if parent in modules:
                        visit(parent)
            elif target and target.startswith('factorlab.'):
                # A from-import may name a symbol rather than a module.
                if target.rpartition('.')[0] not in modules:
                    raise ValueError('unresolved runtime import ' + target)

for path in sorted(package.rglob('*.py')):
    relative = path.relative_to(package).with_suffix('')
    parts = list(relative.parts)
    if parts[-1] == '__init__':
        parts.pop()
    visit('factorlab_selected_strategy' + ('.' + '.'.join(parts) if parts else ''), path, True)
for name in ('factorlab.strategies.worker', 'factorlab.strategies.loader',
             'factorlab.strategies.decision_runtime', 'factorlab.strategies.model'):
    visit(name)

# Host input construction, execution, capital and checkpoint paths are frozen
# independently of the package import closure. Host lifecycle/retention modules
# are not compute roots, but a strategy importing them makes them part of its
# transitive closure above and therefore freezes them too.

# Each exception names exact, audited whole-module contents, not a symbol whose
# body could subsequently change. Imports from strategy compute never qualify.
audited = json.loads(sys.argv[2])
for name in sorted(set(modules) | set(audited)):
    if name in visited:
        continue
    # Offline research is not an executor root. An explicit package import
    # already visited it above and consequently freezes its entire closure.
    if name.startswith(('factorlab.research.', 'factorlab.experiments.')):
        continue
    path = modules.get(name)
    digest = None
    if path is not None:
        tree = ast.parse(path.read_bytes())
        if name == 'factorlab.strategies.runtime_migration':
            # The registry is data supplied identically to both probes. Excluding
            # only that assignment avoids a self-referential policy checksum.
            tree.body = [node for node in tree.body if not (
                isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                    and target.id == '_AUDITED' for target in node.targets))]
        digest = ast_identity(tree)
    if name in audited:
        if digest not in audited[name]:
            raise ValueError('unsupported audited runtime contents: ' + name)
        sources[name] = 'audited:47e58d9-p10-13'
    elif digest is not None:
        sources[name] = digest

# Freeze interpreter, stdlib, extension modules and installed distribution bytes,
# not just version strings (including numerical wheels and frozen-model engines).
def file_identity(path):
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def dependency_identity(path):
    # Only interpreter selection in installed scripts is relocatable. Never
    # resolve interpreter symlinks: another venv may share the same base binary.
    path = Path(path)
    if path.parent.resolve() != Path(sysconfig.get_path('scripts')).resolve():
        return file_identity(path)
    with path.open('rb') as stream:
        first = stream.readline()
        if not first.startswith(b'#!'):
            return file_identity(path)
        target = Path(os.fsdecode(first[2:].rstrip(b'\r\n')))
        interpreter = Path(sys.executable)
        if target.parent.resolve() != interpreter.parent.resolve() or not target.samefile(interpreter):
            raise ValueError('unproved Python entrypoint interpreter: ' + str(path))
        prefix = b'#!<verified-python>\n'
        digest = sha256(prefix)
        while block := stream.read(1024 * 1024):
            digest.update(block)
        return digest.hexdigest()


def digest_files(paths, base):
    result = {}
    for path in sorted(set(paths)):
        if path.is_file() and path.suffix != '.pyc' and '__pycache__' not in path.parts:
            result[str(path.relative_to(base))] = file_identity(path)
    return result
stdlib = Path(sysconfig.get_path('stdlib')).resolve()
stdlib_paths = [p for p in stdlib.rglob('*')
    if not {'site-packages', 'dist-packages', '__pycache__'} & set(p.relative_to(stdlib).parts)]
dependencies = {}
for dist in distributions():
    name = dist.metadata['Name'].lower().replace('_', '-')
    if name == 'factorlab':
        continue
    files = dist.files
    if files is None:
        raise ValueError('dependency has no installed file inventory: ' + name)
    dependencies[name] = dict(version=dist.version, files={str(p): dependency_identity(dist.locate_file(p))
        for p in files if not str(p).endswith('.pyc') and '__pycache__' not in str(p)
        and Path(p).name not in {'RECORD', 'direct_url.json', 'INSTALLER', 'REQUESTED'}
        and dist.locate_file(p).is_file()})
print(dumps(dict(binding=strategy.binding, manifest=strategy.package_manifest,
    sources=sources, dependencies=dependencies,
    interpreter=dict(version=sys.version, implementation=platform.python_implementation(),
        machine=platform.machine(), byteorder=sys.byteorder,
        executable=file_identity(Path(sys.executable).resolve()),
        stdlib=digest_files(stdlib_paths, stdlib)))))
strategy.close()
'''


_AUDITED = {'factorlab.integrations.alpaca.cloud_runtime': ['3c0c73e7985841998ca310737385f5c85a125fa0afe4c37ec1de9ab7cdcf7a7d', '0483f9c54db7e2e2cb05eb1b0c12518548580b8d57418b917506270f5fbabd1b'], 'factorlab.integrations.binance_futures_trading.demo_cloud_storage': ['e938109811c51c2e5f215f54530feaf0a42fe38cb9a5ce880284b1d38ea19b5e', '5241e5aee4a08254bb09740ebc1a744e1455e7e34d268bdd0d5b027cb2f1cf03', '27cbeecfd27a1c447c9c0cca4810fc53ef32312820328db346a6c33421703595'], 'factorlab.integrations.binance_futures_trading.demo_cloud_backup': ['fb4039dcd58bf95d1e4481e9ce1f81c72b88a6b0380512d9750626363385943b', '20a7c27dc5744af1eadf44f56aafe64834f5500ae6087cf57f93e04c2b45d10d', '0992b8d75e099b590c93378b70ef77b5eec12cddbceced9701ce65bcb513f58d'], 'factorlab.integrations.disaster_recovery': ['cb7800a105c2ef9ea4d31d070a2d123ffee31fba6cbb7437ce0851954536fd6c', '2b2a85d3ccbe9fe6bce604cb070460bd3a8193e67cecd1c023e9787182c0f4e1', '6d4276304969c4b16d889c9479620f5b6f0a0bc95c228d9e101b0dc1dbf1dba4'], 'factorlab.strategies.runtime_migration': [None, '9a69fe5ee9cf96210b5eaeb8823963d37918597cd9ab79f195c0daf4ca3bd008', '4cf117b1d2ea5fbfb3d79cb4681a645ecf0483ca9dc417594b3f4393da4f66d6', '10548114fee64fc676e68c016b67f87b87cbb869c0a69526ba2296c0b66800e2']}


def probe(python, package):
    output = subprocess.check_output(
        [str(python), '-I', '-c', _PROBE, str(package), json.dumps(_AUDITED)],
        text=True, timeout=300)
    return decode(json.loads(output))


def require_compatible(previous, replacement, manifest):
    for result in (previous, replacement):
        if result['manifest'] != manifest:
            raise ValueError('runtime probe differs from installed package')
    binding = previous['binding']
    if dict(replacement['binding'], runtime_sha256=binding['runtime_sha256']) != binding:
        raise ValueError('runtime upgrade changes package/configuration/model identity')
    for key in ('sources', 'dependencies', 'interpreter'):
        if previous[key] != replacement[key]:
            raise ValueError('unproved runtime compatibility: ' + key)


def require_drained(store, package_id, events):
    """Committed receipts can replay; uncommitted frozen inputs belong to the old runtime."""
    for event in events:
        if event.get('binding') != store.binding():
            continue
        if store.decision(package_id, event['event_id'], content_identity(event)) is None:
            raise ValueError('previous runtime must drain frozen event ' + event['event_id'])


def _events(root, market, store, package_id):
    if market == 'alpaca':
        binding = store.binding()
        binding_key = [[field, binding[field]] for field in sorted(binding)]
        for path in sorted((root / 'events').glob('*.json')):
            event = decode(json.loads(path.read_bytes()))
            key = sha256(dumps([binding_key, package_id, event['event_id']]).encode()).hexdigest()
            if path.stem == key:
                yield event
        if store.instance_id == 'V69_CAUSAL':
            for path in sorted((root / 'signals').glob('*.native.json')):
                yield decode(json.loads(path.read_bytes()))
    else:
        path = root / 'journal' / 'controller.sqlite3'
        if path.is_file():
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='demo_instance_inputs'").fetchone():
                    for payload, in db.execute('SELECT payload FROM demo_instance_inputs '
                            'WHERE instance_id=? AND package_id=?', (store.instance_id, package_id)):
                        yield decode(json.loads(payload))


def migrate(root, previous_python, *, market, releases=None):
    root = Path(root).resolve(strict=True)
    if market == 'alpaca':
        from factorlab.integrations.alpaca.cloud_runtime import owner_lock, private_root
        root = private_root(root)
        lock = owner_lock(root)
        database = root / 'deployments.sqlite3'
    elif market == 'binance':
        from factorlab.integrations.binance_futures_trading.demo_runtime import operator_lock
        lock = operator_lock(root)
        database = root / 'strategy-deployment.sqlite3'
    else:
        raise ValueError('unknown market')
    releases = Path(releases) if releases is not None else root / 'strategy-releases'
    with lock:
        if not database.is_file():
            return []
        with closing(DeploymentStore(database)) as registry:
            prepared = []
            for owner, in registry.db.execute('SELECT instance_id FROM strategy_registry WHERE binding IS NOT NULL'):
                view = copy(registry)
                view.instance_id = owner
                package_id = view.snapshot()['active']
                if package_id is None:
                    continue
                if view.pending() is not None or view.activation_callback() is not None:
                    raise ValueError('runtime upgrade requires stable instance ' + owner)
                require_drained(view, package_id, _events(root, market, view, package_id))
                manifest = view.manifest(package_id)
                package = releases / package_id
                previous = probe(previous_python, package)
                replacement = probe(sys.executable, package)
                require_compatible(previous, replacement, manifest)
                prepared.append((view, package_id, previous['binding'], replacement['binding']))
            # Verify all instances before admission. Each additive, idempotent
            # receipt retains the original binding, including on interruption.
            result = []
            for view, package_id, previous, replacement in prepared:
                created = view.admit_runtime_upgrade(package_id, previous, replacement)
                result.append(dict(instance_id=view.instance_id, package_id=package_id,
                    previous_runtime_sha256=previous['runtime_sha256'],
                    runtime_sha256=replacement['runtime_sha256'], admission_created=created))
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', required=True, type=Path)
    parser.add_argument('--previous-python', required=True, type=Path)
    parser.add_argument('--market', required=True, choices=('alpaca', 'binance'))
    parser.add_argument('--releases', type=Path)
    args = parser.parse_args()
    print(json.dumps(migrate(args.state_root, args.previous_python,
        market=args.market, releases=args.releases), sort_keys=True))


if __name__ == '__main__':
    main()
