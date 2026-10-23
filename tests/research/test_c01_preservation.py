"""Executable C01 refusals under Solo governance. NO LIVE MONEY."""

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from qbot.research.data.evidence import json_bytes, sha256
from qbot.research.data.market_qualification import (
    load_qualification, publish_qualification, qualify_market_data,
)
from qbot.research.errors import DataQualityError
from tests.research.market_data_fixture import qualification_inputs


ROOT = Path(__file__).resolve().parents[2]


def controller_at(root):
    spec = importlib.util.spec_from_file_location(
        "c01_test_controller", root / ".github/scripts/ci/research_checks.py")
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    return controller


@pytest.fixture(scope="module")
def control():
    return controller_at(ROOT).load_security_revision(ROOT)


@pytest.fixture
def candidate(tmp_path, control):
    root = tmp_path / "candidate"
    subprocess.run(["git", "clone", "--shared", "--no-checkout", str(ROOT), str(root)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    entries = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=ROOT)
    subprocess.run(["git", "update-index", "-z", "--index-info"],
                   input=entries, cwd=root, check=True)
    for name in control.PRESERVATION_FILES | {".gitattributes"}:
        source = ROOT / name
        if source.is_file():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    return root


@pytest.mark.parametrize("version", ["UNKNOWN", "", "main", "HEAD", "LEGACY_MAIN_V1"])
def test_unknown_version_ref_and_fallback_are_rejected(control, version):
    with pytest.raises(ValueError, match="Unregistered C01 version"):
        control.validate_preservation(ROOT, version)


@pytest.mark.parametrize("name", [
    "qbot/research/data/market_qualification.py",
    "qbot/research/factors/registry.py",
    "docs/research/evidence/ci-runtime-optimization-v1/baseline-collection.json",
    "docs/research/evidence/pit-phase1-20261018-v1/delivery-assembly-receipt.json",
    "tests/research/test_five_year_frozen_baseline.py",
])
def test_unregistered_source_receipt_and_test_changes_are_rejected(candidate, control, name):
    path = candidate / name
    assert path.is_file()
    body = path.read_bytes()
    if name.endswith("test_five_year_frozen_baseline.py"):
        assert b'assert result["unchanged_financial_and_other_files"] == 460' in body
        body = body.replace(b'assert result["unchanged_financial_and_other_files"] == 460',
                            b'assert True  # conceal preservation failure')
    else:
        body += b"\n# unauthorized preservation change\n"
    path.write_bytes(body)
    with pytest.raises(ValueError, match="Unregistered security admission bytes"):
        control.validate_preservation(candidate)


def test_old_golden_change_is_rejected(candidate, control):
    golden = next(path for path in sorted(control.PRESERVATION_FILES)
                  if "golden" in path.lower() and (candidate / path).is_file())
    path = candidate / golden
    path.write_bytes(path.read_bytes() + b"unauthorized\n")
    with pytest.raises(ValueError, match="Unregistered security admission bytes"):
        control.validate_preservation(candidate)


def test_unregistered_new_protected_file_is_rejected(candidate, control):
    path = candidate / "qbot/research/unregistered_control.py"
    path.write_text("# unauthorized addition\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unregistered C01 protected addition"):
        control.validate_preservation(candidate)
    path.unlink()
    # The original 470 also include files outside the research directories.
    original_attributes = candidate / ".gitattributes"
    original_attributes.write_bytes(original_attributes.read_bytes() + b"\n# unregistered freeze change\n")
    with pytest.raises(ValueError, match="Unregistered security admission bytes"):
        control.validate_preservation(candidate)


@pytest.mark.parametrize("field,value", [
    ("source_commit", "0" * 40), ("source_commit", "refs/heads/main"),
    ("version", "UNKNOWN"), ("migration_postimages", {"qbot/research/**": {}}),
    ("financial_recertification", True), ("approved", True),
])
def test_registry_claims_cannot_authorize_changed_content(candidate, control, field, value):
    path = candidate / control.REGISTRY
    registry = json.loads(path.read_bytes())
    registry[field] = value
    path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(ValueError, match="registry digest changed"):
        control.validate_preservation(candidate)


@pytest.mark.parametrize("name", [
    ".github/scripts/ci/research_checks.py",
    ".github/scripts/ci/security_admission_revision.py",
    ".github/scripts/ci/c01_preservation.py",
    pytest.param("docs/research/evidence/c01-status-v1/trust-anchor.json", id="docs/research/evidence/c01-solo-v1/trust-anchor.json"),
    ".github/workflows/market-admission.yml",
])
def test_controls_are_rechecked_after_module_load(candidate, control, name):
    path = candidate / name
    path.write_bytes(path.read_bytes() + b"\n# unauthorized control\n")
    with pytest.raises(ValueError, match="C01 control|C01 workflow"):
        control.validate_preservation(candidate)


@pytest.mark.parametrize("name", [
    ".github/scripts/ci/research_checks.py",
    pytest.param("docs/research/evidence/c01-status-v1/trust-anchor.json", id="docs/research/evidence/c01-solo-v1/trust-anchor.json"),
])
def test_legacy_current_control_mixing_is_rejected(candidate, control, name):
    original = name if name.endswith("research_checks.py") else "docs/research/evidence/unified-market-admission-v1/trust-anchor.json"
    raw = subprocess.check_output(["git", "show", control.LEGACY + ":" + original], cwd=candidate)
    (candidate / name).write_bytes(raw)
    with pytest.raises(ValueError):
        control.validate_preservation(candidate)
    # A trusted current loader must also refuse before candidate code executes.
    with pytest.raises(ValueError, match="Independent security checker pin mismatch"):
        controller_at(ROOT).load_security_revision(candidate)


@pytest.fixture
def package(tmp_path):
    inputs, keys, dates, stocks, benchmark = qualification_inputs(tmp_path)
    path = publish_qualification(inputs, keys, dates, stocks=stocks, benchmark=benchmark,
                                 output_root=tmp_path / "qualification")
    return path, inputs, keys, dates, stocks, benchmark


@pytest.mark.parametrize("mutation", ["legacy_identity", "PASS", "execution", "financial", "version"])
def test_self_rehashed_package_cannot_select_version_or_upgrade_evidence(package, mutation):
    path, _, _, _, stocks, benchmark = package
    payload = json.loads(path.read_bytes())
    if mutation == "legacy_identity":
        # Independently reproduced original-main Windows identity. This test
        # does not claim any historical financial package was issued with it.
        payload["report"]["implementation_sha256"] = "097ed2c9f375ad6ae64793fb86f12b1bd0d7b37b1f07bd97554f0e47d1774c52"
    elif mutation == "PASS":
        payload["report"]["stages"]["SOURCE_BINDING"] = "PASS"
    elif mutation == "execution":
        payload["report"]["execution_admission"] = True
    elif mutation == "financial":
        payload["report"]["formal_admission"] = True
        payload["report"]["synthetic_only"] = False
    else:
        payload["version"] = "LEGACY_MAIN_V1"
    payload["generation"] = sha256(json_bytes({k: v for k, v in payload.items() if k != "generation"}))
    path.write_bytes(json_bytes(payload))
    with pytest.raises(DataQualityError):
        load_qualification(path, stocks=stocks, benchmark=benchmark)


def test_missing_pit_does_not_gain_admission_after_version_registration(package, control):
    _, inputs, keys, dates, stocks, benchmark = package
    assert control.validate_preservation(ROOT)["financial_recertification"] is False
    incomplete = replace(inputs, membership=inputs.membership.iloc[:-1],
                         research_start=dates[0].date().isoformat(),
                         research_end=dates[-1].date().isoformat())
    report = qualify_market_data(incomplete, keys, dates, stocks=stocks, benchmark=benchmark)
    assert report.stages["PIT"] == "INCOMPLETE"
    assert not report.formal_admission and not report.execution_admission
