import hashlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pandas as pd
import pytest
from setuptools import find_packages

from qbot.research.exploratory.market_structure.contracts import BASELINE
from qbot.research.exploratory.market_structure.runner import (
    run_synthetic,
    verify_receipt,
)
from qbot.research.exploratory.market_structure.contracts import ContractError

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "qbot/research/exploratory/market_structure"


@pytest.fixture(scope="module")
def delivered(tmp_path_factory):
    destination = tmp_path_factory.mktemp("market-structure") / "demo"
    receipt, values = run_synthetic(destination)
    return destination, receipt, values


def test_full_three_frequency_synthetic_run_and_all_contract_states(delivered):
    directory, receipt, values = delivered
    assert verify_receipt(directory) == receipt
    assert receipt["candidate_count"] == 32 and receipt["implemented_count"] == 14
    assert set(values.frequency) == {5, 15, 30} and values.feature_id.nunique() == 14
    assert values.session_id.nunique() == 6
    assert receipt["real_research_status"] == "REAL_RESEARCH_BLOCKED"
    assert receipt["software_status"] == "SYNTHETIC_DEMO_COMPLETED"
    assert (
        receipt["formal_admission"]
        is receipt["forward_allowed"]
        is receipt["live_allowed"]
        is False
    )
    states = pd.read_csv(directory / "contract_status.csv")
    assert len(states) == 32
    assert states.implementation_status.value_counts().to_dict() == {
        "CONTRACT_ONLY": 18,
        "IMPLEMENTED_SYNTHETIC": 14,
    }
    assert states.real_source_status.eq("REAL_RESEARCH_BLOCKED").all()
    assert (
        states.query("implementation_status=='IMPLEMENTED_SYNTHETIC'")
        .finite_cells.gt(0)
        .all()
    )
    assert pd.read_csv(directory / "conservation.csv").error.abs().max() < 1e-6
    flow = pd.read_csv(directory / "flow_classification.csv")
    assert flow.unclassified_amount_ratio.max() > 0
    assert flow.incomplete_classification_count.sum() > 0
    assert flow.missing_or_blocked_count.sum() > 0
    controls = pd.read_csv(directory / "control_rejections.csv")
    assert {"CONSTANT", "NUMERIC_DUPLICATE", "HIGHLY_REDUNDANT"}.issubset(
        controls.reason
    )
    assert "STRUCTURAL_DUPLICATE" in set(
        pd.read_csv(directory / "control_structure.csv").status
    )
    assert "NO LIVE MONEY" in (directory / "summary.md").read_text(encoding="utf-8")
    for name, sha in receipt["artifacts"].items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == sha
    assert set(receipt["source_sha256"]) == {
        p.relative_to(ROOT).as_posix() for p in SOURCE.glob("*.py")
    }


def test_receipt_detects_changed_artifact_and_extra_file(delivered, tmp_path):
    directory, _, _ = delivered
    target = tmp_path / "copy"
    shutil.copytree(directory, target)
    (target / "summary.md").write_text("altered", encoding="utf-8")
    with pytest.raises(ContractError, match="hash"):
        verify_receipt(target)
    (target / "extra.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ContractError, match="inventory"):
        verify_receipt(target)


def test_source_cli_help_and_namespace_packaging_limit_are_explicit():
    assert sys.version_info[:2] == (3, 9)
    assert not (SOURCE / "__init__.py").exists()
    result = subprocess.run(
        [sys.executable, "-m", "qbot.research.exploratory.market_structure", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and "REAL_RESEARCH_BLOCKED" in result.stdout
    assert "qbot.research.exploratory.market_structure" not in find_packages(
        where=str(ROOT)
    )
    assert "research.exploratory.market_structure" not in find_packages(
        where=str(ROOT / "qbot")
    )


def test_namespace_main_dispatch_and_invalid_frequency(monkeypatch, tmp_path, capsys):
    import qbot.research.exploratory.market_structure.runner as runner

    def fake(output, frequencies, seed):
        assert frequencies == (5, 15, 30) and seed == 20261018
        return (
            dict(
                software_status="SYNTHETIC_DEMO_COMPLETED",
                real_research_status="REAL_RESEARCH_BLOCKED",
            ),
            None,
        )

    monkeypatch.setattr(runner, "run_synthetic", fake)
    monkeypatch.setattr(
        sys, "argv", ["market_structure", "--output", str(tmp_path / "cli")]
    )
    runpy.run_module("qbot.research.exploratory.market_structure", run_name="__main__")
    assert "SYNTHETIC_DEMO_COMPLETED" in capsys.readouterr().out
    # Real runner validates its own frequencies, without accepting a production mode.
    with pytest.raises(ValueError, match="frequencies"):
        run_synthetic(tmp_path / "bad", (1,))


def test_protected_tracked_content_is_unchanged():
    changed = subprocess.check_output(
        ["git", "diff", BASELINE, "--name-only", "--diff-filter=MDR"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    # The user-authorized P0 repair has exact postimages; all other historical
    # content retains the original prohibition. No blanket gate exception.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "independent_research_checks", ROOT / ".github/scripts/ci/research_checks.py"
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    security = controller.load_security_revision(ROOT)
    revision = security.load_revision(ROOT)
    assert security.read_registry(ROOT)["version"] == "STATUS_ADAPTER_SOLO_PRESERVATION_V1"
    predecessor = ROOT / "docs/research/evidence/c01-solo-v1/version-registry.json"
    assert json.loads(predecessor.read_bytes())["version"] == "CSMAR_RAW_SOLO_PRESERVATION_V2"
    approved = (set(revision["files"]) | (set(controller.SECURITY_CONTROL_FILES) - {controller.SECURITY_CHECKER_PATH})) if revision else set()
    assert set(changed) <= approved
    assert (
        json.loads(
            (
                ROOT
                / "docs/research/evidence/ci-runtime-optimization-v1/baseline-collection.json"
            ).read_text()
        )["all_ids_count"]
        == 3144
    )
