"""Offline safeguard: this data audit must not rewrite prior research evidence."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
FREEZE = ROOT / "docs/research/evidence/five-year-data-feasibility-v1/frozen-baseline.json"


def test_original_research_evidence_and_factor_parameters_are_frozen(tmp_path):
    frozen = json.loads(FREEZE.read_text(encoding="utf-8"))
    baseline = frozen["baseline_commit"]
    assert baseline == "6864315bc8eeaf96356d6822d12f431ec485a94e"
    assert len(frozen["factors"]) == 14 and frozen["unique_candidate_expressions"] == 39
    assert frozen["candidate_configuration"]["max_panel_rows"] == 250000
    entries = frozen["files"]
    assert len(entries) == frozen["frozen_file_count"] == 470
    protected = {item["path"] for item in entries}
    tree = subprocess.check_output(["git", "ls-tree", "-r", baseline], cwd=ROOT, encoding="utf-8")
    blobs = {line.split("\t", 1)[1]: line.split("\t", 1)[0].split()[2] for line in tree.splitlines()}
    for item in entries:
        assert blobs[item["path"]] == item["baseline_git_blob_sha1"]
    changed = subprocess.check_output(["git", "-c", "core.quotepath=false", "diff", "--no-renames", "--name-only", baseline],
                                      cwd=ROOT, encoding="utf-8").splitlines()
    # Preserve the original historical freeze. Only four explicitly authorized
    # engineering files accept exact, independently reviewed Git postimages.
    scripts = ROOT / ".github/scripts/ci"
    specification = importlib.util.spec_from_file_location(
        "ci_exact_engineering_migration", scripts / "frozen_engineering_migration.py"
    )
    migration = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(migration)
    result = migration.validate_migration(
        ROOT, frozen, FREEZE.relative_to(ROOT).as_posix(),
        "e98d621ebdbc8d619de22390502ec07a79d825a3",
        ROOT / "docs/research/evidence/ci-runtime-optimization-v1/engineering-migration.json",
        "d08e34e905db5b0dee4750220cc5aff5b46b966796b2b554f74c5e5e2347ab9e",
    )
    security_paths = {
        "qbot/research/cli.py", "qbot/research/data/providers/base.py",
        "qbot/research/data/providers/local.py", "tests/research/test_cli_phase2.py",
        "tests/research/test_cli_forward_evaluation.py",
        "tests/research/test_phase1_file_pipeline.py",
        "qbot/research/data/market_qualification.py",
    }
    assert set(result["exact_security_postimages"]) == security_paths
    assert result["unchanged_financial_and_other_files"] == 460
    assert protected.intersection(changed) == migration.ENGINEERING_PATHS | security_paths
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(scripts) + os.pathsep + environment.get("PYTHONPATH", "")
    subprocess.run(
        [sys.executable, "-m", "unittest", "test_trigger_policy", "test_ci_contracts.WorkflowTests"],
        cwd=ROOT, env=environment, check=True,
    )
    subprocess.run(
        [sys.executable, str(scripts / "validate_fixture_equivalence.py"),
         "--contract-root", str(ROOT / "docs/research/evidence/ci-runtime-optimization-v1"),
         "--output", str(tmp_path / "engineering-fixture-equivalence.json")],
        cwd=ROOT, env=environment, check=True,
    )
