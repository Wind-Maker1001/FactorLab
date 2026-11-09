"""Conservative merge-base risk selection, executed from the trusted base checkout."""
import argparse
import base64
import ast
import json
from pathlib import Path
import subprocess


SAFE_ROOTS = ('docs/user/',)
CONTROL_ROOTS = ('.github/', 'tools/ci/', 'tools/security/', 'tests/security/',
                 'requirements/', 'docs/ci/')
CORE_ROOTS = ('src/factorlab/factors/', 'src/factorlab/models/',
              'src/factorlab/metrics/', 'src/factorlab/validation/',
              'src/factorlab/labels/', 'src/factorlab/evaluation/',
              'src/factorlab/backtest/', 'src/factorlab/portfolio/',
              'src/factorlab/cross_market/')
CONTROL_FILES = ('pyproject.toml', 'MANIFEST.in', 'setup.cfg', 'setup.py',
                 'pytest.ini', 'conftest.py', 'tox.ini', '.gitattributes', '.gitignore')
ROOT_DOCUMENTS = ('README.md', 'LICENSE', 'NOTICE', 'CONTRIBUTING.md', 'SECURITY.md')
RESEARCH_DOCUMENT_EXTENSIONS = ('.md', '.txt', '.csv', '.json', '.xml',
                                '.pdf', '.png', '.jpg', '.jpeg', '.sha256')
# Existing independent evidence checks and admission preflight are not ordinary diagnostics.
PROTECTED_RESEARCH_PATHS = (
    'tests/evidence/test_pit_archive_verifiers.py',
    'tools/data/candidate_preflight.py',
)


def targeted_path(path):
    """Only standalone diagnostics and ordinary evidence tests qualify."""
    parts = path.split('/')
    return (path not in PROTECTED_RESEARCH_PATHS and len(parts) == 3 and
            ((parts[:2] == ['tools', 'data'] and path.endswith('.py') and
              parts[2] not in ('__init__.py', 'conftest.py')) or
             (parts[:2] == ['tests', 'evidence'] and parts[2].startswith('test_') and
              parts[2].endswith('.py'))))


def research_document(path):
    parts = path.split('/')
    return (len(parts) >= 3 and parts[:2] == ['docs', 'validation'] and
            all(p not in ('', '.', '..') and not p.startswith('.') for p in parts[2:]) and
            Path(path).suffix.lower() in RESEARCH_DOCUMENT_EXTENSIONS)


def control_path(path):
    return (path.startswith(CONTROL_ROOTS) or path in CONTROL_FILES or
            path.endswith('/conftest.py') or
            (path.startswith(('tools/', 'tests/', 'docs/validation/')) and
             not targeted_path(path) and not research_document(path)))


def covered_path(path):
    return (path.startswith(CORE_ROOTS + SAFE_ROOTS) or path in ROOT_DOCUMENTS or
            targeted_path(path) or research_document(path))


def safe_addition(path):
    """Additions to executable/application paths still require full validation."""
    return (targeted_path(path) or research_document(path) or
            (path.startswith(SAFE_ROOTS) and
             Path(path).suffix.lower() in RESEARCH_DOCUMENT_EXTENSIONS))


def classify(paths, changes=(), unsafe_modes=False):
    """Fail closed for unfamiliar code, deletion, type changes and unsafe modes."""
    control = any(control_path(path) for path in paths)
    unknown = any(not covered_path(path) for path in paths)
    structural = any(status != 'A' and status != 'M' or
                     (status == 'A' and not safe_addition(path))
                     for status, path in changes)
    # Never treat executable/symlink additions or mode changes as harmless documents.
    risky = unknown or structural or unsafe_modes
    return dict(current=risky, locking=risky, package=control or unsafe_modes,
                review=control or unsafe_modes or any(status not in ('A', 'M')
                       for status, _ in changes), files=paths)



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
    # Compare the trusted merge-base with the candidate, never trusting PR labels.
    raw = subprocess.check_output(['git', 'diff', '--no-renames', '--name-status', '-z',
                                   a.base + '...' + a.head], cwd=a.repository)
    fields = [part.decode('utf-8') for part in raw.split(b'\0') if part]
    if len(fields) % 2:
        raise ValueError('unexpected git name-status output')
    changes = [(fields[i], fields[i + 1]) for i in range(0, len(fields), 2)]
    paths = sorted({path for _, path in changes})
    affected = affected_consumers(a.repository, paths)
    modes = subprocess.check_output(['git', 'diff', '--summary', a.base + '...' + a.head],
                                    cwd=a.repository)
    # An added symlink or executable must not hide behind a safe filename.
    nonregular_additions = []
    for status, path in changes:
        if status == 'A':
            tree = subprocess.check_output(['git', 'ls-tree', a.head, '--', path],
                                           cwd=a.repository)
            mode = tree.split(b' ', 1)[0] if tree else b''
            if mode != b'100644':
                nonregular_additions.append(path)
    unsafe_modes = b'mode change' in modes or bool(nonregular_additions)
    result = classify(affected, changes=changes, unsafe_modes=unsafe_modes)
    # Even a normally safe diagnostic becomes sensitive when imported by runtime code.
    if any(targeted_path(path) and path.startswith('tools/data/') for path in paths) and any(
            path.startswith('src/factorlab/') and path not in paths for path in affected):
        result['current'] = result['locking'] = True
    targets = sorted(path for path in paths if targeted_path(path))
    result['targeted_paths'] = targets
    result['unusual_added_modes'] = nonregular_additions
    result.update(base=a.base, head=a.head, files=paths, affected=affected)
    a.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    if a.github_output:
        encoded_targets = base64.b64encode(json.dumps(targets, separators=(',', ':')).encode()).decode()
        with a.github_output.open('a', encoding='utf-8') as stream:
            for key in ('current', 'locking', 'package', 'review'):
                stream.write(f'{key}={str(result[key]).lower()}\n')
            stream.write(f'targeted={encoded_targets}\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
