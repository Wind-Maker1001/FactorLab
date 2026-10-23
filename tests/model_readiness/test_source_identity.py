"""Adversarial identity tests, independent reference digest and historical replay."""

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from qbot.research.diagnostics.model_readiness.source_identity import (
    NORMALIZATION, PROTOCOL, SCOPE, SourceIntegrityError, assert_source_unchanged,
    canonical_digest, git_source_snapshot, source_snapshot,
)
from tests.model_readiness.source_hash_audit import legacy_digest, reconstruct


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "core.autocrlf", "false")
    git(tmp_path, "config", "core.hooksPath", str(tmp_path / "unused-hooks"))
    (tmp_path / "qbot/research").mkdir(parents=True)
    (tmp_path / "qbot/research/A.py").write_bytes(b"a = 'alpha'\n# second line\n")
    (tmp_path / "qbot/research/z.py").write_bytes("# 中文\nz = 2\n".encode("utf-8"))
    git(tmp_path, "add", "qbot")
    git(tmp_path, "-c", "user.name=Readiness Test", "-c", "user.email=readiness@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    return tmp_path


def test_independent_framed_reference_matches_git_and_crlf_worktree(repository):
    files = {p.relative_to(repository).as_posix(): p.read_bytes()
             for p in (repository / "qbot/research").glob("*.py")}
    records = [{"bytes": len(data), "path": name, "sha256": hashlib.sha256(data).hexdigest()}
               for name, data in sorted(files.items(), key=lambda item: item[0].encode("utf-8"))]
    # Defined independently of canonical_digest, using the public wire contract.
    expected = hashlib.sha256(json.dumps({
        "protocol": PROTOCOL, "scope": SCOPE, "normalization": NORMALIZATION, "files": records,
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    first = source_snapshot(repository)
    for path in (repository / "qbot/research").glob("*.py"):
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    windows = source_snapshot(repository)
    committed = git_source_snapshot(repository)
    assert first["canonical_sha256"] == windows["canonical_sha256"] == committed["canonical_sha256"] == expected
    assert first["worktree_raw_v1_sha256"] != windows["worktree_raw_v1_sha256"]
    assert windows["git_raw_v1_sha256"] == first["worktree_raw_v1_sha256"]
    assert windows["source_count"] == 2


@pytest.mark.parametrize("change", [b"a = 'Alpha'\n# second line\n", b"a = 'alpha' \n# second line\n",
                                    b"a = 'alpha'\n# second line", b"\xef\xbb\xbfa = 'alpha'\n# second line\n"])
def test_non_eol_edits_including_bom_whitespace_and_final_newline_fail(repository, change):
    (repository / "qbot/research/A.py").write_bytes(change)
    with pytest.raises(SourceIntegrityError, match="uncommitted source content"):
        source_snapshot(repository)


@pytest.mark.parametrize("ignored", [False, True])
@pytest.mark.parametrize("filename", ["hidden.py", "hidden.PY"])
def test_extra_untracked_or_ignored_source_cannot_be_silently_omitted(repository, ignored, filename):
    if ignored:
        (repository / ".gitignore").write_text(filename + "\n", encoding="utf-8")
    (repository / "qbot/research" / filename).write_bytes(b"hidden = True\n")
    with pytest.raises(SourceIntegrityError, match="extra=.*" + filename):
        source_snapshot(repository)


def test_missing_source_fails(repository):
    (repository / "qbot/research/A.py").unlink()
    with pytest.raises(SourceIntegrityError, match="missing=.*A.py"):
        source_snapshot(repository)


@pytest.mark.parametrize("content", [b"a = 1\r", b"# \xff\n"])
def test_unsupported_source_encoding_or_bare_cr_is_rejected(repository, content):
    (repository / "qbot/research/A.py").write_bytes(content)
    with pytest.raises(SourceIntegrityError, match="lone CR|UTF-8"):
        source_snapshot(repository)


def test_raw_eol_change_during_run_fails_even_when_canonical_identity_matches(repository):
    start = source_snapshot(repository)
    path = repository / "qbot/research/A.py"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    end = source_snapshot(repository)
    assert start["canonical_sha256"] == end["canonical_sha256"]
    with pytest.raises(SourceIntegrityError, match="changed during run: worktree_raw"):
        assert_source_unchanged(start, end)


def test_commit_change_during_run_is_not_hidden_by_equal_source(repository):
    start = source_snapshot(repository)
    git(repository, "-c", "user.name=Readiness Test", "-c", "user.email=readiness@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "different run version")
    end = source_snapshot(repository)
    assert start["canonical_sha256"] == end["canonical_sha256"]
    with pytest.raises(SourceIntegrityError, match="changed during run: git_commit"):
        assert_source_unchanged(start, end)


def test_file_name_and_record_boundaries_are_part_of_contract():
    assert canonical_digest({"qbot/research/a.py": b"bc"}) != canonical_digest({"qbot/research/ab.py": b"c"})
    first = {"qbot/research/a.py": b"a\n", "qbot/research/z.py": b"z\n"}
    assert canonical_digest(first) == canonical_digest(dict(reversed(list(first.items()))))


def test_inherited_git_routing_cannot_redirect_source_snapshot(repository, monkeypatch, tmp_path):
    expected = source_snapshot(repository)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nonexistent.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "elsewhere"))
    assert source_snapshot(repository) == expected


def test_historical_git_blobs_and_eol_map_reconstruct_original_raw_receipt():
    root = Path(__file__).resolve().parents[2]
    evidence = json.loads((root / "docs/research/evidence/model-readiness-source-v2/original-head-forensics.json").read_text(encoding="utf-8"))
    original = {}
    for record in evidence["files"]:
        name = record["path"]
        blob = git(root, "show", evidence["revision"] + ":" + name)
        assert hashlib.sha256(blob).hexdigest() == record["git_objects"]["raw_sha256"]
        replay = reconstruct(blob, evidence["windows_crlf_lf_ordinals"][name])
        assert hashlib.sha256(replay).hexdigest() == record["windows_worktree"]["raw_sha256"]
        assert record["windows_worktree"]["lf_equal_git"]
        original[name] = replay
    assert len(original) == 138
    assert legacy_digest(original) == evidence["replayed_windows_legacy_sha256"] == "c4c5e400ff44abdd968be10c81cd66e10fc8312422987ff7009d1e8c884a00c5"
    assert evidence["views"]["git_objects"]["raw_legacy_sha256"] == "abad7e91e31fbd881b81ffaaa9a7766fe1bf26850ec802f83dff7c72dd0fc64f"
