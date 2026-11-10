"""Unified orchestration checks against the existing offline research engines."""
from copy import deepcopy
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from factorlab.dataset.identity import file_hash
from factorlab.experiments import ExperimentTracker
from factorlab.errors import FactorLabError
from factorlab.research.config import EXECUTION, BOUNDARY
from tests.research.workflow_fixtures import ROOT, research_inputs, write_json


@pytest.fixture
def workflow_case(tmp_path):
    inputs = research_inputs(tmp_path)
    template = json.loads((ROOT / "configs/research/p5-r02-601857-2018.json").read_text())
    common = dict(initial_cash=100000.0, max_weight=1.0, board_lot=100)
    base = dict(schema_version=1, task="custom_daily", execution=deepcopy(EXECUTION), boundary=deepcopy(BOUNDARY), data=dict(id="synthetic-workflow-seam",
        manifest_sha256=inputs["manifest_hash"], source_kind="SYNTHETIC"), fees=template["fees"])
    custom = dict(symbol="601857.SH", start="2018-01-01", end="2018-04-30",
        warmup_start="2017-09-01", **common,
        strategy={"class": "MyStrategy", "parameters": {"window": 5}, "sha256": file_hash(inputs["strategy"])})
    config = write_json(tmp_path / "custom.json", dict(**base, research=custom))
    window = dict(symbol="601857.SH", industry="energy", year=2018, start="2018-01-01",
        end="2018-04-30", warmup_start="2017-09-01", query_start="2017-09-01", query_end="2018-04-30")
    multi = write_json(tmp_path / "multi.json", dict(base, task="p6_r01", research=dict(
        windows=[window, dict(window, symbol="601398.SH", industry="bank")], common=common, strategies=template["strategies"], signal_price_column="raw_close")))
    runtime = write_json(tmp_path / "runtime.json", dict(research_root=str(inputs["research"]),
        source_root=str(inputs["source"]), strategy_file=str(inputs["strategy"])))
    multi_runtime = write_json(tmp_path / "multi-runtime.json", dict(research_root=str(inputs["research"]), source_root=str(inputs["source"])))
    return dict(config=config, multi=multi, runtime=runtime, multi_runtime=multi_runtime,
        store=tmp_path / "tracker", inputs=inputs, root=tmp_path)


def execute(case, *, task="custom", run_id="r1", experiment_id="e1", **kwargs):
    from factorlab.research.workflow import run
    return run(case["config" if task == "custom" else "multi"],
        case["runtime" if task == "custom" else "multi_runtime"],
        output=case["root"] / run_id, store=case["store"], experiment_id=experiment_id,
        run_id=run_id, **kwargs)


def test_two_engines_inspect_compare_verify_and_replay(workflow_case, monkeypatch):
    from factorlab.research.workflow import inspect, compare, verify, replay
    c = workflow_case
    def refuse_network(*args, **kwargs):
        raise AssertionError("offline research attempted network access")
    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    first = execute(c)
    assert first["payload"]["run"]["status"] == "SUCCEEDED"
    second = execute(c, run_id="r2", experiment_id="e2")
    assert second["payload"]["run"]["status"] == "SUCCEEDED"
    assert inspect(c["store"], "r1")
    assert compare(c["store"], ["r1", "r2"])["comparable"]
    assert verify(c["store"], "r1", runtime_path=c["runtime"])
    repeated = replay(c["store"], "r1", runtime_path=c["runtime"], output=c["root"] / "replay", new_run_id="r3")
    assert repeated
    multi = execute(c, task="multi", run_id="multi", experiment_id="multi")
    assert multi["payload"]["run"]["status"] == "SUCCEEDED"
    with pytest.raises(ValueError):
        compare(c["store"], ["r1", "multi"])
    reports = list((c["root"] / "multi").rglob("*.md"))
    assert reports
    report = reports[0].read_text(encoding="utf-8")
    for boundary in ("NO LIVE MONEY", "PIT_UNVERIFIED", "HFQ_UNVERIFIED", "FORMAL_ADMISSION_BLOCKED", "TOTAL_RETURN_UNAVAILABLE"):
        assert boundary in report
    assert "null" in report
    tracker = ExperimentTracker(c["store"])
    original = tracker.get_run("r1")
    with pytest.raises(FileExistsError):
        execute(c)
    assert tracker.get_run("r1") == original


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(unknown=True),
    lambda c: c.update(task="unknown"),
    lambda c: c["data"].update(local_path="C:/data"),
    lambda c: c["research"].update(allow_short=True),
    lambda c: c["fees"].update(typo_bps=1),
    lambda c: c.update(schema_version=True),
    lambda c: c["boundary"].update(formal_admission=0),
    lambda c: c["execution"].update(long_only=1),
    lambda c: c["boundary"].update(formal_admission=True),
])
def test_strict_config(workflow_case, mutation):
    from factorlab.research.workflow import load_config
    path = workflow_case["config"]
    config = json.loads(path.read_text())
    mutation(config)
    write_json(path, config)
    with pytest.raises(ValueError):
        load_config(path)


def test_input_and_artifact_drift(workflow_case):
    from factorlab.research.workflow import verify
    c = workflow_case
    execute(c)
    manifest = c["inputs"]["source"] / "manifest.json"
    original = manifest.read_bytes()
    manifest.write_bytes(original + b"\n")
    with pytest.raises((ValueError, FactorLabError)):
        verify(c["store"], "r1", runtime_path=c["runtime"])
    manifest.write_bytes(original)
    artifacts = ExperimentTracker(c["store"]).get_run("r1")["payload"]["run"]["artifacts"]
    artifact = Path(next(iter(artifacts.values()))["path"])
    artifact.write_bytes(artifact.read_bytes() + b"\n")
    with pytest.raises(ValueError):
        verify(c["store"], "r1", runtime_path=c["runtime"])


def test_data_unavailable_and_execution_failure_are_distinct(workflow_case):
    c = workflow_case
    runtime = json.loads(c["runtime"].read_text())
    runtime["source_root"] = str(c["root"] / "missing-private-source")
    write_json(c["runtime"], runtime)
    result = execute(c)
    payload = result["payload"]["run"]
    assert payload["status"] != "SUCCEEDED"
    assert "DATA_UNAVAILABLE" in json.dumps(payload["validation"])
    assert not payload["portfolio_metrics"]
    runtime["source_root"] = str(c["inputs"]["source"])
    write_json(c["runtime"], runtime)
    strategy = c["inputs"]["strategy"]
    strategy.write_text('''class MyStrategy:
    required_fields = ('adj_close',)
    warmup_sessions = 5
    def __init__(self, window: int = 5):
        pass
    def generate_targets(self, bars, calendar):
        raise RuntimeError('deliberate strategy failure')
''', encoding="utf-8")
    config = json.loads(c["config"].read_text())
    config["research"]["strategy"]["sha256"] = file_hash(strategy)
    write_json(c["config"], config)
    failed = execute(c, run_id="failure", experiment_id="failure")["payload"]["run"]
    assert failed["status"] == "FAILED"
    assert "EXECUTION_FAILED" in json.dumps(failed["validation"])
    assert "deliberate strategy failure" in json.dumps(failed["validation"])


def test_cli_operates_saved_experiment(workflow_case):
    c = workflow_case
    cli = [sys.executable, '-I', str(ROOT / 'tools/research/workflow.py')]
    def invoke(*arguments, expected=0):
        process = subprocess.run(cli + list(map(str, arguments)), capture_output=True, text=True)
        assert process.returncode == expected, process.stderr
        return json.loads(process.stdout or process.stderr)
    for name, config, runtime in [('r1', c['config'], c['runtime']), ('r2', c['config'], c['runtime']),
                                  ('multi', c['multi'], c['multi_runtime'])]:
        result = invoke('run', '--config', config, '--runtime', runtime, '--output', c['root'] / name,
                        '--store', c['store'], '--experiment-id', name, '--run-id', name)
        assert result['status'] == 'SUCCEEDED'
    assert invoke('inspect', '--store', c['store'], '--run-id', 'r1')['status'] == 'SUCCEEDED'
    assert invoke('compare', '--store', c['store'], '--run-ids', 'r1', 'r2')['comparable']
    assert invoke('verify', '--store', c['store'], '--run-id', 'r1')['verified']
    assert invoke('replay', '--store', c['store'], '--run-id', 'r1', '--runtime', c['runtime'],
                  '--output', c['root'] / 'cli-replay', '--new-run-id', 'cli-replay')['verified']
    assert invoke('compare', '--store', c['store'], '--run-ids', 'r1', 'multi', expected=2)['status'] == 'REJECTED'


def test_replay_mismatch_is_failed_before_terminal_publication(workflow_case, monkeypatch):
    import pandas as pd
    from factorlab.research import workflow
    c = workflow_case
    execute(c)
    original = workflow._execute
    def changed_execution(config, runtime, samples, output):
        metrics = original(config, runtime, samples, output)
        path = output / 'strategy_nav.parquet'
        nav = pd.read_parquet(path)
        nav.loc[0, 'strategy_nav'] += .01
        nav.to_parquet(path, index=False)
        return metrics
    monkeypatch.setattr(workflow, '_execute', changed_execution)
    with pytest.raises(ValueError, match='replay failed'):
        workflow.replay(c['store'], 'r1', runtime_path=c['runtime'], output=c['root'] / 'mismatch', new_run_id='mismatch')
    terminal = ExperimentTracker(c['store']).get_run('mismatch')['payload']['run']
    assert terminal['status'] == 'FAILED'
    assert terminal['validation']['lifecycle']['status'] == 'REPLAY_MISMATCH'
    assert terminal['validation']['lifecycle']['stage'] == 'REPLAY_VERIFICATION'


def test_report_failure_is_not_success(workflow_case, monkeypatch):
    from factorlab.research import workflow
    def fail_report(*args, **kwargs):
        raise OSError('report storage unavailable')
    monkeypatch.setattr(workflow, '_report', fail_report)
    terminal = execute(workflow_case)['payload']['run']
    assert terminal['status'] == 'FAILED'
    assert terminal['validation']['lifecycle']['status'] == 'REPORT_FAILED'
    assert terminal['validation']['lifecycle']['unfinished_stages'] == ['REPORT']


def test_changed_strategy_and_config_rejected(workflow_case):
    from factorlab.research import workflow
    c = workflow_case
    execute(c)
    strategy = c['inputs']['strategy']
    strategy.write_text(strategy.read_text() + '\n# changed source\n', encoding='utf-8')
    with pytest.raises(ValueError, match='strategy source identity'):
        workflow.verify(c['store'], 'r1', runtime_path=c['runtime'])
    rejected = execute(c, run_id='changed-input', experiment_id='changed-input')['payload']['run']
    assert rejected['status'] == 'FAILED'
    assert rejected['validation']['lifecycle']['status'] == 'INPUT_REJECTED'
    assert not rejected['portfolio_metrics']
    config = json.loads(c['config'].read_text())
    config['research']['max_weight'] = .5
    write_json(c['config'], config)
    with pytest.raises(ValueError, match='definition differs'):
        execute(c, run_id='changed-config')


def test_input_relocation_preserves_identity(workflow_case):
    import shutil
    from factorlab.research import workflow
    c = workflow_case
    execute(c)
    relocated = c['root'] / 'relocated'
    relocated.mkdir()
    shutil.copytree(c['inputs']['research'], relocated / 'research')
    shutil.copytree(c['inputs']['source'], relocated / 'source')
    shutil.copy2(c['inputs']['strategy'], relocated / 'strategy.py')
    binding = write_json(c['root'] / 'relocated.json', dict(research_root=str(relocated / 'research'),
        source_root=str(relocated / 'source'), strategy_file=str(relocated / 'strategy.py')))
    assert workflow.verify(c['store'], 'r1', runtime_path=binding)['verified']
    assert workflow.replay(c['store'], 'r1', runtime_path=binding, output=c['root'] / 'relocated-replay', new_run_id='relocated')['verified']


def test_negative_research_is_completed_not_failed(workflow_case):
    c = workflow_case
    config = json.loads(c['config'].read_text())
    config['fees']['commission_bps'] = 5000.0
    write_json(c['config'], config)
    terminal = execute(c)['payload']['run']
    assert terminal['status'] == 'SUCCEEDED'
    assert terminal['validation']['lifecycle']['status'] == 'COMPLETED'
    returns = [v for k, v in terminal['portfolio_metrics'].items() if k.endswith(':cumulative_return')]
    assert any(v < 0 for v in returns)
    assert not terminal['validation']['lifecycle']['unfinished_stages']
