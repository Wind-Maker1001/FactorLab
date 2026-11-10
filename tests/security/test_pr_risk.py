"""Conservative selection and mandatory extension failure propagation."""
import pytest
from tools.ci.pr_risk import FLAGS, affected_consumers, classify, git, select


@pytest.mark.parametrize('path', ['docs/validation/new/REPORT.md',
    'docs/validation/new/result.json', 'docs/validation/new/result.csv'])
def test_inert_report_additions_remain_fast(tmp_path, path):
    base = init_repo(tmp_path)
    write(tmp_path, path, '{}')
    result = select(tmp_path, base, commit(tmp_path))
    assert not any(result[key] for key in FLAGS)


def write(root, path, text):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding='utf-8')


def commit(root):
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-qm', 'fixture')
    return git(root, 'rev-parse', 'HEAD').decode().strip()


def init_repo(root):
    git(root, 'init', '-q')
    write(root, 'README.md', 'fixture')
    return commit(root)


@pytest.mark.parametrize('consumer,source,high', [
    ('tests/evidence/test_audit.py', 'import audit\ndef test_audit(): assert audit.VALUE == 1\n', False),
    ('src/factorlab/data/admission.py', 'from tools.data import audit\n', True),
    ('.github/workflows/audit.yml', 'run: python tools/data/audit.py\n', True),
    ('config/pipeline.yaml', 'entry: tools/data/audit.py\n', True),
])
def test_new_research_tool_requires_tests_and_no_sensitive_consumers(tmp_path, consumer, source, high):
    base = init_repo(tmp_path)
    write(tmp_path, 'tools/data/audit.py', 'VALUE = 1\n')
    write(tmp_path, 'tests/evidence/test_audit.py', 'import audit\ndef test_audit(): assert audit.VALUE == 1\n')
    write(tmp_path, consumer, source)
    result = select(tmp_path, base, commit(tmp_path))
    assert result['current'] is high
    assert 'tests/evidence/test_audit.py' in result['targeted_tests']


@pytest.mark.parametrize('mode', ['dynamic', 'untested', 'executable', 'deleted', 'symlink'])
def test_unproven_or_structural_changes_escalate(tmp_path, mode):
    init_repo(tmp_path)
    write(tmp_path, 'tools/data/audit.py', 'VALUE = 1\n')
    write(tmp_path, 'tests/evidence/test_audit.py', 'import audit\ndef test_audit(): pass\n')
    base = commit(tmp_path)
    if mode == 'deleted':
        (tmp_path / 'tools/data/audit.py').unlink()
    elif mode == 'executable':
        git(tmp_path, 'update-index', '--chmod=+x', 'tools/data/audit.py')
        git(tmp_path, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'mode')
    elif mode == 'symlink':
        oid = git(tmp_path, 'rev-parse', 'HEAD:README.md').decode().strip()
        git(tmp_path, 'update-index', '--add', '--cacheinfo', '120000,' + oid + ',tools/data/link.py')
        git(tmp_path, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'link')
    elif mode == 'dynamic':
        write(tmp_path, 'tools/data/audit.py', 'import importlib\nimportlib.import_module(input())\n')
    else:
        write(tmp_path, 'tools/data/unknown.py', 'VALUE = 1\n')
    head = 'HEAD' if mode in ('executable', 'symlink') else commit(tmp_path)
    result = select(tmp_path, base, head)
    assert result['current'] and result['locking']


def test_removed_base_consumer_and_rename_do_not_hide_risk(tmp_path):
    init_repo(tmp_path)
    write(tmp_path, 'src/factorlab/factors/a.py', 'VALUE = 1\n')
    write(tmp_path, 'src/factorlab/data/admission.py', 'from factorlab.factors.a import VALUE\n')
    base = commit(tmp_path)
    write(tmp_path, 'src/factorlab/data/admission.py', '')
    (tmp_path / 'src/factorlab/factors/a.py').rename(tmp_path / 'src/factorlab/factors/b.py')
    result = select(tmp_path, base, commit(tmp_path))
    assert result['current'] and result['locking']


@pytest.mark.parametrize('reference', [
    'import importlib\nimportlib.import_module(input())\n',
    'PATH = "tools/data/*.py"\n',
])
def test_unresolved_formal_loader_and_scoped_glob_escalate(tmp_path, reference):
    init_repo(tmp_path)
    write(tmp_path, 'src/factorlab/cli.py', reference)
    base = commit(tmp_path)
    write(tmp_path, 'tools/data/audit.py', 'VALUE = 1\n')
    write(tmp_path, 'tests/evidence/test_audit.py', 'import audit\ndef test_audit(): pass\n')
    assert select(tmp_path, base, commit(tmp_path))['current']


def test_plain_file_inventory_is_not_an_executable_consumer(tmp_path):
    init_repo(tmp_path)
    write(tmp_path, 'docs/validation/source-list.json', '["tests/evidence/verify.py"]')
    write(tmp_path, 'tools/ci/verify.py', 'INPUT = "docs/validation/source-list.json"\n')
    write(tmp_path, 'tests/evidence/verify.py', 'INPUT = "targeted-tests.xml"\n')
    base = commit(tmp_path)
    write(tmp_path, 'docs/validation/new/targeted-tests.xml', '<testsuites/>')
    write(tmp_path, 'tools/data/audit.py', 'VALUE = 1\n')
    write(tmp_path, 'tests/evidence/test_audit.py', 'import audit\ndef test_audit(): pass\n')
    result = select(tmp_path, base, commit(tmp_path))
    assert not any(result[k] for k in FLAGS)


@pytest.mark.parametrize('source,high', [
    ('from pathlib import Path\nPath("README.md").write_text("generated")\n', False),
    ('from pathlib import Path as P\n(P("output") / "README.md").write_bytes(b"generated")\n', False),
    ('from pathlib import Path\ndef publish(output): return _write(Path(output))\n'
     'def _write(output): (output / "README.md").write_text("generated")\n', True),
    ('from pathlib import Path\nPath("README.md").read_text()\n', True),
    ('from pathlib import Path\nPath("README.md").write_text(Path("README.md").read_text())\n', True),
    ('from pathlib import Path\nPath("generated.txt").write_text("README.md")\n', True),
    ('def publish(output): (output / "README.md").write_text("generated")\n', True),
    ('from pathlib import Path\ndef publish(output): return _write(output)\n'
     'def _write(output): (output / "README.md").write_text("generated")\n', True),
    ('from pathlib import Path\ndef publish(output): return _write(Path(output))\n'
     'def _write(output): (output / "README.md").write_text("generated")\n'
     'callback = _write\n', True),
    ('from pathlib import Path\ndef publish(output): return _write(Path(output))\n'
     'def _write(output):\n output = custom()\n (output / "README.md").write_text("generated")\n', True),
    ('from pathlib import Path\ndef publish(Path): Path("README.md").write_text("generated")\n', True),
    ('from custom import Path\nPath("README.md").write_text("generated")\n', True),
    ('from pathlib import Path\nPath(mapping["README.md"]).write_text("generated")\n', True),
    ('from pathlib import Path\nPath(load("README.md")).write_text("generated")\n', True),
    ('from pathlib import Path\nfrom custom import *\nPath("README.md").write_text("generated")\n', True),
    ('from pathlib import Path\ntry: pass\nexcept Exception as Path:\n Path("README.md").write_text("generated")\n', True),
    ('from pathlib import Path\nmatch value:\n case Path:\n  Path("README.md").write_text("generated")\n', True),
    ('from pathlib import Path\nmutate(Path)\nPath("README.md").write_text("generated")\n', True),
    ('def check(path):\n if path.name == "README.md": return path.read_text()\n', True),
])
def test_only_proven_pathlib_outputs_are_not_inputs(tmp_path, source, high):
    init_repo(tmp_path)
    write(tmp_path, 'tools/ci/consumer.py', source)
    base = commit(tmp_path)
    changed = 'docs/validation/new/README.md'
    write(tmp_path, changed, 'research report')
    result = select(tmp_path, base, commit(tmp_path))
    assert all(result[key] is high for key in FLAGS)
    assert ('tools/ci/consumer.py' in result['affected']) is high


@pytest.mark.parametrize('reference,dependency', [
    ('from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n'
     'files = [ROOT / name for name in ("README.md", "NOTICE")]\n', 'README.md'),
    ('from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n'
     'source = ROOT / "docs" / "user" / "README.md"\nsource.read_text()\n',
     'docs/user/README.md'),
    ('from pathlib import Path\nPath("docs/user/README.md").read_text()\n',
     'docs/user/README.md'),
    ('INPUT = "docs/user/*.md"\n', 'docs/user/README.md'),
])
def test_resolved_paths_do_not_consume_unrelated_same_named_report(tmp_path, reference, dependency):
    init_repo(tmp_path)
    consumer = 'tools/ci/consumer.py'
    write(tmp_path, consumer, reference)
    write(tmp_path, dependency, 'original')
    base = commit(tmp_path)
    changed = 'docs/validation/new/README.md'
    write(tmp_path, changed, 'report')
    result = select(tmp_path, base, commit(tmp_path))
    assert not any(result[flag] for flag in FLAGS)
    assert consumer not in result['affected']
    base = result['head']
    write(tmp_path, dependency, 'modified')
    result = select(tmp_path, base, commit(tmp_path))
    assert all(result[flag] for flag in FLAGS)
    for flag in FLAGS:
        assert dict(path=consumer, rule='affected-consumer:' + dependency) in result['reasons'][flag]


@pytest.mark.parametrize('reference', [
    'open("README.md").read()\n',
    'from pathlib import Path\nPath("README.md").read_text()\n',
    'def consume(root):\n return (root / "README.md").read_text()\n',
    'from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n'
    'def consume(ROOT):\n return (ROOT / "README.md").read_text()\n',
    'from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n'
    'ROOT = custom()\n(ROOT / "README.md").read_text()\n',
])
def test_unresolved_basename_inputs_keep_dependency_reasons(tmp_path, reference):
    init_repo(tmp_path)
    consumer = 'tools/ci/consumer.py'
    write(tmp_path, consumer, reference)
    base = commit(tmp_path)
    changed = 'docs/validation/new/README.md'
    write(tmp_path, changed, 'report')
    result = select(tmp_path, base, commit(tmp_path))
    for flag in FLAGS:
        assert result[flag]
        assert dict(path=consumer, rule='affected-consumer:' + changed) in result['reasons'][flag]


@pytest.mark.parametrize('filename', ['identity.json', 'receipt.json', 'contrasts.csv'])
@pytest.mark.parametrize('reference', [
    '"{name}"\n',
    'print("{name}")\n',
    'import logging\nlogging.info("{name}")\n',
    'from pathlib import Path\nPath("output/{name}").write_text("generated")\n',
])
def test_noninput_mentions_do_not_consume_research_files(tmp_path, filename, reference):
    init_repo(tmp_path)
    consumer = 'tools/ci/consumer.py'
    write(tmp_path, consumer, reference.format(name=filename))
    base = commit(tmp_path)
    changed = 'docs/validation/new/' + filename
    write(tmp_path, changed, '{}')
    write(tmp_path, 'docs/validation/new/REPORT.md', filename)
    result = select(tmp_path, base, commit(tmp_path))
    assert not any(result[flag] for flag in FLAGS)
    assert consumer not in result['affected']
    assert all(consumer not in decision['consumers'] for decision in result['decisions'])
    assert all(not result['reasons'][flag] for flag in FLAGS)


@pytest.mark.parametrize('reference', [
    '"receipt.json"\nopen(__doc__).read()\n',
    'def print(name): return open(name).read()\nprint("receipt.json")\n',
    'from pathlib import Path\nmutate(Path)\n'
    '(Path(__file__).resolve().parents[2] / "receipt.json").read_text()\n',
    'def consume(obj):\n return (obj.resolve().parent / "receipt.json").read_text()\n',
])
def test_unknown_mention_consumers_remain_conservative(tmp_path, reference):
    init_repo(tmp_path)
    consumer = 'tools/ci/consumer.py'
    write(tmp_path, consumer, reference)
    base = commit(tmp_path)
    changed = 'docs/validation/new/receipt.json'
    write(tmp_path, changed, '{}')
    result = select(tmp_path, base, commit(tmp_path))
    assert result['review']
    assert dict(path=consumer, rule='affected-consumer:' + changed) in result['reasons']['review']


def test_output_exclusion_does_not_hide_dynamic_execution(tmp_path):
    init_repo(tmp_path)
    write(tmp_path, 'src/factorlab/cli.py', 'import importlib\nimportlib.import_module(input())\n')
    base = commit(tmp_path)
    write(tmp_path, 'tools/data/audit.py',
          'from pathlib import Path\nPath("README.md").write_text("generated")\n')
    write(tmp_path, 'tests/evidence/test_audit.py', 'import audit\ndef test_audit(): pass\n')
    result = select(tmp_path, base, commit(tmp_path))
    assert result['current'] and result['locking']
    assert any(reason['rule'] == 'unresolved-formal-loader' for reason in result['reasons']['current'])


def test_modified_factor_selects_actual_test_consumer(tmp_path):
    init_repo(tmp_path)
    write(tmp_path, 'src/factorlab/factors/momentum.py', 'VALUE = 1\n')
    write(tmp_path, 'tests/research/test_momentum.py', 'from factorlab.factors.momentum import VALUE\n')
    base = commit(tmp_path)
    write(tmp_path, 'src/factorlab/factors/momentum.py', 'VALUE = 2\n')
    result = select(tmp_path, base, commit(tmp_path))
    assert not result['current']
    assert result['targeted_tests'] == ['tests/research/test_momentum.py']


@pytest.mark.parametrize('module,extended', [('momentum', False), ('registry', True)])
def test_reviewed_demo_loader_does_not_taint_unrelated_factor(tmp_path, module, extended):
    init_repo(tmp_path)
    write(tmp_path, 'src/factorlab/demo.py', 'import importlib\nimportlib.import_module(namespace + "." + name)\n')
    source = 'src/factorlab/factors/' + module + '.py'
    write(tmp_path, source, 'VALUE = 1\n')
    write(tmp_path, 'tests/research/test_factor.py', 'from factorlab.factors.' + module + ' import VALUE\n')
    base = commit(tmp_path)
    write(tmp_path, source, 'VALUE = 2\n')
    assert select(tmp_path, base, commit(tmp_path))['current'] is extended


def test_test_plugin_registration_is_control_plane(tmp_path):
    base = init_repo(tmp_path)
    write(tmp_path, 'tests/evidence/test_audit.py', 'pytest_plugins = ["malicious"]\n')
    result = select(tmp_path, base, commit(tmp_path))
    assert all(result[k] for k in FLAGS)


@pytest.mark.parametrize('path', ['docs/validation/x:stream.md', 'tools/data/../audit.py',
                                  'tests/evidence/test_bad.py::test_one'])
def test_noncanonical_paths_cannot_lower_risk(path):
    assert classify([path])['current']


def test_dependency_changes_do_not_request_control_approval():
    result = classify(['requirements/dev.txt'])
    assert result['current'] and result['package']
    assert not result['review']


def test_trade_execution_requires_locks():
    result = classify(['src/factorlab/backtest/execution.py'])
    assert result['current'] and result['locking']


@pytest.mark.parametrize('path', [
    'src/factorlab/data/adjustment/engine.py',
    'src/factorlab/data/admission.py',
    'src/factorlab/universe/event_evidence.py',
    'src/factorlab/forward/_windows_lock.py',
    'src/factorlab/unknown/new_module.py',
    'unclassified-input.json',
])
def test_unknown_or_sensitive_changes_require_full_software_and_locks(path):
    result = classify([path])
    assert result['current'] and result['locking']


@pytest.mark.parametrize('path', [
    '.github/workflows/factorlab-pr.yml', 'tools/ci/pr_risk.py', '.gitattributes',
    'tests/security/test_independent_controls.py',
    'pyproject.toml', 'pytest.ini', 'conftest.py', 'somewhere/conftest.py',
])
def test_control_changes_cannot_approve_themselves(path):
    result = classify([path])
    assert result['current'] and result['package'] and result['review']


def test_safe_documentation_does_not_mask_sensitive_change():
    assert classify(['docs/user/guide.md', 'src/factorlab/data/admission.py'])['current']


def test_covered_core_still_runs_mandatory_incremental_suites():
    result = classify(['src/factorlab/factors/momentum.py'])
    assert not result['current']
    assert not result['review']


def test_cross_module_consumers_force_conservative_extension(tmp_path):
    root = tmp_path / 'src/factorlab'
    (root / 'models').mkdir(parents=True)
    (root / 'data').mkdir()
    (root / 'models/base.py').write_text('class Model: pass\n')
    (root / 'data/admission.py').write_text('from ..models.base import Model\n')
    affected = affected_consumers(tmp_path, ['src/factorlab/models/base.py'])
    assert 'src/factorlab/data/admission.py' in affected
    assert classify(affected)['current']


def test_merge_gate_rejects_failures_and_missing_extensions(monkeypatch):
    import json
    from pathlib import Path
    import yaml

    workflow = yaml.load((Path(__file__).resolve().parents[2] /
        '.github/workflows/factorlab-pr.yml').read_text(), Loader=yaml.BaseLoader)
    script = next(step['run'] for step in workflow['jobs']['merge-gate']['steps']
                  if step.get('shell') == 'python {0}')
    from unittest.mock import MagicMock
    import urllib.request
    response = MagicMock()
    monkeypatch.setattr(urllib.request, 'urlopen', response)
    for name, value in dict(GITHUB_API_URL='https://api.github.com',
        GITHUB_SERVER_URL='https://github.com', GITHUB_REPOSITORY='owner/repo',
        GITHUB_RUN_ID='123', STATUS_SHA='a' * 40, GH_TOKEN='test').items():
        monkeypatch.setenv(name, value)
    results = {name: {'result': 'success'} for name in (
        'risk', 'fast', 'control-review', 'current', 'current-coverage', 'locking', 'package')}
    results['risk']['outputs'] = dict(current='true', locking='true', package='true', review='true')
    for job in results:
        for status in ('failure', 'skipped', 'cancelled'):
            attack = json.loads(json.dumps(results))
            attack[job]['result'] = status
            monkeypatch.setenv('RESULTS', json.dumps(attack))
            with pytest.raises(SystemExit) as exc:
                exec(compile(script, '<merge-gate>', 'exec'), {})
            assert exc.value.code
            assert json.loads(response.call_args.args[0].data)['state'] == 'failure'
    for job in ('current', 'current-coverage'):
        attack = {key: value for key, value in results.items() if key != job}
        monkeypatch.setenv('RESULTS', json.dumps(attack))
        with pytest.raises(SystemExit) as exc:
            exec(compile(script, '<merge-gate>', 'exec'), {})
        assert exc.value.code
    for invalid in ({}, dict(current='false', locking='false', package='false', review='unknown')):
        attack = dict(results, risk=dict(result='success', outputs=invalid))
        monkeypatch.setenv('RESULTS', json.dumps(attack))
        with pytest.raises(SystemExit) as exc:
            exec(compile(script, '<merge-gate>', 'exec'), {})
        assert exc.value.code
    for low_risk in (False, True):
        passing = json.loads(json.dumps(results))
        if low_risk:
            passing['risk']['outputs'] = dict.fromkeys(('current', 'locking', 'package', 'review'), 'false')
            for job in ('control-review', 'current', 'current-coverage', 'locking', 'package'):
                passing[job]['result'] = 'skipped'
        monkeypatch.setenv('RESULTS', json.dumps(passing))
        with pytest.raises(SystemExit) as exc:
            exec(compile(script, '<merge-gate>', 'exec'), {})
        assert not exc.value.code
        assert json.loads(response.call_args.args[0].data)['state'] == 'success'


def test_fast_consumes_trusted_plan_and_runner():
    from pathlib import Path
    import yaml
    workflow = yaml.load((Path(__file__).resolve().parents[2] /
        '.github/workflows/factorlab-pr.yml').read_text(), Loader=yaml.BaseLoader)
    fast = workflow['jobs']['fast']
    assert fast['needs'] == 'risk'
    assert any(step.get('with', {}).get('name') == 'pr-risk' and
               step.get('uses', '').startswith('actions/download-artifact@') for step in fast['steps'])
    targeted = next(step['run'] for step in fast['steps'] if '--suite targeted' in step.get('run', ''))
    assert 'python -I ../trusted/tools/ci/integration_checks.py' in targeted
    assert '--risk-plan ../trusted-risk/risk.json' in targeted
    assert any('execution.json' in step.get('with', {}).get('path', '') for step in fast['steps'])


@pytest.mark.parametrize('parents,is_pr,subject,passes', [
    ('base head', 'true', 'merge', True),
    ('other head', 'true', 'merge', False),
    ('base other', 'true', 'merge', False),
    ('base head extra', 'true', 'merge', False),
    ('', 'false', 'head', True),
    ('', 'false', 'other', False),
])
def test_risk_binds_exact_event_parents(monkeypatch, tmp_path, parents, is_pr, subject, passes):
    from tools.ci import pr_identity
    monkeypatch.setattr(pr_identity, 'git',
                        lambda repository, *args: subject if args[0] == 'rev-parse' else parents)
    expected = dict(pr=47 if is_pr == 'true' else None, base='base', pr_head='head')
    if passes:
        assert pr_identity.subject(tmp_path, expected) == subject
    else:
        with pytest.raises(ValueError):
            pr_identity.subject(tmp_path, expected)



def test_pr_shards_and_coverage_use_one_trusted_subject():
    from pathlib import Path
    import yaml
    workflow = yaml.load((Path(__file__).resolve().parents[2] /
        '.github/workflows/factorlab-pr.yml').read_text(), Loader=yaml.BaseLoader)
    jobs = workflow['jobs']
    current = jobs['current']
    assert current['strategy'] == dict(**{'fail-fast': 'false'},
                                      matrix=dict(python=['3.13', '3.14'], shard=['0', '1', '2']))
    assert current['if'] == "needs.risk.outputs.current == 'true'"
    assert any('--suite current --shard ${{ matrix.shard }}' in step.get('run', '')
               for step in current['steps'])
    for name in ('fast', 'current', 'locking', 'package'):
        checkout = jobs[name]['steps'][0]
        assert checkout['with']['ref'] == '${{ github.event.pull_request.base.sha || github.sha }}'
        assert checkout['with']['path'] == 'candidate'
        reconstruction = next(step for step in jobs[name]['steps']
                              if 'pr_merge_ref.py' in step.get('run', ''))
        assert reconstruction['env']['VALIDATION_SUBJECT'] == '${{ needs.risk.outputs.subject }}'
        assert '--expected-subject "$VALIDATION_SUBJECT"' in reconstruction['run']
        assert checkout['with']['persist-credentials'] == 'false'
    artifact = current['steps'][-1]['with']
    assert artifact['name'] == 'extended-${{ matrix.python }}-shard-${{ matrix.shard }}'
    assert 'local-ci/extended/*/execution.json' in artifact['path']
    assert 'local-ci/extended/*/dependencies.txt' in artifact['path']
    coverage = jobs['current-coverage']
    assert coverage['needs'] == ['risk', 'current']
    assert 'always()' in coverage['if'] and "needs.risk.outputs.current == 'true'" in coverage['if']
    assert coverage['steps'][0]['with']['ref'] == '${{ github.event.pull_request.base.sha || github.sha }}'
    assert any('test "$RESULT" = success' in step.get('run', '') for step in coverage['steps'])
    aggregate = next(step['run'] for step in coverage['steps'] if '--pr-shards' in step.get('run', ''))
    assert 'for version in 3.13 3.14' in aggregate and 'for shard in 0 1 2' in aggregate
    for flag in ('subject-sha', 'pr-head-sha', 'trusted-base-sha', 'event-sha', 'run-id', 'run-attempt'):
        assert '--' + flag in aggregate
    gate = jobs['merge-gate']
    assert 'current-coverage' in gate['needs'] and gate['if'] == 'always()'
    assert gate['environment'] == 'ci-merge-publisher'
    assert any('test "$APP_ID" = 5670895' in step.get('run', '') for step in gate['steps'])
    assert jobs['control-review']['environment'] == 'ci-control-review'
    assert 'pull_request_target' in workflow['on']
    for job in jobs.values():
        assert 'continue-on-error' not in job
        assert all('continue-on-error' not in step for step in job.get('steps', []))


def test_full_retains_real_legacy_failure_and_release_block():
    from pathlib import Path
    import yaml

    root = Path(__file__).resolve().parents[2]
    full = yaml.load((root / '.github/workflows/factorlab-full.yml').read_text(), Loader=yaml.BaseLoader)
    assert {'workflow_dispatch', 'workflow_call', 'schedule', 'push'} <= set(full['on'])
    assert 'legacy' in full['jobs']['release-gate']['needs']
    for job in full['jobs'].values():
        assert 'continue-on-error' not in job
        for step in job.get('steps', []):
            assert 'continue-on-error' not in step
    assert any('--suite historical' in s.get('run', '') for s in full['jobs']['legacy']['steps'])
    assert any('--historical-only' in s.get('run', '') for s in full['jobs']['validation-v2-review']['steps'])
    assert full['jobs']['incremental']['strategy']['matrix'] == {
        'python': ['3.14', '3.13'], 'shard': ['0', '1', '2']}
    assert full['jobs']['incremental']['strategy']['fail-fast'] == 'false'
    assert 'current-coverage' in full['jobs']['release-gate']['needs']
    assert full['jobs']['current-coverage']['if'] == 'always()'
    assert full['jobs']['current-coverage']['needs'] == 'incremental'
    assert any('test "$RESULT" = success' in s.get('run', '')
               for s in full['jobs']['current-coverage']['steps'])


def test_full_release_gate_refuses_every_non_success(monkeypatch):
    from pathlib import Path
    import json
    import yaml
    full = yaml.load((Path(__file__).resolve().parents[2] /
        '.github/workflows/factorlab-full.yml').read_text(), Loader=yaml.BaseLoader)
    gate = full['jobs']['release-gate']
    results = {job: {'result': 'success'} for job in gate['needs']}
    for job in results:
        for status in ('failure', 'skipped', 'cancelled'):
            attack = dict(results, **{job: {'result': status}})
            monkeypatch.setenv('RESULTS', json.dumps(attack))
            with pytest.raises(SystemExit) as exc:
                exec(compile(gate['steps'][0]['run'], '<full-release-gate>', 'exec'), {})
            assert exc.value.code

