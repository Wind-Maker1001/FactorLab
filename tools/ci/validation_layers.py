"""Exact historical selection for current CI; full regression never loads this plugin."""
import csv
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "docs/ci/CI_BASELINE_FAILURE_MATRIX.csv"


def historical_nodes():
    with MATRIX.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    nodes = {row["test_id"] for row in rows}
    if len(rows) != 186 or len(nodes) != 186:
        raise pytest.UsageError("historical matrix must retain exactly 186 unique records")
    return nodes


def pytest_addoption(parser):
    parser.addoption("--current-layer-inventory", type=Path)


def pytest_ignore_collect(collection_path, config):
    relative = collection_path.relative_to(ROOT).as_posix()
    # Only historical *collection errors* are excluded as whole files.
    return relative in {node for node in historical_nodes() if "::" not in node}


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    historical = historical_nodes()
    selected, archive = [], []
    for item in items:
        (archive if item.nodeid in historical else selected).append(item)
    observed = {item.nodeid for item in archive}
    expected = {node for node in historical if "::" in node}
    if observed != expected:
        raise pytest.UsageError("historical selection drift: missing=%s extra=%s" %
                                (sorted(expected - observed), sorted(observed - expected)))
    # Disjoint, exhaustive selection: every collected nonhistorical test runs.
    items[:] = selected
    config.hook.pytest_deselected(items=archive)
    destination = config.getoption("--current-layer-inventory")
    if destination:
        destination.write_text(json.dumps(dict(
            schema="FACTORLAB_VALIDATION_LAYER_INVENTORY_V1",
            current=[item.nodeid for item in selected],
            historical_collected=sorted(observed),
            historical_collection_errors=sorted(historical - expected),
            full_regression_selection_changed=False), indent=2) + "\n", encoding="utf-8")
