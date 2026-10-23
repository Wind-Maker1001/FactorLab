"""V1 attack contracts. Every negative test reaches an explicit refusal."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fixed_factorlab_verifier", ROOT / "tools/security/fixed_validator.py")
fixed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixed)


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / "candidate"
    for directory in fixed.DIRECTORIES:
        shutil.copytree(ROOT / directory, root / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    for name in fixed.TOP_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    anchor = {"schema": fixed.PROTOCOL, "version": 1, "project": "FactorLab",
              "baseline": "98b133841a5cd5ecffcf715e8626b41db2c5b8b6", "approval": None,
              "files": fixed.inventory(root)}
    body = (json.dumps(anchor, sort_keys=True) + "\n").encode()
    target = root / fixed.ANCHOR
    target.parent.mkdir(parents=True)
    target.write_bytes(body)
    trusted = tmp_path / "external-anchor.json"
    trusted.write_bytes(body)
    return root, trusted, hashlib.sha256(body).hexdigest()


def test_integrity_is_never_an_approval(candidate):
    result = fixed.validate(*candidate)
    assert result["integrity"] == "PASS"
    assert result["authorization"] == result["independent_review"] == "BLOCKED"
    assert result["formal_admission"] is result["execution_admission"] is False


@pytest.mark.parametrize("name", ["src/factorlab/data/admission.py", "src/factorlab/data/market_qualification.py",
    "src/factorlab/cli.py", "src/factorlab/_identity.py", "src/factorlab/security/__init__.py",
    "src/factorlab/resources/csi500_5y.yaml", "tests/research/test_factor_metrics_golden.py",
    "pyproject.toml", "MANIFEST.in", ".gitattributes", "LICENSE", ".github/workflows/factorlab.yml",
    "tools/security/fixed_validator.py", "tools/ci/run_checks.py"])
def test_source_config_test_and_control_tampering_rejected(candidate, name):
    root, _, _ = candidate
    p = root / name
    p.write_bytes(p.read_bytes() + b"\n# tampered\n")
    with pytest.raises(fixed.SecurityViolation, match="SOURCE_IDENTITY_CHANGED"):
        fixed.validate(*candidate)


@pytest.mark.parametrize("path", ["../outside", "/tmp/x", "C:/x", "a\\b", "a/./b", "a//b", "a/../b", "a. /b", "NUL/x"])
def test_noncanonical_rule_paths_rejected(path):
    with pytest.raises(fixed.SecurityViolation, match="NONCANONICAL_PATH"):
        fixed.canonical(path)


@pytest.mark.parametrize("mutation", ["append", "execute", "delete"])
def test_changed_checker_rejected_without_candidate_execution(candidate, mutation):
    root, _, _ = candidate
    p = root / "tools/security/fixed_validator.py"
    sentinel = root / "UNTRUSTED_EXECUTED"
    if mutation == "delete":
        p.unlink()
    elif mutation == "execute":
        p.write_text("from pathlib import Path\nPath(" + repr(str(sentinel)) + ").touch()\n")
    else:
        p.write_bytes(p.read_bytes() + b"\n# changed\n")
    with pytest.raises(fixed.SecurityViolation, match="SOURCE_(SCOPE|IDENTITY)_CHANGED"):
        fixed.validate(*candidate)
    assert not sentinel.exists()


@pytest.mark.parametrize("mutation", ["controller", "anchor", "joint_reanchor"])
def test_resealing_independent_controls_never_authorizes(candidate, mutation):
    root, _, _ = candidate
    p = root / "tools/security/fixed_validator.py"
    p.write_bytes(p.read_bytes() + b"\n# self-approved checker\n")
    if mutation != "controller":
        path = root / fixed.ANCHOR
        value = json.loads(path.read_bytes())
        value["files"]["tools/security/fixed_validator.py"] = hashlib.sha256(p.read_bytes()).hexdigest()
        path.write_text(json.dumps(value))
        if mutation == "joint_reanchor":
            (root / ".github/workflows/factorlab.yml").write_text("SELF_APPROVED_PIN: " + hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(fixed.SecurityViolation, match="CANDIDATE_ANCHOR_CHANGED|SOURCE_IDENTITY_CHANGED"):
        fixed.validate(*candidate)


def test_checker_and_receipt_joint_self_authorization_rejected(candidate):
    root, _, _ = candidate
    p = root / "tools/security/fixed_validator.py"
    p.write_text("def validate(*a): return {'authorization':'PASS'}")
    (root / "REPACKAGED_RECEIPT.json").write_text('{"PASS":true,"approved":true}')
    with pytest.raises(fixed.SecurityViolation, match="SOURCE_IDENTITY_CHANGED"):
        fixed.validate(*candidate)


@pytest.mark.parametrize("field,value", [("version", 0), ("version", "HEAD"), ("project", "Qbot"),
    ("schema", "FACTORLAB_C01_SOLO_CONTROL_ANCHOR_V1"), ("baseline", "0" * 40),
    ("approval", {"approved": True, "reviewer": "myself"})])
def test_version_origin_and_approval_claims_rejected_even_with_new_local_pin(candidate, field, value):
    root, trusted, _ = candidate
    path = root / fixed.ANCHOR
    payload = json.loads(path.read_bytes())
    payload[field] = value
    raw = json.dumps(payload).encode()
    path.write_bytes(raw)
    trusted.write_bytes(raw)
    with pytest.raises(fixed.SecurityViolation, match="UNSUPPORTED_TRUST_CONTRACT"):
        fixed.validate(root, trusted, hashlib.sha256(raw).hexdigest())


def test_rehashed_source_and_locally_reissued_anchor_still_blocked(candidate):
    root, trusted, _ = candidate
    p = root / "src/factorlab/cli.py"
    p.write_bytes(p.read_bytes() + b"\n# local change\n")
    anchor = json.loads(trusted.read_bytes())
    anchor["files"] = fixed.inventory(root)
    raw = json.dumps(anchor).encode()
    trusted.write_bytes(raw)
    (root / fixed.ANCHOR).write_bytes(raw)
    result = fixed.validate(root, trusted, hashlib.sha256(raw).hexdigest())
    assert result["authorization"] == "BLOCKED" and result["formal_admission"] is False


@pytest.mark.parametrize("change", ["addition", "deletion", "rename"])
def test_closed_source_scope(candidate, change):
    root, _, _ = candidate
    p = root / "src/factorlab/security/__init__.py"
    if change == "addition":
        (p.parent / "unregistered.py").write_text("# unauthorized")
    elif change == "deletion":
        p.unlink()
    else:
        p.rename(p.with_name("renamed.py"))
    with pytest.raises(fixed.SecurityViolation, match="SOURCE_SCOPE_CHANGED"):
        fixed.validate(*candidate)


@pytest.mark.parametrize("pin", ["", "PASS", "0" * 64])
def test_no_pin_or_forged_pin_rejected(candidate, pin):
    root, trusted, _ = candidate
    with pytest.raises(fixed.SecurityViolation, match="EXTERNAL_.*PIN"):
        fixed.validate(root, trusted, pin)


def test_hardlink_cannot_alias_qualified_source(candidate):
    root, _, _ = candidate
    p = root / "src/factorlab/security/__init__.py"
    (root / "alias.py").hardlink_to(p)
    with pytest.raises(fixed.SecurityViolation, match="NOT_SINGLE_LINK_REGULAR_FILE"):
        fixed.validate(*candidate)


def test_duplicate_json_keys_rejected():
    with pytest.raises(fixed.SecurityViolation, match="DUPLICATE_JSON_KEY"):
        fixed.strict_json(b'{"approval":null,"approval":true}')


def test_symlink_source_path_is_rejected(candidate):
    root, _, _ = candidate
    p = root / "src/factorlab/security/__init__.py"
    real = root / "original-security.py"
    p.rename(real)
    p.symlink_to(real)
    with pytest.raises(fixed.SecurityViolation, match="LINK_OR_REPARSE_PATH"):
        fixed.validate(*candidate)


def test_git_environment_and_repackaged_receipts_cannot_change_result(candidate, monkeypatch):
    root, _, _ = candidate
    monkeypatch.setenv("GIT_DIR", str(root / "fake-git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(root / "fake-source"))
    monkeypatch.setenv("FACTORLAB_TRUST_ANCHOR_SHA256", "0" * 64)
    (root / "APPROVAL.json").write_text('{"approved":true,"formal_admission":true,"status":"PASS"}')
    result = fixed.validate(*candidate)
    assert result["authorization"] == "BLOCKED" and result["formal_admission"] is False


def test_runtime_has_no_local_approval_path(monkeypatch):
    from factorlab.security import assert_independent_admission, REQUIRED_STAGES
    from factorlab.errors import ProviderError
    assert REQUIRED_STAGES == {"RAW", "ADJUSTMENT", "STATUS", "PIT", "SOURCE_BINDING", "FORMAL"}
    monkeypatch.setenv("FACTORLAB_APPROVED", "PASS")
    with pytest.raises(ProviderError, match="V1_HAS_NO_INDEPENDENT_APPROVAL_MECHANISM"):
        assert_independent_admission()
