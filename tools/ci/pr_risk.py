"""Conservative merge-base risk selection, executed from the trusted base checkout."""
import argparse
import ast
import json
from pathlib import Path
import subprocess


SAFE_ROOTS = ('docs/user/',)
CONTROL_ROOTS = ('.github/', 'tools/', 'tests/', 'requirements/', 'docs/ci/', 'docs/validation/')
CORE_ROOTS = ('src/factorlab/factors/', 'src/factorlab/models/',
              'src/factorlab/metrics/', 'src/factorlab/validation/',
              'src/factorlab/labels/', 'src/factorlab/evaluation/',
              'src/factorlab/backtest/', 'src/factorlab/portfolio/',
              'src/factorlab/cross_market/')


def classify(paths):
    # Only explicitly covered modules can avoid exhaustive software validation.
    control = any(p.startswith(CONTROL_ROOTS) or p in (
        'pyproject.toml', 'MANIFEST.in', 'setup.cfg', 'setup.py', 'pytest.ini',
        'conftest.py', 'tox.ini', '.gitattributes', '.gitignore') or
        p.endswith('/conftest.py') for p in paths)
    unknown = any(not (p.startswith(CORE_ROOTS + SAFE_ROOTS) or p in (
        'README.md', 'LICENSE', 'NOTICE', 'CONTRIBUTING.md', 'SECURITY.md')) for p in paths)
    return dict(current=unknown, locking=unknown, package=control,
                review=control, files=paths)


def affected_consumers(repository, paths):
    """Follow import consumers, not just changed filenames, without importing PR code."""
    sources = {p.relative_to(repository).as_posix(): p
               for p in (repository / 'src/factorlab').rglob('*.py')}
    modules = {name: name.removeprefix('src/').removesuffix('.py').replace('/', '.')
               .removesuffix('.__init__') for name in sources}
    consumers = {}
    for name, source in sources.items():
        package = modules[name] if source.name == '__init__.py' else modules[name].rpartition('.')[0]
        for node in ast.walk(ast.parse(source.read_text(encoding='utf-8-sig'), filename=name)):
            imported = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                prefix = '.'.join(package.split('.')[:len(package.split('.')) - node.level + 1]) if node.level else ''
                base = '.'.join(part for part in (prefix, node.module) if part)
                imported = [base, *(base + '.' + alias.name for alias in node.names)]
            for module in imported:
                consumers.setdefault(module, set()).add(name)
    affected = set(paths)
    pending = list(paths)
    while pending:
        name = pending.pop()
        module = modules.get(name, name.removeprefix('src/').removesuffix('.py').replace('/', '.'))
        # Package imports can expose a changed child through re-exported APIs.
        related = set().union(*(users for dependency, users in consumers.items()
                                if module == dependency or module.startswith(dependency + '.')))
        for consumer in related - affected:
            affected.add(consumer)
            pending.append(consumer)
    return sorted(affected)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repository', type=Path, required=True)
    p.add_argument('--base', required=True)
    p.add_argument('--head', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--github-output', type=Path)
    a = p.parse_args()
    # Include both sides of renames; no filename keyword heuristics or PR metadata.
    raw = subprocess.check_output(['git', 'diff', '--no-renames', '--name-only', '-z',
                                   a.base + '...' + a.head], cwd=a.repository)
    paths = sorted(n.decode('utf-8') for n in raw.split(b'\0') if n)
    affected = affected_consumers(a.repository, paths)
    result = classify(affected)
    structural = subprocess.check_output(['git', 'diff', '--no-renames', '--name-only',
        '--diff-filter=ACDRTUXB', a.base + '...' + a.head], cwd=a.repository)
    modes = subprocess.check_output(['git', 'diff', '--summary', a.base + '...' + a.head],
                                    cwd=a.repository)
    if structural or b'mode change' in modes:
        result['current'] = result['locking'] = True
    result.update(base=a.base, head=a.head, files=paths, affected=affected)
    a.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    if a.github_output:
        with a.github_output.open('a', encoding='utf-8') as stream:
            for key in ('current', 'locking', 'package', 'review'):
                stream.write(f'{key}={str(result[key]).lower()}\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
