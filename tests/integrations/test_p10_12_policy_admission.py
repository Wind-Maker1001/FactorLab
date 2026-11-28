from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    'p10_12_native_migration',
    Path(__file__).resolve().parents[2] / 'tools/migrate_alpaca_v69_runtime.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


@pytest.fixture
def transition():
    before = dict(manifest={'package': 'native'},
                  binding={'package_id': 'native', 'runtime_sha256': 'old'},
                  sources={name: pair[0] for name, pair in migration._POLICY_SOURCES.items()},
                  dependencies={'numpy': 'unchanged'}, interpreter={'stdlib': 'unchanged'})
    before['sources']['factorlab.integrations.alpaca.native_v69'] = 'unchanged'
    after = deepcopy(before)
    after['binding']['runtime_sha256'] = 'new'
    after['sources'].update({name: pair[1] for name, pair in migration._POLICY_SOURCES.items()})
    return before, after


def qualify(before, after, old_closure=frozenset(), new_closure=frozenset()):
    return migration.require_policy_transition(
        before, after, before['manifest'], old_closure, new_closure)


def test_exact_policy_transition_is_classified_without_mutating_probes(transition):
    before, after = transition
    retained = deepcopy(after)
    receipt = qualify(before, after)
    assert receipt['kind'] == 'HOST_POLICY_CHANGE'
    assert set(receipt['source_pairs']) == set(migration._POLICY_SOURCES)
    assert after == retained
    assert qualify(before, after) == receipt


@pytest.mark.parametrize('side', [0, 1])
@pytest.mark.parametrize('name', list(migration._POLICY_SOURCES))
def test_wrong_policy_pair_is_rejected(transition, side, name):
    transition[side]['sources'][name] = 'future-or-unreviewed'
    with pytest.raises(ValueError, match='source pair'):
        qualify(*transition)


@pytest.mark.parametrize('side', [0, 1])
@pytest.mark.parametrize('name', list(migration._POLICY_SOURCES))
def test_compute_import_cannot_be_exempted(transition, side, name):
    closures = [set(), set()]
    closures[side].add(name)
    with pytest.raises(ValueError, match='compute closure'):
        qualify(*transition, *closures)


@pytest.mark.parametrize('key', ['sources', 'dependencies', 'interpreter', 'manifest', 'binding'])
def test_other_runtime_changes_are_rejected(transition, key):
    before, after = transition
    after[key]['extra'] = 'changed'
    with pytest.raises(ValueError):
        qualify(before, after)


def test_policy_source_pairs_bind_reviewed_repository_contents():
    import ast
    import hashlib
    import json
    import subprocess
    from factorlab.strategies.runtime_migration import _PROBE
    node = next(node for node in ast.parse(_PROBE).body
                if isinstance(node, ast.FunctionDef) and node.name == 'ast_identity')
    namespace = dict(ast=ast, json=json, sha256=hashlib.sha256)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<identity>', 'exec'), namespace)
    root = Path(__file__).resolve().parents[2]
    for name, pair in migration._POLICY_SOURCES.items():
        path = 'src/' + name.replace('.', '/') + '.py'
        previous = subprocess.check_output(['git', 'show', 'e9791e1:' + path], cwd=root)
        replacement = (root / path).read_bytes()
        assert tuple(namespace['ast_identity'](ast.parse(source))
                     for source in (previous, replacement)) == pair
