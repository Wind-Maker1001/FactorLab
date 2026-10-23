"""P0 authorization changes exact bytes, never the historical test outcomes."""

import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "independent_admission_control", ROOT / ".github/scripts/ci/research_checks.py"
)
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)
security = controller.load_security_revision(ROOT)


@pytest.fixture
def revision_clone(tmp_path):
    root = tmp_path / "clone"
    subprocess.check_call(
        ["git", "clone", "--shared", "--no-checkout", str(ROOT), str(root)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    entries = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=ROOT)
    subprocess.run(["git", "update-index", "-z", "--index-info"],
                   input=entries, cwd=root, check=True)
    for name in security.PRESERVATION_FILES | {security.RECEIPT, ".gitattributes", controller.SECURITY_ANCHOR, controller.SECURITY_WORKFLOW} | set(controller.SECURITY_CONTROL_FILES):
        source = ROOT / name
        if source.is_file():
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    return root


def test_revision_preserves_all_legacy_collection_and_outcome_requirements():
    expected = json.loads((ROOT / "docs/research/evidence/ci-runtime-optimization-v1/baseline-collection.json").read_text())
    original = copy.deepcopy(expected)
    adjusted = security.coverage_contract(ROOT, expected)
    assert expected == original
    assert adjusted.keys() == expected.keys()
    collection_keys = {part + suffix for part in ("all_ids", "selected_ids", "deselected_ids")
                       for suffix in ("_count", "_sha256")}
    for key in expected.keys() - {"execution_by_platform"} - collection_keys:
        assert adjusted[key] == expected[key]
    delta = security.read_registry(ROOT)["execution_delta"]
    for part in ("all_ids", "selected_ids", "deselected_ids"):
        assert adjusted[part + "_count"] == expected[part + "_count"] + len(delta["added"][part])
    for family, contract in expected["execution_by_platform"].items():
        for key in contract.keys() - {"coverage_valid_lines", "outcomes_sha256"}:
            assert adjusted["execution_by_platform"][family][key] == contract[key]
        current = adjusted["execution_by_platform"][family]["coverage_valid_lines"]
        for name in set(contract["coverage_valid_lines"]) - security.COVERAGE_FILES - set(security.read_registry(ROOT)["coverage_valid_lines"]):
            assert current[name] == contract["coverage_valid_lines"][name]
    assert security.load_revision(ROOT)["base_commit"] == security.BASE


@pytest.mark.parametrize("name", sorted(security.FILES))
def test_every_authorized_file_still_rejects_unregistered_bytes(revision_clone, name):
    path = revision_clone / name
    path.write_bytes(path.read_bytes() + b"\n# unregistered security change\n")
    with pytest.raises(ValueError, match="Unregistered security admission bytes"):
        security.load_revision(revision_clone)


@pytest.mark.parametrize("mutation", ["source_scope", "coverage", "base", "postimage"])
def test_rewriting_the_receipt_cannot_self_authorize_changes(revision_clone, mutation):
    path = revision_clone / security.RECEIPT
    payload = json.loads(path.read_bytes())
    if mutation == "source_scope":
        payload["files"]["qbot/research/factors/registry.py"] = {}
    elif mutation == "coverage":
        payload["coverage_valid_lines"] = {}
    elif mutation == "base":
        payload["base_commit"] = "0" * 40
    else:
        payload["files"]["qbot/research/cli.py"]["approved_blob"] = "0" * 40
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="receipt digest changed"):
        security.load_revision(revision_clone)


@pytest.mark.parametrize("name", ["qbot/research/cli.py", "qbot/research/data/providers/base.py", "qbot/research/data/providers/local.py"])
def test_archival_view_requires_exact_current_security_and_original_preimages(revision_clone, tmp_path, name):
    release = "docs/research/evidence/audit-pit-release-20261018-v1/release-manifest.json"
    destination = revision_clone / release
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / release, destination)
    snapshots = security.historical_protected_view(revision_clone, tmp_path / "originals")
    import hashlib
    expected = {item["path"]: item["normalized_text_sha256"]
                for item in json.loads(destination.read_bytes())["protected_contracts"]}
    assert set(snapshots) == {"qbot/research/cli.py", "qbot/research/data/providers/base.py", "qbot/research/data/providers/local.py", "qbot/research/data/market_qualification.py"}
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == expected[key]
               for key, path in snapshots.items())
    current = revision_clone / name
    current.write_bytes(current.read_bytes() + b"\n# unauthorized current change\n")
    with pytest.raises(ValueError, match="Unregistered security admission bytes"):
        security.historical_protected_view(revision_clone, tmp_path / "untrusted")
