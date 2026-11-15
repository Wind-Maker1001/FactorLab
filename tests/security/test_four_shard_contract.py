"""Four-shard trial boundaries and evidence attacks."""
import json
from pathlib import Path

import pytest

from tools.ci import sharding
from tests.security.test_validation_v2 import shard_count, shard_receipts, pr_shard_receipts, timings_v1


@pytest.mark.parametrize('attack', ['extra', 'count', 'counts', 'collection', 'skipped', 'missing_end'])
def test_trial_evidence_attacks(pr_shard_receipts, shard_count, attack):
    root, output, identity = pr_shard_receipts
    folder = root / '3.14' / '1' / 'current'
    if attack == 'extra':
        (root / '3.14' / str(shard_count)).mkdir()
    elif attack in ('count', 'counts'):
        path = folder / ('receipt.json' if shard_count == 4 else 'selection.json')
        data = json.loads(path.read_bytes())
        if attack == 'count' or shard_count == 3:
            data['shard_count'] = 4 if shard_count == 3 else 3
        else:
            data['counts']['passed'] = 0
        path.write_text(json.dumps(data))
    elif attack == 'collection':
        for path in (root / '3.14').glob('*/current/selection.json'):
            data = json.loads(path.read_bytes())
            data['historical_collected'] = ['different.py::test_old']
            path.write_text(json.dumps(data))
    elif attack == 'skipped':
        path = folder / 'tests.xml'
        path.write_text('<testsuite><testcase classname="tests.b" name="test_node"><skipped/></testcase></testsuite>')
    else:
        path = folder / 'execution.json'
        data = json.loads(path.read_bytes())
        data['completed'] = []
        path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        sharding.aggregate_pr(root, output, identity, shard_count)


@pytest.mark.parametrize('count', [0, 1, 2, 5, -1])
def test_only_reviewed_counts(count):
    with pytest.raises(ValueError, match='shard_count'):
        sharding.partition(['a::test'], {}, count)


def test_four_partition_is_module_unique():
    nodes = [f'tests/{module}.py::test_{i}' for module in 'abcdefgh' for i in range(3)]
    weights = {f'tests/{module}.py': i + 1 for i, module in enumerate('abcdefgh')}
    shards = sharding.partition(nodes, weights, 4)
    assert shards == sharding.partition(nodes, dict(reversed(list(weights.items()))), 4)
    assert sorted(node for shard in shards for node in shard) == sorted(nodes)
    assert all(shards)
    for module in weights:
        assert sum(any(node.startswith(module + '::') for node in shard) for shard in shards) == 1


def test_committed_synthetic_receipts_replay(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / 'docs/validation/ci-perf-04/synthetic-receipts.json'
    bundle = json.loads(source.read_bytes())
    root = tmp_path / 'inputs'
    for name, contents in bundle['artifacts'].items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding='utf-8')
    timings = tmp_path / 'timings.json'
    timings.write_text(json.dumps(timings_v1({
        f'tests/{module}.py': float(4-i) for i, module in enumerate('abcd')})))
    monkeypatch.setattr(sharding, 'TIMINGS', timings)
    output = tmp_path / 'verified'
    sharding.aggregate_pr(root, output, bundle['identity'], 4)
    for version in ('3.13', '3.14'):
        receipt = json.loads((output / version / 'receipt.json').read_bytes())
        assert receipt['counts']['passed'] == 4
        assert receipt['shard_count'] == 4


def test_trial_workflow_retains_full_defaults_and_publisher():
    root = Path(__file__).resolve().parents[2]
    workflow = (root / '.github/workflows/factorlab-pr.yml').read_text()
    full = (root / '.github/workflows/factorlab-full.yml').read_text()
    assert "vars.CI_PR_SHARD_COUNT || '3'" in workflow
    assert "4) matrix='[0,1,2,3]'" in workflow
    assert 'fromJSON(needs.risk.outputs.shards)' in workflow
    assert '--shard-count ${{ needs.risk.outputs.shard_count }}' in workflow
    assert '--shard-count "$SHARD_COUNT"' in workflow
    assert 'for ((shard=0; shard<SHARD_COUNT; shard++))' in workflow
    assert 'environment: ci-merge-publisher' in workflow
    assert 'environment: ci-control-review' in workflow
    assert "test \"$APP_ID\" = 5670895" in workflow
    assert 'permission-statuses: write' in workflow
    assert 'shard: [0, 1, 2]' in full
    assert '--shard-count' not in full
