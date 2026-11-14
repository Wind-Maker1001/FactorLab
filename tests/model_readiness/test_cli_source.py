"""CLI final source verification uses the runner's offline identity contract."""
import json
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from factorlab.diagnostics.model_readiness import __main__ as cli
from factorlab.diagnostics.model_readiness import runner


@pytest.mark.parametrize('changed', [False, True])
def test_cli_final_running_source_identity(tmp_path, monkeypatch, changed):
    identity = {'git_commit': None, 'source_sha256': {'runner.py': 'initial'}}
    monkeypatch.setattr(runner, 'software_identity', lambda: identity)

    def acceptance(output, *args, **kwargs):
        runner.save_json(output / 'source-start.json', runner.running_source_snapshot())
        pd.DataFrame([{'seconds': 0}]).to_csv(output / 'resources.csv', index=False)
        return {'matrix': [{'check': check, 'status': 'PASS'} for check in
                           ('comparison_counterexamples', 'input_identity')],
                'source_verification': {'status': 'PASS'}}

    def comparison(*args, **kwargs):
        (tmp_path / 'comparison-tests.xml').write_text(
            '<testsuite>' + '<testcase />' * 10 + '</testsuite>')
        if changed:
            identity['source_sha256']['runner.py'] = 'modified'
        return SimpleNamespace(returncode=0)

    reports = []
    monkeypatch.setattr(cli, 'run_acceptance', acceptance)
    monkeypatch.setattr(cli.subprocess, 'run', comparison)
    monkeypatch.setattr(cli, 'write_report', lambda output, report, rows: reports.append(report))
    monkeypatch.setattr(cli, 'source_context_root', lambda: tmp_path)
    monkeypatch.setattr(sys, 'argv', ['model_readiness', '--output', str(tmp_path)])
    assert cli.main() == int(changed)
    final = json.loads((tmp_path / 'source-final.json').read_text())
    assert final['protocol'] == 'FACTORLAB_RUNNING_SOURCE_V1'
    assert final['git_commit'] is None
    row = next(row for row in reports[0]['matrix'] if row['check'] == 'input_identity')
    assert row['status'] == ('FAIL' if changed else 'PASS')
