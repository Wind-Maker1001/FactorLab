"""Commit/checkout byte contracts; these are software delivery tests only."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from examples.research.seal_tdx_delivery import build_index, public_path, verify_index


def git(root, *args):
    env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1")
    return subprocess.check_output(["git", *args], cwd=root, env=env, stderr=subprocess.PIPE)


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "software-test@example.invalid")
    git(root, "config", "user.name", "Software test")
    git(root, "config", "core.autocrlf", "true")
    (root / "docs/research").mkdir(parents=True)
    (root / ".gitattributes").write_bytes(b"*.md text\n*.json -text\n")
    (root / "docs/research/report.md").write_bytes(b"report\r\nsecond line\r\n")
    (root / "docs/research/raw.json").write_bytes(b'{"keep": true}\r\n')
    git(root, "add", ".")
    git(root, "commit", "-m", "Synthetic delivery byte fixture")
    return root


@pytest.mark.parametrize("autocrlf", ["false", "true"])
def test_committed_digest_is_stable_across_clean_checkout(repository, tmp_path, autocrlf):
    paths = ["docs/research/report.md", "docs/research/raw.json"]
    index = build_index(repository, "HEAD", paths)
    blob = git(repository, "cat-file", "blob", index["source_commit"] + ":" + paths[0])
    raw = (repository / paths[0]).read_bytes()
    assert raw != blob
    assert index["files"][paths[0]]["git_blob_sha256"] == hashlib.sha256(blob).hexdigest()
    assert index["files"][paths[0]]["git_blob_sha256"] != hashlib.sha256(raw).hexdigest()
    assert all("worktree_sha256" not in entry for entry in index["files"].values())
    clone = tmp_path / "clean"
    git(tmp_path, "clone", "--shared", "--no-checkout", str(repository), str(clone))
    git(clone, "config", "core.autocrlf", autocrlf)
    git(clone, "config", "core.eol", "lf")
    git(clone, "checkout", "--detach", index["source_commit"])
    assert not git(clone, "status", "--porcelain")
    checked = verify_index(clone, index)
    assert checked["verified_files"] == 2
    assert checked["files"][paths[0]]["raw_bytes_equal"] == (autocrlf == "false")
    assert checked["files"][paths[1]]["raw_bytes_equal"]


@pytest.mark.parametrize("failure", ["edit_after_seal", "delete", "wrong_sha", "wrong_size", "stale_worktree_field"])
def test_delivery_rejects_stale_or_tampered_bytes(repository, failure):
    name = "docs/research/report.md"
    index = copy.deepcopy(build_index(repository, "HEAD", [name]))
    path = repository / name
    if failure == "edit_after_seal":
        path.write_bytes(b"substantive change\r\n")
    elif failure == "delete":
        path.unlink()
    elif failure == "wrong_sha":
        index["files"][name]["git_blob_sha256"] = "0" * 64
    elif failure == "wrong_size":
        index["files"][name]["git_blob_bytes"] += 1
    else:
        index["files"][name]["worktree_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises((ValueError, FileNotFoundError)):
        verify_index(repository, index)


@pytest.mark.parametrize("name", ["../private", "docs/research/../../private", "/docs/research/a",
                                 "D:/private", "data/research/private.json", "docs\\research\\a", None])
def test_delivery_never_seals_private_or_ambiguous_paths(name):
    with pytest.raises(ValueError):
        public_path(name)


def test_all_published_tdx_files_in_clean_checkout(tmp_path):
    root = Path(__file__).resolve().parents[2]
    index = json.loads((root / "docs/research/evidence/tdx-data-quality-v2/delivery-files.json").read_bytes())
    clone = tmp_path / "public-clean-checkout"
    git(tmp_path, "clone", "--shared", "--no-checkout", str(root), str(clone))
    git(clone, "config", "core.autocrlf", "false")
    git(clone, "config", "core.eol", "lf")
    git(clone, "checkout", "--detach", index["source_commit"])
    assert not git(clone, "status", "--porcelain")
    result = verify_index(clone, index)
    assert result["verified_files"] == len(index["files"])
    assert all(entry["raw_bytes_equal"] for entry in result["files"].values())
    old = json.loads((clone / "docs/research/evidence/tdx-data-quality-v1/delivery-files.json").read_bytes())
    assert set(old["files"]) < set(index["files"])
    report = "docs/research/TDX_DATA_QUALITY_AUDIT.md"
    raw = (clone / report).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == old["files"][report]["git_blob_sha256"]
    assert hashlib.sha256(raw).hexdigest() != old["files"][report]["worktree_sha256"]
