"""Full publication safety with exact, versioned historical exceptions."""

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from examples.research import review_yhh30m_release as scanner
from tests.market_structure.test_minute_gate_compatibility import publication_repo, write  # noqa: F401


ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = scanner.LEGACY_LOG
DOCUMENT = "docs/research/market_structure/README.md"


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def save_policy(root, monkeypatch, policy):
    raw = json.dumps(policy, sort_keys=True).encode()
    write(root, scanner.PATH_RULES, raw)
    monkeypatch.setattr(scanner, "PATH_RULES_SHA256", hashlib.sha256(raw).hexdigest())


def history(root, monkeypatch, names=(ARCHIVE, DOCUMENT)):
    for name in names:
        write(root, name, (ROOT / name).read_bytes())
    write(root, scanner.PATH_AUDIT, (ROOT / scanner.PATH_AUDIT).read_bytes())
    write(root, ".gitattributes", b"* -text\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "audited synthetic history")
    baseline = git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(scanner, "PATH_AUDIT_BASELINE", baseline)
    policy = json.loads((ROOT / scanner.PATH_RULES).read_bytes())
    policy["baseline_commit"] = baseline
    save_policy(root, monkeypatch, policy)
    return policy


@pytest.mark.parametrize("topology", ["original", "squashed-retained", "squashed-discarded"])
def test_frozen_publication_survives_real_git_topologies(publication_repo, monkeypatch, tmp_path, topology):
    root, base = publication_repo
    history(root, monkeypatch)
    frozen = scanner.PATH_AUDIT_BASELINE
    git(root, "add", ".")
    git(root, "commit", "-qm", "publication rules")
    tip = git(root, "rev-parse", "HEAD")
    tree = git(root, "rev-parse", "HEAD^{tree}")
    subject = tip
    if topology != "original":
        subject = git(root, "commit-tree", tree, "-p", base, "-m", "publication squash")
    git(root, "update-ref", "refs/heads/published", subject)
    remote = tmp_path / "published.git"
    git(tmp_path, "init", "--bare", str(remote))
    refs = [base + ":refs/heads/main", "refs/heads/published:refs/heads/published"]
    if topology == "squashed-retained":
        refs.append(tip + ":refs/heads/retained")
    git(remote, "fetch", "--no-tags", "--no-recurse-submodules", str(root), *refs)
    checkout = tmp_path / "actions-checkout"
    git(tmp_path, "init", str(checkout))
    git(checkout, "remote", "add", "origin", str(remote))
    git(checkout, "fetch", "--no-tags", "--no-recurse-submodules", "origin",
        "+refs/heads/*:refs/remotes/origin/*", subject)
    git(checkout, "checkout", "--detach", subject)
    assert git(checkout, "status", "--porcelain") == ""
    assert git(checkout, "rev-parse", "HEAD^{tree}") == tree
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", frozen, "HEAD"],
                              cwd=checkout, capture_output=True).returncode
    exists = subprocess.run(["git", "cat-file", "-e", frozen + "^{commit}"],
                            cwd=checkout, capture_output=True).returncode
    if topology == "original":
        assert ancestor == exists == 0
    elif topology == "squashed-retained":
        assert ancestor == 1 and exists == 0
    else:
        assert ancestor != 0 and exists != 0
    result = scanner.review(checkout, base)
    assert result["status"] == "PASS", result["blocked_findings"]
    assert result["publication_scope"] == result["safety_scope"] == "FULL_DIFF"
    assert result["out_of_scope_files"] == []
    assert result["audited_encoding_exceptions"] == [ARCHIVE]
    assert git(checkout, "status", "--porcelain") == ""


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("directory", ["docs/research/other", "qbot/research/exploratory/other"])
@pytest.mark.parametrize("kind,reason", [
    ("path", "UNEXPLAINED_ABSOLUTE_PATH"),
    ("token", "SECRET_SHAPED_VALUE"),
    ("parquet", "RAW_OR_LARGE_ARTIFACT"),
    ("csv", "RAW_MARKET_CSV_COLUMNS"),
    ("large", "RAW_OR_LARGE_ARTIFACT"),
    ("encoding", "NON_UTF8_CONTENT"),
])
def test_outside_minute_scope_is_still_checked(publication_repo, directory, kind, reason, scoped):
    root, base = publication_repo
    content, suffix = {
        "path": (json.dumps({"p": "C:" + chr(47) + "private"}).encode(), ".json"),
        "token": (("ghp_" + "A" * 36).encode(), ".txt"),
        "parquet": (b"synthetic data", ".parquet"),
        "csv": (b"open,close\n1,2\n", ".csv"),
        "large": (b"a" * 2_000_001, ".log"),
        "encoding": (b"synthetic \xa1\xa1", ".log"),
    }[kind]
    name = directory + chr(47) + "probe" + suffix
    write(root, name, content)
    result = scanner.review(root, base, scope_yhh30m=scoped)
    assert result["status"] == "FAIL"
    assert {"file": name, "reason": reason} in [
        {"file": row["file"], "reason": row["reason"]} for row in result["blocked_findings"]
    ]
    assert result["safety_checked_files"] == [name]
    assert result["changed_files"] == ([] if scoped else [name])


@pytest.mark.parametrize("scoped", [False, True])
def test_exact_archive_passes_in_both_modes(publication_repo, monkeypatch, scoped):
    root, base = publication_repo
    history(root, monkeypatch)
    original = (root / ARCHIVE).read_bytes()
    result = scanner.review(root, base, scope_yhh30m=scoped)
    assert result["status"] == "PASS"
    assert result["safety_scope"] == "FULL_DIFF"
    assert result["audited_encoding_exceptions"] == [ARCHIVE]
    assert result["audited_path_exceptions"][DOCUMENT] > 0
    assert result["exception_baseline_commit"] == git(root, "rev-parse", "HEAD")
    assert (root / ARCHIVE).read_bytes() == original


def test_changed_legacy_bytes_lose_exception(publication_repo, monkeypatch):
    root, base = publication_repo
    history(root, monkeypatch)
    archive = root / ARCHIVE
    archive.write_bytes(archive.read_bytes() + b"\nchanged")
    result = scanner.review(root, base)
    assert result["status"] == "FAIL"
    assert {"file": ARCHIVE, "reason": "NON_UTF8_CONTENT"} in result["blocked_findings"]
    assert result["audited_encoding_exceptions"] == []


def test_changed_utf8_document_loses_path_exception(publication_repo, monkeypatch):
    root, base = publication_repo
    history(root, monkeypatch)
    document = root / DOCUMENT
    document.write_bytes(document.read_bytes() + ("\nC:" + chr(47) + "private").encode())
    result = scanner.review(root, base)
    assert result["status"] == "FAIL"
    assert DOCUMENT not in result["audited_path_exceptions"]
    assert any(row["file"] == DOCUMENT for row in result["blocked_findings"])


def test_new_path_cannot_claim_a_frozen_file_hash(publication_repo, monkeypatch):
    root, base = publication_repo
    policy = history(root, monkeypatch)
    name = "docs/research/market_structure/evidence/new.md"
    write(root, name, (ROOT / DOCUMENT).read_bytes())
    policy["files"][name] = dict(policy["files"][DOCUMENT])
    save_policy(root, monkeypatch, policy)
    result = scanner.review(root, base)
    assert result["status"] == "FAIL"
    assert name not in result["audited_path_exceptions"]
    assert {"file": scanner.PATH_RULES, "reason": "PATH_AUDIT_MANIFEST_MISMATCH"} in result["blocked_findings"]


def test_frozen_manifest_digest_mismatch_is_blocked(publication_repo, monkeypatch):
    root, base = publication_repo
    history(root, monkeypatch)
    manifest = root / scanner.PATH_AUDIT
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    result = scanner.review(root, base)
    assert result["status"] == "FAIL"
    assert {"file": scanner.PATH_RULES, "reason": "PATH_AUDIT_MANIFEST_MISMATCH"} in result["blocked_findings"]


def test_matching_current_hash_still_requires_frozen_source_membership(publication_repo, monkeypatch):
    root, base = publication_repo
    policy = history(root, monkeypatch)
    raw = ("C:" + chr(47) + "new-private-location").encode()
    write(root, DOCUMENT, raw)
    policy["files"][DOCUMENT]["sha256"] = hashlib.sha256(raw).hexdigest()
    save_policy(root, monkeypatch, policy)
    result = scanner.review(root, base)
    assert result["status"] == "FAIL"
    assert {"file": scanner.PATH_RULES, "reason": "PATH_AUDIT_MANIFEST_MISMATCH"} in result["blocked_findings"]


def test_new_file_under_archival_directory_is_not_exempt(publication_repo, monkeypatch):
    root, base = publication_repo
    history(root, monkeypatch)
    name = "docs/research/market_structure/evidence/v2/red/new.log"
    write(root, name, (ROOT / ARCHIVE).read_bytes())
    result = scanner.review(root, base)
    assert result["status"] == "FAIL"
    assert {"file": name, "reason": "NON_UTF8_CONTENT"} in result["blocked_findings"]


def test_new_actual_machine_path_is_blocked(publication_repo, monkeypatch):
    root, base = publication_repo
    history(root, monkeypatch)
    name = "docs/research/market_structure/evidence/new.json"
    write(root, name, json.dumps({"p": str(root.parent / "private")}).encode())
    result = scanner.review(root, base)
    assert {"file": name, "reason": "MACHINE_PATH"} in result["blocked_findings"]


def test_unicode_filename_is_scanned_as_its_real_git_path(publication_repo):
    root, base = publication_repo
    name = "docs/research/evidence/\u5ba1\u8ba1.json"
    write(root, name, json.dumps({"p": "C:" + chr(47) + "private"}).encode())
    result = scanner.review(root, base)
    assert result["safety_checked_files"] == [name]
    assert {"file": name, "reason": "UNEXPLAINED_ABSOLUTE_PATH", "context": ""} in result["blocked_findings"]


def test_escaped_json_secret_is_checked_after_decoding(publication_repo):
    root, base = publication_repo
    name = "docs/research/evidence/escaped.json"
    raw = json.dumps({"secret": "ghp_" + "A" * 36}).replace("ghp_", "ghp" + r"\u005f").encode()
    write(root, name, raw)
    result = scanner.review(root, base)
    assert {"file": name, "reason": "SECRET_SHAPED_VALUE"} in result["blocked_findings"]


def test_rule_version_cannot_select_a_different_baseline(publication_repo, monkeypatch):
    root, base = publication_repo
    policy = history(root, monkeypatch)
    policy["baseline_commit"] = base
    save_policy(root, monkeypatch, policy)
    result = scanner.review(root, base)
    assert {"file": scanner.PATH_RULES, "reason": "PATH_AUDIT_MANIFEST_MISMATCH"} in result["blocked_findings"]


def test_legacy_log_requires_frozen_text_attribute(publication_repo, monkeypatch):
    root, base = publication_repo
    history(root, monkeypatch)
    write(root, ".gitattributes", b"*.log text\n")
    result = scanner.review(root, base)
    assert result["status"] == "FAIL"
    assert {"file": ARCHIVE, "reason": "LEGACY_TEXT_ATTRIBUTES_MISMATCH"} in result["blocked_findings"]


def test_binary_signature_is_not_treated_as_bad_utf8_text(publication_repo):
    root, base = publication_repo
    name = "docs/research/market_structure/evidence/image.png"
    write(root, name, b"\x89PNG\r\n\x1a\nsynthetic")
    result = scanner.review(root, base)
    assert result["status"] == "FAIL"
    assert result["blocked_findings"] == [{"file": name, "reason": "BINARY_ARTIFACT_REQUIRES_REVIEW"}]


def test_unexpected_match_count_invalidates_exception(publication_repo, monkeypatch):
    root, base = publication_repo
    policy = history(root, monkeypatch)
    policy["files"][DOCUMENT]["path_match_counts"]["WINDOWS_DRIVE"] += 1
    save_policy(root, monkeypatch, policy)
    result = scanner.review(root, base)
    assert result["status"] == "FAIL"
    assert {"file": DOCUMENT, "reason": "PATH_EXCEPTION_COUNT_MISMATCH"} in result["blocked_findings"]
    assert DOCUMENT not in result["audited_path_exceptions"]


def test_rehashed_rule_file_cannot_grant_exception(publication_repo, monkeypatch):
    root, base = publication_repo
    policy = history(root, monkeypatch)
    policy["files"][DOCUMENT]["sha256"] = "0" * 64
    write(root, scanner.PATH_RULES, json.dumps(policy).encode())
    result = scanner.review(root, base)
    assert result["status"] == "FAIL"
    assert {"file": scanner.PATH_RULES, "reason": "PATH_AUDIT_MANIFEST_MISMATCH"} in result["blocked_findings"]


@pytest.mark.parametrize("alias", [".", "../file", "a/../file", chr(47) + "file", "a//file", "a\\file", "a/./file"])
def test_noncanonical_rule_paths_are_rejected(publication_repo, monkeypatch, alias):
    root, base = publication_repo
    policy = history(root, monkeypatch)
    policy["files"][alias] = policy["files"].pop(DOCUMENT)
    save_policy(root, monkeypatch, policy)
    result = scanner.review(root, base)
    assert result["status"] == "FAIL"
    assert {"file": scanner.PATH_RULES, "reason": "PATH_AUDIT_MANIFEST_MISMATCH"} in result["blocked_findings"]


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("gate", scanner.GATES)
def test_every_protected_gate_remains_global(publication_repo, gate, scoped):
    root, base = publication_repo
    name = gate + "probe.py" if gate.endswith(chr(47)) else gate
    write(root, name, b"# synthetic gate modification\n")
    result = scanner.review(root, base, scope_yhh30m=scoped)
    assert result["status"] == "FAIL"
    assert result["protected_gate_changes"] == [name]


@pytest.mark.parametrize("kind", ["token", "csv", "parquet"])
def test_authenticated_exception_never_exempts_secrets_or_raw_data(publication_repo, monkeypatch, kind):
    root, base = publication_repo
    value = "C:" + chr(47) + "synthetic"
    content, suffix = {
        "token": ((value + "\nghp_" + "A" * 36).encode(), ".txt"),
        "csv": (("open,close\n1," + value + "\n").encode(), ".csv"),
        "parquet": (value.encode(), ".parquet"),
    }[kind]
    name = "docs/research/market_structure/evidence/synthetic" + suffix
    digest = hashlib.sha256(content).hexdigest()
    write(root, name, content)
    manifest = json.dumps({"git_blob_files": {name: digest}}).encode()
    write(root, scanner.PATH_AUDIT, manifest)
    monkeypatch.setattr(scanner, "PATH_AUDIT_SHA256", hashlib.sha256(manifest).hexdigest())
    git(root, "add", ".")
    git(root, "commit", "-qm", "synthetic audited history")
    baseline = git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(scanner, "PATH_AUDIT_BASELINE", baseline)
    policy = {"schema": "PUBLICATION_PATH_RULES_V1", "baseline_commit": baseline,
              "evidence_manifest_sha256": scanner.PATH_AUDIT_SHA256,
              "files": {name: {"sha256": digest, "encoding": "utf-8-sig",
                                "path_match_counts": {"WINDOWS_DRIVE": 1}}}}
    save_policy(root, monkeypatch, policy)
    result = scanner.review(root, base)
    assert result["status"] == "FAIL"
    assert any(row["file"] == name for row in result["blocked_findings"])
