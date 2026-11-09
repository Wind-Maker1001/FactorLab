"""Merge-base risk policy and static impact analysis; run from the trusted base."""
import argparse
import ast
import fnmatch
import json
from pathlib import Path, PurePosixPath
import subprocess


FLAGS = ('current', 'locking', 'package', 'review')
CONTROL_ROOTS = ('.github/', 'tools/ci/', 'tests/security/', 'docs/ci/')
CONTROL_FILES = {'pytest.ini', 'tox.ini', '.gitattributes', '.gitignore',
                 'setup.cfg', 'conftest.py'}
PACKAGE_FILES = {'pyproject.toml', 'MANIFEST.in', 'setup.py'}
CORE_ROOTS = tuple('src/factorlab/' + p + '/' for p in
                   ('factors', 'models', 'metrics', 'validation', 'labels', 'evaluation',
                    'backtest', 'portfolio', 'cross_market'))
LOCK_ROOTS = ('src/factorlab/forward/', 'src/factorlab/backtest/',
              'src/factorlab/portfolio/', 'src/factorlab/execution/')
DOC_SUFFIXES = {'.md', '.rst', '.txt', '.json', '.csv', '.xml'}
DYNAMIC_CALLS = {'import_module', '__import__', 'spec_from_file_location',
                 'SourceFileLoader', 'exec', 'eval', 'run_path', 'run_module'}
# Reviewed scope of the synthetic demo's package-relative dynamic loader.
# Include dependencies reaching these targets, not every unrelated package file.
DYNAMIC_SCOPES = {'src/factorlab/demo.py': (
    'src/factorlab/factors/registry.py', 'src/factorlab/dataset/builder.py',
    'src/factorlab/universe/filters.py', 'src/factorlab/validation/',
    'src/factorlab/backtest/', 'src/factorlab/metrics/',
    'src/factorlab/reports/', 'src/factorlab/_identity.py')}


def valid_test_path(path):
    p = PurePosixPath(path)
    return (path == p.as_posix() and path.startswith('tests/') and
            p.name.startswith('test_') and p.suffix == '.py' and
            all(part not in ('', '.', '..') for part in path.split('/')) and
            not any(c in path for c in '\\:\r\n\x00*?[]'))


def classify(paths):
    result = dict.fromkeys(FLAGS, False)
    reasons = {flag: [] for flag in FLAGS}
    def require(path, rule, flags):
        for flag in flags:
            result[flag] = True
            reasons[flag].append(dict(path=path, rule=rule))
    for path in paths:
        p = PurePosixPath(path)
        if (path != p.as_posix() or p.is_absolute() or '..' in p.parts or
                any(c in path for c in '\\:\r\n\x00') or
                any(part.endswith((' ', '.')) for part in p.parts)):
            require(path, 'noncanonical-path', FLAGS)
        elif (path.startswith(CONTROL_ROOTS) or path in CONTROL_FILES or path in DYNAMIC_SCOPES or
                p.name == 'conftest.py' or p.name == '__init__.py' or
                p.name in ('sitecustomize.py', 'usercustomize.py')):
            require(path, 'control-or-discovery', FLAGS)
        elif path.startswith('requirements/') or path in PACKAGE_FILES:
            require(path, 'dependencies-or-packaging', ('current', 'package'))
            if path in PACKAGE_FILES:
                require(path, 'entry-or-build-configuration', ('review',))
        elif path.startswith(LOCK_ROOTS):
            require(path, 'execution-or-shared-state', ('current', 'locking'))
        elif path.startswith(CORE_ROOTS) and p.suffix == '.py':
            pass
        elif valid_test_path(path) or (path.startswith('tools/data/') and p.suffix == '.py'):
            pass  # Candidates only: select() requires impact and executable coverage evidence.
        elif ((path.startswith(('docs/user/', 'docs/validation/')) and p.suffix in DOC_SUFFIXES)
              or path in ('README.md', 'LICENSE', 'NOTICE', 'CONTRIBUTING.md', 'SECURITY.md')):
            pass
        else:
            require(path, 'sensitive-or-unclassified', ('current', 'locking'))
    return dict(result, reasons=reasons, files=sorted(paths))


def git(repository, *args):
    return subprocess.check_output(['git', *args], cwd=repository)


def tree(repository, revision):
    entries = {}
    for record in git(repository, 'ls-tree', '-r', '-z', revision).split(b'\0'):
        if record:
            meta, name = record.split(b'\t', 1)
            mode, kind, oid = meta.decode().split()
            entries[name.decode('utf-8')] = (mode, kind, oid)
    return entries


def contents(repository, entries):
    """Read tracked text blobs in one Git process, never following symlinks."""
    selected = {p: e for p, e in entries.items() if e[0] in ('100644', '100755') and
                (PurePosixPath(p).suffix in {'.py', '.toml', '.yaml', '.yml', '.json', '.ini', '.cfg',
                                            '.sh', '.bash', '.ps1', '.bat', '.cmd', '.js', '.ts'}
                 or PurePosixPath(p).name in ('MANIFEST.in', 'Makefile', 'Dockerfile'))}
    proc = subprocess.run(['git', 'cat-file', '--batch'], cwd=repository,
                          input=''.join(e[2] + '\n' for e in selected.values()).encode(),
                          stdout=subprocess.PIPE, check=True)
    data, offset, result = proc.stdout, 0, {}
    for path in selected:
        end = data.index(b'\n', offset)
        size = int(data[offset:end].split()[-1])
        offset = end + 1
        result[path] = data[offset:offset + size].decode('utf-8-sig')
        offset += size + 1
    return result


def output_path_literals(parsed):
    """Exclude only paths proven to be receivers of pathlib write-only operations."""
    constructors = {alias.asname or alias.name for node in parsed.body
                    if isinstance(node, ast.ImportFrom) and node.module == 'pathlib'
                    for alias in node.names if alias.name == 'Path'}
    bindings = []
    for node in ast.walk(parsed):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bindings.append(node.id)
        elif isinstance(node, ast.arg):
            bindings.append(node.arg)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            bindings.append(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bindings.append(node.rest)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings.append(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bindings.extend(alias.asname or alias.name.split('.')[0] for alias in node.names)
    parents = {child: node for node in ast.walk(parsed) for child in ast.iter_child_nodes(node)}
    constructors = {name for name in constructors if bindings.count(name) == 1 and
                    '*' not in bindings and all(
                        isinstance(parents.get(node), ast.Call) and parents[node].func is node
                        for node in ast.walk(parsed) if isinstance(node, ast.Name) and node.id == name)}

    # TODO: Prove helper-parameter and assigned-path provenance before excluding
    # their output receivers; a write-like method on an unknown object may read.
    def path_expression(node):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in constructors):
            return (not node.keywords and all(isinstance(argument, ast.Name) or
                    (isinstance(argument, ast.Constant) and isinstance(argument.value, str))
                    for argument in node.args))
        return (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)
                and path_expression(node.left))

    ignored = set()
    for node in ast.walk(parsed):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ('write_text', 'write_bytes')
                and path_expression(node.func.value)):
            ignored.update(child for child in ast.walk(node.func.value)
                           if isinstance(child, ast.Constant) and isinstance(child.value, str))
    return ignored


def graph(texts, names):
    modules = {}
    for path in texts:
        if path.endswith('.py'):
            module = path.removeprefix('src/').removesuffix('.py').replace('/', '.')
            module = module.removesuffix('.__init__')
            modules.setdefault(module, set()).add(path)
            if path.startswith('tools/data/'):
                modules.setdefault(PurePosixPath(path).stem, set()).add(path)
    consumers, uncertain = {}, {}
    def edge(dependency, consumer):
        consumers.setdefault(dependency, set()).add(consumer)
    for path, text in texts.items():
        literals = []
        if path.endswith('.py'):
            parsed = ast.parse(text, filename=path)
            outputs = output_path_literals(parsed)
            package = path.removeprefix('src/').removesuffix('.py').replace('/', '.')
            if not path.endswith('/__init__.py'):
                package = package.rpartition('.')[0]
            imports = []
            dynamic_aliases = {alias.asname: alias.name for node in ast.walk(parsed)
                               if isinstance(node, ast.ImportFrom) and node.module in ('importlib', 'importlib.util', 'runpy', 'builtins')
                               for alias in node.names if alias.asname and alias.name in DYNAMIC_CALLS}
            for node in ast.walk(parsed):
                if path.startswith('tests/') and (
                    (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('pytest_')) or
                    (isinstance(node, ast.Name) and node.id == 'pytest_plugins')):
                    uncertain.setdefault(path, []).append('test-control-hook')
                if isinstance(node, ast.Import):
                    imports.extend(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    prefix = '.'.join(package.split('.')[:len(package.split('.')) - node.level + 1]) if node.level else ''
                    base = '.'.join(p for p in (prefix, node.module) if p)
                    imports.extend([base, *(base + '.' + a.name for a in node.names)])
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in outputs:
                    literals.append(node.value)
                elif isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ''
                    name = dynamic_aliases.get(name, name)
                    if name in DYNAMIC_CALLS:
                        args = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                        if name in ('import_module', '__import__') and args:
                            imports.append(args[0])
                        else:
                            uncertain.setdefault(path, []).append(name)
            for module in imports:
                parts = module.split('.')
                for length in range(1, len(parts) + 1):
                    for source in modules.get('.'.join(parts[:length]), ()):
                        edge(source, path)
        else:
            # Evidence inventories describe files; changing a described file
            # does not change that inventory. Runtime consumers of the evidence
            # itself are still discovered from their own source/configuration.
            literals = [] if path.startswith('docs/') else [text]
        # TODO: Resolve basename-only input provenance and path-comparison literals;
        # unknown references still conservatively connect every same-named file.
        # Computed references are never resolved by executing code.
        for dependency in names:
            basename = PurePosixPath(dependency).name
            module = dependency.removeprefix('src/').removesuffix('.py').replace('/', '.')
            for value in literals:
                if (dependency in value or
                    (dependency.endswith('.py') and module in value) or
                    (basename == value and len(basename) > 3) or
                    ('/' in value and not value.startswith(('*', '?')) and
                     ('*' in value or '?' in value) and fnmatch.fnmatchcase(dependency, value))):
                    edge(dependency, path)
                    break
    return consumers, uncertain


def closure(paths, consumers):
    affected, pending = set(paths), list(paths)
    while pending:
        for consumer in consumers.get(pending.pop(), ()):
            if consumer not in affected:
                affected.add(consumer)
                pending.append(consumer)
    return affected


def affected_consumers(repository, paths):
    texts = {p.relative_to(repository).as_posix(): p.read_text(encoding='utf-8-sig')
             for p in (repository / 'src/factorlab').rglob('*.py')}
    consumers, _ = graph(texts, texts)
    return sorted(closure(paths, consumers))


def select(repository, base, head):
    base = git(repository, 'merge-base', base, head).decode().strip()
    head = git(repository, 'rev-parse', head + '^{commit}').decode().strip()
    before, after = tree(repository, base), tree(repository, head)
    paths = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    graphs = [graph(contents(repository, entries), entries) for entries in (before, after)]
    affected = set(paths)
    for consumers, _ in graphs:
        affected.update(closure(paths, consumers))
    result = classify(paths)
    def require(path, rule, flags=('current', 'locking')):
        for flag in flags:
            result[flag] = True
            result['reasons'][flag].append(dict(path=path, rule=rule))
    targets = {p for p in affected if valid_test_path(p) and
               p in after and after[p][0] == '100644'}
    for consumers, uncertain in graphs:
        for loader in uncertain:
            users = closure([loader], consumers)
            if (loader.startswith(('src/', 'tools/data/')) and
                    any(user.startswith(('src/', '.github/', 'tools/ci/')) for user in users)):
                scope = DYNAMIC_SCOPES.get(loader)
                if scope is None or any(path.startswith(scope) for path in affected):
                    require(loader, 'unresolved-formal-loader')
    for path in paths:
        old, new = before.get(path), after.get(path)
        if (new and new[0] != '100644') or (old and old[0] != '100644') or (old and not new):
            require(path, 'deletion-or-nonregular-mode')
        if new and valid_test_path(path) and new[0] == '100644':
            targets.add(path)
        if path.endswith('.py') and not path.startswith(CONTROL_ROOTS):
            users = set()
            for consumers, uncertain in graphs:
                impacted = closure([path], consumers)
                users.update(impacted)
                for user in impacted:
                    if user in uncertain:
                        require(user, 'unresolved-dynamic-loading',
                                FLAGS if 'test-control-hook' in uncertain[user] else ('current', 'locking'))
            tests = {p for p in users if valid_test_path(p) and p in after and after[p][0] == '100644'}
            targets.update(tests)
            if not tests and not valid_test_path(path):
                require(path, 'no-proven-test-consumer')
        # A changed input can be consumed by formal/configuration/control code.
        for consumers, _ in graphs:
            for consumer in closure([path], consumers) - {path}:
                if (consumer.startswith(('tools/data/', 'examples/research/')) or
                        (consumer.startswith('tests/') and not consumer.startswith('tests/security/')
                         and PurePosixPath(consumer).name != 'conftest.py')):
                    continue
                risk = classify([consumer])
                for flag in FLAGS:
                    if risk[flag]:
                        require(consumer, 'affected-consumer:' + path, (flag,))
    # An added module may satisfy previously computed loader targets. Only the
    # research namespace has a bounded static local-import policy; new package
    # modules and package initialization remain conservative.
    for path in paths:
        if path not in before and path.startswith('src/'):
            require(path, 'new-package-module')
    if result['current']:
        # Extended already covers affected nonhistorical modules; still require
        # explicit execution evidence for every changed test file.
        targets.intersection_update(paths)
    decisions = []
    for path in paths:
        users = set().union(*(closure([path], consumers) for consumers, _ in graphs))
        decisions.append(dict(path=path, before=before.get(path), after=after.get(path),
                              consumers=sorted(users - {path}),
                              tests=sorted(users & targets),
                              rules=sorted({reason['rule'] for reasons in result['reasons'].values()
                                            for reason in reasons if reason['path'] == path}) or
                                    ['covered-code-or-isolated-research-or-inert-evidence']))
    result.update(schema='FACTORLAB_PR_RISK_V2', base=base, head=head,
                  affected=sorted(affected), targeted_tests=sorted(targets), decisions=decisions)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args()
    result = select(args.repository, args.base, args.head)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    if args.github_output:
        with args.github_output.open('a', encoding='utf-8') as stream:
            for key in FLAGS:
                stream.write(f'{key}={str(result[key]).lower()}\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
