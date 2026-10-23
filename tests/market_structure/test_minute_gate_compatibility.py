"""Cross-module publication scope leaves archived evidence and gates intact."""

import json
import subprocess

import pytest

from examples.research.review_yhh30m_release import review


@pytest.fixture
def publication_repo(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.name", "Acceptance Test"),
        ("config", "user.email", "acceptance@example.invalid"),
        ("commit", "--allow-empty", "-qm", "synthetic baseline"),
    ):
        subprocess.check_call(["git", *args], cwd=root)
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    return root, base


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_scoped_scan_rejects_unaudited_non_utf8_without_rewriting(publication_repo):
    root, base = publication_repo
    name = "docs/research/market_structure/evidence/v2/red/red-tests.log"
    path = write(root, name, b"original evidence: \xa1\xa1\n")
    original = path.read_bytes()
    result = review(root, base, scope_yhh30m=True)
    assert result["status"] == "FAIL"
    assert result["blocked_findings"] == [{"file": name, "reason": "NON_UTF8_CONTENT"}]
    assert result["publication_scope"] == "YHH30M"
    assert result["out_of_scope_files"] == [name]
    assert path.read_bytes() == original


@pytest.mark.parametrize("kind", ["path", "token", "raw"])
def test_scoped_scan_still_rejects_owned_artifacts(publication_repo, kind):
    root, base = publication_repo
    if kind == "path":
        content = json.dumps({"p": "C:" + chr(47) + "outside"}).encode()
        name = "docs/research/evidence/yhh30m-test/receipt.json"
    elif kind == "token":
        content = json.dumps({"token": "ghp_" + "A" * 36}).encode()
        name = "qbot/engineering/yhh30m/receipt.json"
    else:
        content = b"synthetic raw bytes"
        name = "qbot/engineering/yhh30m/data.parquet"
    write(root, name, content)
    result = review(root, base, scope_yhh30m=True)
    assert result["status"] == "FAIL"
    assert result["blocked_findings"]


def test_global_gate_changes_cannot_hide_outside_scope(publication_repo):
    root, base = publication_repo
    name = "qbot/research/data/schema.py"
    write(root, name, b"# synthetic changed gate\n")
    result = review(root, base, scope_yhh30m=True)
    assert result["status"] == "FAIL"
    assert result["protected_gate_changes"] == [name]


def test_default_full_scan_remains_strict(publication_repo):
    root, base = publication_repo
    write(root, "unrelated.json", json.dumps({"p": "C:" + chr(47) + "outside"}).encode())
    assert review(root, base)["status"] == "FAIL"


def test_scoped_scan_includes_minute_examples_and_documentation(publication_repo):
    root, base = publication_repo
    names = [
        "examples/research/review_yhh30m_release.py",
        "tests/minute_data/new_case.py",
        "docs/research/YHH_30M_NEW.md",
        ".github/workflows/minute-data-evidence.yml",
    ]
    for name in names:
        write(root, name, b"# safe synthetic content\n")
    result = review(root, base, scope_yhh30m=True)
    assert result["status"] == "PASS"
    assert result["changed_files"] == sorted(names)
    assert result["out_of_scope_files"] == []
