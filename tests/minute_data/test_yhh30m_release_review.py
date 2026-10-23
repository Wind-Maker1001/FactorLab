"""Release checks reject host paths and raw data without hiding test fragments."""

import json
from pathlib import Path
import subprocess

import pytest

from examples.research.review_yhh30m_release import review


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True)
    git("init", "-q")
    git("config", "user.name", "Acceptance Test")
    git("config", "user.email", "acceptance@example.invalid")
    (root / "tests").mkdir()
    vector = "C:" + chr(47) + "outside"
    (root / "tests" / "vectors.py").write_text("VECTOR = " + repr(vector) + "\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "synthetic baseline")
    return root, git("rev-parse", "HEAD").strip()


@pytest.mark.parametrize("kind", ["host_path", "unknown_path", "token", "raw_csv", "parquet"])
def test_release_rejects_actual_roots_secrets_and_raw_artifacts(repository, kind):
    root, base = repository
    if kind == "host_path":
        (root / "receipt.json").write_text(json.dumps({"p": str(Path.home() / "private.csv")}), encoding="utf-8")
    elif kind == "unknown_path":
        (root / "receipt.json").write_text(json.dumps({"p": "C:" + chr(47) + "outside"}), encoding="utf-8")
    elif kind == "token":
        (root / "receipt.json").write_text(json.dumps({"token": "ghp_" + "A" * 36}), encoding="utf-8")
    elif kind == "raw_csv":
        (root / "bars.csv").write_text("open,close\n1,2\n", encoding="utf-8")
    else:
        (root / "data.parquet").write_bytes(b"not publishable market data")
    result = review(root, base)
    assert result["status"] == "FAIL"
    assert result["blocked_findings"]


def test_relative_filename_suffix_is_not_an_absolute_host_path(repository):
    root, base = repository
    expression = 'name = "evidence/" + audit.name + ' + repr(chr(47) + "summary.json") + "\n"
    (root / "relative.py").write_text(expression, encoding="utf-8")
    (root / "inventory.csv").write_text("month,rows,parquet_sha256\n2021-01,2,abc\n", encoding="utf-8")
    assert review(root, base)["status"] == "PASS"


def test_identical_standalone_literal_is_still_checked(repository):
    root, base = repository
    suffix = chr(47) + "summary.json"
    expression = 'name = "evidence/" + audit.name + ' + repr(suffix) + "\nother = " + repr(suffix) + "\n"
    (root / "relative.py").write_text(expression, encoding="utf-8")
    assert review(root, base)["status"] == "FAIL"


def test_only_historical_public_test_case_vector_is_permitted(repository):
    root, base = repository
    path = root / "docs/research/evidence/yhh30m-conversion-v1/verification.json"
    path.parent.mkdir(parents=True)
    case = "tests.research.synthetic::test_path[C:" + chr(47) + "outside]"
    path.write_text(json.dumps({"resolved_case_statuses": {case: "PASS"}}), encoding="utf-8")
    result = review(root, base)
    assert result["status"] == "PASS"
    assert result["historical_test_vectors_defined_in_main"]
