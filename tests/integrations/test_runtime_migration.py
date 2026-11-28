from copy import deepcopy
from types import SimpleNamespace

import pytest

from factorlab.strategies.runtime_migration import require_compatible, require_drained, _PROBE


def ast_identity(tree):
    import ast
    import json
    from hashlib import sha256
    node = next(node for node in ast.parse(_PROBE).body
        if isinstance(node, ast.FunctionDef) and node.name == 'ast_identity')
    namespace = dict(ast=ast, json=json, sha256=sha256)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<canonical AST>', 'exec'), namespace)
    return namespace['ast_identity'](tree)


def test_ast_identity_ignores_empty_version_fields_but_preserves_semantics():
    import ast
    original = ast.parse('def compute(x): return x + 1')
    changed = ast.parse('def compute(x): return x + 1')
    function = changed.body[0]
    function._fields = (*function._fields, 'new_optional_field')
    function.new_optional_field = None
    assert ast_identity(original) == ast_identity(changed)
    function.new_optional_field = [ast.Constant(value='semantic')]
    assert ast_identity(original) != ast_identity(changed)
    assert ast_identity(original) != ast_identity(ast.parse('def compute(x): return x + 2'))


def test_generated_entrypoints_verify_interpreter_and_preserve_body(tmp_path):
    import ast
    from hashlib import file_digest, sha256
    import os
    import sys
    import venv
    from pathlib import Path
    tree = ast.parse(_PROBE)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {'file_identity', 'dependency_identity'}]
    identities = []
    body = b'import sys\nfrom pip._internal.cli.main import main\nsys.exit(main())\n'
    for label in ('old', 'replacement'):
        environment = tmp_path / label
        venv.EnvBuilder(with_pip=False).create(environment)
        scripts = environment / ('Scripts' if sys.platform == 'win32' else 'bin')
        python = scripts / ('python.exe' if sys.platform == 'win32' else 'python')
        alias = scripts / 'verified-python3'
        os.link(python, alias)
        entry = scripts / 'pip'
        def write(interpreter, payload):
            entry.write_bytes(b'#!' + os.fsencode(interpreter) + b'\n' + payload)
        namespace = dict(Path=Path, sha256=sha256, file_digest=file_digest, os=os,
            sys=SimpleNamespace(executable=str(python)),
            sysconfig=SimpleNamespace(get_path=lambda key: str(scripts)))
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<entrypoint identity>', 'exec'), namespace)
        write(python, body)
        identities.append(namespace['dependency_identity'](entry))
        current = tmp_path / (label + '-current')
        if sys.platform == 'win32':
            import subprocess
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(current), str(environment)],
                check=True, capture_output=True)
        else:
            current.symlink_to(environment, target_is_directory=True)
        namespace['sys'].executable = str(current / scripts.name / python.name)
        assert namespace['dependency_identity'](entry) == identities[-1]
        write(alias, body)
        assert namespace['dependency_identity'](entry) == identities[-1]
        write(python, body + b'print("changed")\n')
        assert namespace['dependency_identity'](entry) != identities[-1]
        wrong = scripts.parent / 'other/bin/python'
        wrong.parent.mkdir(parents=True)
        os.link(python, wrong)
        write(wrong, body)
        with pytest.raises(ValueError, match='interpreter'):
            namespace['dependency_identity'](entry)
    assert identities[0] == identities[1]


def test_migration_registry_retains_installed_predecessor_and_rejects_future_edits():
    import ast
    import subprocess
    from factorlab.strategies.runtime_migration import _AUDITED
    tree = ast.parse(subprocess.check_output(['git', 'show',
        'd912c8e:src/factorlab/strategies/runtime_migration.py']))
    tree.body = [node for node in tree.body if not (isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == '_AUDITED' for target in node.targets))]
    allowed = _AUDITED['factorlab.strategies.runtime_migration']
    assert ast_identity(tree) in allowed
    tree.body.append(ast.Expr(value=ast.Constant(value='unaudited future change')))
    assert ast_identity(tree) not in allowed


def evidence():
    return dict(manifest={'package_id': 'package'}, binding=dict(package_id='package',
        runtime_sha256='old', configuration={'size': '1'}, model={'sha256': 'frozen'}),
        sources={'strategy': 'compute'}, dependencies={'numpy': 'wheel-bytes'},
        interpreter={'stdlib': 'stdlib-bytes'})


def test_same_compute_allows_new_runtime_identity():
    old = evidence()
    new = deepcopy(old)
    new['binding']['runtime_sha256'] = 'new-host'
    require_compatible(old, new, old['manifest'])


@pytest.mark.parametrize('field', ['sources', 'dependencies', 'interpreter', 'manifest'])
def test_changed_compute_or_environment_is_not_admitted(field):
    old = evidence()
    new = deepcopy(old)
    new[field] = {'changed': True}
    with pytest.raises(ValueError):
        require_compatible(old, new, old['manifest'])


@pytest.mark.parametrize('field', ['configuration', 'model', 'package_id'])
def test_identity_check_is_never_bypassed(field):
    old = evidence()
    new = deepcopy(old)
    new['binding'][field] = 'changed'
    with pytest.raises(ValueError, match='identity'):
        require_compatible(old, new, old['manifest'])


def test_uncommitted_frozen_events_must_drain_but_committed_receipts_survive():
    binding = {'instance_id': 'one'}
    committed = {'binding': binding, 'event_id': 'committed'}
    frozen = {'binding': binding, 'event_id': 'frozen'}
    store = SimpleNamespace(binding=lambda: binding,
        decision=lambda package, event, identity: {'saved': True} if event == 'committed' else None)
    require_drained(store, 'package', [committed, {'binding': {'instance_id': 'other'}}])
    with pytest.raises(ValueError, match='drain frozen event frozen'):
        require_drained(store, 'package', [committed, frozen])


def test_binance_frozen_inputs_come_from_controller_journal(tmp_path):
    import sqlite3
    from factorlab.strategies.runtime_migration import _events
    from factorlab.trading.storage import dumps
    binding = {'instance_id': 'one'}
    event = {'binding': binding, 'event_id': 'frozen'}
    path = tmp_path / 'journal/controller.sqlite3'
    path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE demo_instance_inputs(instance_id,package_id,event_id,payload)')
        for owner, package in (('one', 'active'), ('other', 'active'), ('one', 'old')):
            db.execute('INSERT INTO demo_instance_inputs VALUES(?,?,?,?)',
                (owner, package, 'frozen', dumps(event)))
    # An unrelated portfolio DB must not hide the controller's frozen input.
    sqlite3.connect(tmp_path / 'portfolio.sqlite3').close()
    store = SimpleNamespace(instance_id='one', binding=lambda: binding,
        decision=lambda *args: None)
    events = list(_events(tmp_path, 'binance', store, 'active'))
    assert events == [event]
    with pytest.raises(ValueError, match='drain frozen event frozen'):
        require_drained(store, 'active', events)
    store.decision = lambda *args: {'committed': True}
    require_drained(store, 'active', events)


@pytest.mark.parametrize('active', [False, True])
def test_demo_installer_migration_failure_restores_original_active_state(active):
    import ast
    from pathlib import Path
    from factorlab.strategies.loader import SOURCE_ROOT
    tree = ast.parse((SOURCE_ROOT.parent / 'tools/install_demo_strategy_runtime.py').read_text())
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
    lock = next(node for node in main.body if isinstance(node, ast.With))
    start = next(i for i, node in enumerate(lock.body) if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'was_active' for target in node.targets))
    code = ast.Module(body=lock.body[start:start + 3], type_ignores=[])
    calls = []
    def command(*args):
        calls.append(args)
        if args[:2] == ('systemctl', 'show'):
            return 'active' if active else 'inactive'
        if args[0] == 'runuser':
            raise ValueError('incompatible')
    namespace = dict(command=command, UNIT='execute.service', python='new',
        STATE=Path('/state'), old_venv=Path('/old'))
    with pytest.raises(ValueError, match='incompatible'):
        exec(compile(code, '<installer>', 'exec'), namespace)
    assert (('systemctl', 'start', 'execute.service') in calls) is active


@pytest.mark.parametrize('active', [False, True])
def test_alpaca_installer_migration_failure_restores_original_active_state(tmp_path, active):
    import shutil
    import subprocess
    from factorlab.strategies.loader import SOURCE_ROOT
    shell = shutil.which('sh')
    if shell is None:
        pytest.skip('POSIX installer requires sh')
    text = (SOURCE_ROOT.parent / 'tools/install_alpaca_paper_runtime.sh').read_text()
    fragment = text.split('    was_active=false', 1)[1].split('\nfi\nln -s', 1)[0]
    import shlex
    import sys
    fragment = fragment.replace('"$base/current/venv/bin/python"', shlex.quote(sys.executable))
    script = ('set -eu\nsystemctl() { '
        'if [ "$1" = show ]; then printf "%s\\n" "FACTORLAB_ALPACA_STATE_ROOT=/state/r01-return"; return; fi; '
        'printf "%s\\n" "$*"; '
        'if [ "$1" = is-active ]; then return ' + ('0' if active else '3') + '; fi; }\n'
        'runuser() { printf "%s\\n" "$*"; return 1; }\nrelease=/new\nbase=/old\nwas_active=false' + fragment)
    result = subprocess.run([shell, '-c', script], text=True, capture_output=True)
    assert result.returncode == 1
    assert ('start factorlab-alpaca-paper.service' in result.stdout) is active
    assert '--state-root /state/r01-return' in result.stdout


def test_compute_closure_can_be_proved_for_installed_framework():
    import ast
    from hashlib import sha256
    from importlib.util import resolve_name
    from pathlib import Path
    from factorlab.strategies.loader import SOURCE_ROOT
    modules = {}
    for path in (SOURCE_ROOT / 'factorlab').rglob('*.py'):
        parts = list(path.relative_to(SOURCE_ROOT).with_suffix('').parts)
        if parts[-1] == '__init__':
            parts.pop()
        modules['.'.join(parts)] = path
    tree = ast.parse(_PROBE)
    visit = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'visit')
    namespace = dict(ast=ast, sha256=sha256, resolve_name=resolve_name, ast_identity=ast_identity,
        visited=set(), sources={}, modules=modules,
        framework={'factorlab.strategies.loader', 'factorlab.strategies.worker', 'factorlab.strategies.package'})
    exec(compile(ast.Module(body=[visit], type_ignores=[]), '<probe traversal>', 'exec'), namespace)
    for name in ('factorlab.strategies.loader', 'factorlab.strategies.worker',
                 'factorlab.strategies.decision_runtime', 'factorlab.strategies.v69_runtime'):
        namespace['visit'](name)
    assert 'factorlab.integrations.alpaca.cloud_runtime' not in namespace['sources']
    assert 'factorlab.integrations.binance_futures_trading.demo_cloud_storage' not in namespace['sources']


def test_audited_host_changes_admit_only_outside_strategy_import_closure(tmp_path):
    import ast
    from hashlib import sha256
    name = 'factorlab.integrations.alpaca.cloud_runtime'
    path = tmp_path / 'cloud_runtime.py'
    import json
    segment = _PROBE.split('audited = ', 1)[1].split('# Freeze interpreter,', 1)[0]
    segment = 'audited = ' + segment
    before = 'def owner_lock(): return 1\nclass MarketWorker:\n def run(self): return 1\n'
    after = 'def owner_lock(): return 1\nclass MarketWorker:\n def run(self): return 2\nclass StreamRetention: pass\n'
    allowed = [ast_identity(ast.parse(text))
               for text in (before, after)]

    def digest(text, imported=False):
        path.write_text(text)
        tree = ast.parse(text)
        full = ast_identity(tree)
        namespace = dict(ast=ast, sha256=sha256, modules={name: path}, json=json, ast_identity=ast_identity,
            sys=SimpleNamespace(argv=['probe', 'package', json.dumps({name: allowed})]),
            visited={name} if imported else set(), sources={name: full} if imported else {})
        exec(compile(segment, '<audited host proof>', 'exec'), namespace)
        return namespace['sources'][name]

    before = 'def owner_lock(): return 1\nclass MarketWorker:\n def run(self): return 1\n'
    after = 'def owner_lock(): return 1\nclass MarketWorker:\n def run(self): return 2\nclass StreamRetention: pass\n'
    assert digest(before) == digest(after)
    assert digest(before, True) != digest(after, True)
    with pytest.raises(ValueError, match='unsupported audited runtime contents'):
        digest(after.replace('owner_lock(): return 1', 'owner_lock(): return 2'))
    with pytest.raises(ValueError, match='unsupported audited runtime contents'):
        digest(after.replace('run(self): return 2', 'run(self): return 3'))


@pytest.fixture(scope='module')
def paired_release_interpreters(tmp_path_factory):
    import io
    import os
    from pathlib import Path
    import shutil
    import subprocess
    import sys
    import tarfile
    import venv
    from importlib.metadata import distribution
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from factorlab.strategies.loader import SOURCE_ROOT

    root = SOURCE_ROOT.parent
    workspace = tmp_path_factory.mktemp('runtime-pair')
    baselines = []
    for revision in ('47e58d9044e27dc47fc140f87b48b73531db6d3f',
                     '80d22fa23b2bedd557684dd4db9559e6180ffb9e',
                     '2e02729157b1e6a9c277506533d1af1fc1a6a1a0'):
        baseline = workspace / revision
        baseline.mkdir()
        archive = subprocess.check_output(['git', '-C', str(root), 'archive', revision, 'src'])
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(baseline, filter='data')
        baselines.append(baseline / 'src')
    dependencies = workspace / 'dependencies'
    dependencies.mkdir()
    pending = ['numpy', 'pandas', 'tzdata']
    copied = set()
    while pending:
        name = canonicalize_name(pending.pop())
        if name in copied:
            continue
        copied.add(name)
        dist = distribution(name)
        for requirement in dist.requires or ():
            dependency = Requirement(requirement)
            if dependency.marker is None or dependency.marker.evaluate({'extra': ''}):
                pending.append(dependency.name)
        for file in dist.files:
            relative = Path(file)
            if '..' in relative.parts or relative.suffix == '.pyc':
                continue
            source = Path(dist.locate_file(file))
            if not source.is_file():
                continue
            target = dependencies / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source, target)
            except OSError:
                shutil.copyfile(source, target)
    interpreters = []
    for label, sources in (('previous', baselines[0]), ('production', baselines[1]),
                           ('announcements', baselines[2]), ('replacement', SOURCE_ROOT)):
        env = workspace / label
        venv.EnvBuilder(with_pip=False).create(env)
        python = env / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        site = Path(subprocess.check_output([str(python), '-I', '-c',
            'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip())
        (site / 'runtime-pair.pth').write_text(str(sources) + '\n' + str(dependencies) + '\n')
        interpreters.append(python)
    return workspace, interpreters


@pytest.mark.parametrize('variant', ['external', 'binance-frozen', 'v69', 'v179', 'v182'])
def test_actual_baseline_candidate_probe_preserves_package_and_admits_host_upgrade(
        tmp_path, paired_release_interpreters, variant):
    import json
    from dataclasses import asdict
    from pathlib import Path
    from factorlab.strategies.package import build_package, install_package
    from factorlab.strategies.runtime_migration import probe
    from factorlab.strategies.loader import SOURCE_ROOT
    root = SOURCE_ROOT.parent
    workspace, interpreters = paired_release_interpreters
    archive = tmp_path / 'package.zip'
    if variant == 'binance-frozen':
        from examples.external_strategy.run import example_manifest
        manifest = build_package(root / 'examples',
            example_manifest('c', commit='a' * 40, account_id='demo'), archive)
    elif variant == 'v69':
        from factorlab.integrations.alpaca.native_v69 import NATIVE_V69_WRAPPER
        from factorlab.strategies.v69_runtime import V69AlpacaStrategy
        source = tmp_path / 'native'
        source.mkdir()
        (source / 'strategy.py').write_bytes(NATIVE_V69_WRAPPER)
        spec = V69AlpacaStrategy(account_id='paper').describe()
        fields = asdict(spec)
        requirements = {key: fields[key] for key in ('data', 'schedule', 'state', 'capital', 'execution')}
        requirements['capital']['requested'] = '0'
        manifest = build_package(source, dict(schema_version=2, interface='FACTORLAB_DECISION_V2',
            strategy_id=spec.strategy_id, strategy_version=spec.version, market=spec.market,
            account_id='paper', symbols=spec.symbols, execution_mode='quantity_deltas',
            requirements=requirements, code_commit='a' * 40, entrypoint='strategy:Strategy',
            files={'strategy.py': ''}, configuration={'account_id': 'paper'}, dependencies=[],
            evidence={'native_strategy': 'V69_CAUSAL', 'journal': 'portfolio.sqlite3'},
            migration='wait_flat'), archive)
    elif variant == 'external':
        from examples.alpaca_balanced.strategy import BalancedPortfolio
        configuration = dict(account_id='paper', symbols=['SPY', 'IWM'], capital='100')
        spec = BalancedPortfolio(**configuration).describe()
        fields = asdict(spec)
        requirements = {key: fields[key] for key in ('data', 'schedule', 'state', 'capital', 'execution')}
        requirements['capital']['requested'] = '100'
        manifest = build_package(root / 'examples/alpaca_balanced', dict(schema_version=2,
            interface='FACTORLAB_DECISION_V2', strategy_id=spec.strategy_id, strategy_version=spec.version,
            market=spec.market, account_id='paper', symbols=spec.symbols, execution_mode='target_weights',
            requirements=requirements, code_commit='a' * 40, entrypoint='strategy:BalancedPortfolio',
            files={'strategy.py': ''}, configuration=configuration, dependencies=[],
            evidence={'offline': True}, migration='wait_flat'), archive)
    else:
        from factorlab.strategies.v179_v182_runtime import build_blend_strategy_package
        artifact = json.loads((root / 'examples/v179_v182_alpaca/artifacts/v179_v182_ridge_target.json').read_text())
        manifest = build_blend_strategy_package(variant=variant, account_id='paper', capital='100',
            model_artifact=artifact, code_commit='a' * 40, destination=archive, evidence={'offline': True})
    release, _ = install_package(archive, tmp_path / 'releases')
    previous, production, announcements, replacement = [probe(python, release) for python in interpreters]
    for baseline in (previous, production):
        assert baseline['binding']['runtime_sha256'] != replacement['binding']['runtime_sha256']
        with pytest.raises(ValueError, match='sources'):
            require_compatible(baseline, replacement, manifest)
    require_compatible(announcements, replacement, manifest)
    require_compatible(replacement, announcements, manifest)
    if variant == 'v69':
        _assert_admitted_runtime_chain(tmp_path, manifest, production['binding'],
            announcements['binding'], replacement['binding'])


def _assert_admitted_runtime_chain(tmp_path, manifest, original, admitted, replacement):
    from contextlib import closing
    from factorlab.strategies.deployment import DeploymentStore
    package = manifest['package_id']
    binding = dict(market=manifest['market'], account_id='paper', strategy_id='V69_CAUSAL',
        instance_id='V69_CAUSAL', allocation_id='v69-original')
    with closing(DeploymentStore(tmp_path / 'deployments.sqlite3')) as registry:
        registry.register_instance(binding)
        with closing(registry.for_instance('V69_CAUSAL')) as view:
            view.adopt_existing(manifest, {'weekly': 'unchanged'}, 'saved-event',
                {'journal_id': 'portfolio.sqlite3', 'source_code_sha256': original['code_sha256']},
                verify=lambda *args: None)
            view.bind_runtime(package, original)
            # This records the already-authorized production admission, rather
            # than claiming its economic changes qualify for today's proof.
            assert view.admit_runtime_upgrade(package, original, admitted)
            saved = view._runtime_binding(package)['binding']
            before = view.snapshot()
            assert view.admit_runtime_upgrade(package, admitted, replacement)
            assert not view.admit_runtime_upgrade(package, admitted, replacement)
            view.bind_runtime(package, replacement)
            view.bind_runtime(package, admitted)
            view.bind_runtime(package, original)
            assert view._runtime_binding(package)['binding'] == saved
            assert view.snapshot() == before
            chain = view._rows('runtime_admissions', 'package_id=? AND runtime_sha256=?',
                (package, replacement['runtime_sha256'])).fetchone()
            assert chain['previous_runtime_sha256'] == admitted['runtime_sha256']


def test_probe_rejects_dynamic_code_loading_before_admission():
    # Exercise the exact probe traversal without starting or importing a strategy.
    import ast
    from hashlib import sha256
    from importlib.util import resolve_name
    from pathlib import Path
    import tempfile
    tree = ast.parse(_PROBE)
    visit = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'visit')
    namespace = dict(ast=ast, sha256=sha256, resolve_name=resolve_name, ast_identity=ast_identity,
        visited=set(), sources={}, modules={}, framework=set())
    exec(compile(ast.Module(body=[visit], type_ignores=[]), '<probe traversal>', 'exec'), namespace)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'strategy.py'
        path.write_text("__import__('factorlab.integrations.alpaca.cloud_runtime')")
        with pytest.raises(ValueError, match='dynamic code'):
            namespace['visit']('strategy', path, True)
