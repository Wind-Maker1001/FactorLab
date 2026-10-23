"""Versioned, commit-bound source identity for the optional readiness tool.

Canonical identity normalizes CRLF to LF only. Raw byte identities remain
separate evidence. No source file or Git attribute is rewritten by this module.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess


PROTOCOL = "factorlab-independent-model-readiness-source-v1"
SCOPE = "Git-tracked regular Python files (*.py, ASCII case-insensitive) recursively under src/factorlab"
NORMALIZATION = "UTF-8 bytes; CRLF -> LF only; reject lone CR; preserve all other bytes"


class SourceIntegrityError(ValueError):
    """Source cannot be tied to one committed implementation."""


def _git(root, *arguments, input_bytes=None):
    environment = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        environment.pop(name, None)
    try:
        return subprocess.run(
            ["git", "-C", str(root), *arguments], input=input_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            env=environment,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SourceIntegrityError("cannot read committed source with Git: %s" % exc) from exc


def _normalize(content):
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceIntegrityError("source must be UTF-8") from exc
    result = content.replace(b"\r\n", b"\n")
    if b"\r" in result:
        raise SourceIntegrityError("source contains unsupported lone CR")
    return result


def canonical_digest(files):
    """Hash a framed, versioned manifest, sorted by UTF-8 POSIX path bytes.

    File lengths are normalized byte lengths. No raw/EOL/platform metadata
    enters the canonical payload. JSON separators and key ordering are fixed.
    """
    records = []
    for name in sorted(files, key=lambda value: value.encode("utf-8")):
        content = _normalize(files[name])
        records.append({"path": name, "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest()})
    payload = {"protocol": PROTOCOL, "scope": SCOPE,
               "normalization": NORMALIZATION, "files": records}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _raw_legacy_digest(files):
    # Compatibility evidence only; never used as the new canonical identity.
    digest = hashlib.sha256()
    for name in sorted(files, key=lambda value: value.encode("utf-8")):
        digest.update(name.encode("utf-8"))
        digest.update(files[name])
    return digest.hexdigest()


def _committed_files(root, revision):
    root = Path(root).resolve()
    top = Path(_git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
    if top != root:
        raise SourceIntegrityError("root must be the Git repository root")
    commit = _git(root, "rev-parse", "--verify", "--end-of-options",
                  revision + "^{commit}").decode("ascii").strip()
    entries = {}
    for record in _git(root, "ls-tree", "-rz", commit, "--", "src/factorlab").split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        name = path.decode("utf-8")
        if not name.lower().endswith(".py"):
            continue
        mode, kind, oid = metadata.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise SourceIntegrityError("source must be a regular Git blob: " + name)
        entries[name] = oid
    if not entries:
        raise SourceIntegrityError("committed source inventory is empty")
    names = sorted(entries, key=lambda value: value.encode("utf-8"))
    result = _git(root, "cat-file", "--batch", input_bytes=b"\n".join(entries[n] for n in names) + b"\n")
    files, cursor = {}, 0
    for name in names:
        end = result.index(b"\n", cursor)
        oid, kind, size = result[cursor:end].split()
        if oid != entries[name] or kind != b"blob":
            raise SourceIntegrityError("unexpected Git object: " + name)
        start, length = end + 1, int(size)
        files[name] = result[start:start + length]
        cursor = start + length
        if len(files[name]) != length or result[cursor:cursor + 1] != b"\n":
            raise SourceIntegrityError("truncated Git object: " + name)
        cursor += 1
    if cursor != len(result):
        raise SourceIntegrityError("unexpected trailing Git object data")
    return commit, files


def _working_files(root):
    root = Path(root).resolve()
    files = {}
    source_root = root / "src/factorlab"
    if not source_root.is_dir():
        raise SourceIntegrityError("working source directory is missing")
    for directory, children, filenames in os.walk(source_root, followlinks=False):
        parent = Path(directory)
        for name in children:
            child = parent / name
            if child.is_symlink() or child.resolve() != child.absolute():
                raise SourceIntegrityError("linked source directory: " + str(child))
        for name in filenames:
            if not name.lower().endswith(".py"):
                continue
            path = parent / name
            if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                raise SourceIntegrityError("source is not a regular working file: " + str(path))
            if path.resolve() != path.absolute():
                raise SourceIntegrityError("linked working source: " + str(path))
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def git_source_snapshot(root, revision="HEAD"):
    commit, files = _committed_files(root, revision)
    return {"schema_version": 2, "protocol": PROTOCOL, "scope": SCOPE,
            "normalization": NORMALIZATION, "git_commit": commit,
            "source_count": len(files), "canonical_sha256": canonical_digest(files),
            "git_raw_v1_sha256": _raw_legacy_digest(files)}


def source_snapshot(root, revision="HEAD"):
    """Reject missing, extra (even ignored), linked or dirty source files.

    Git's clean/diff status is deliberately insufficient: compare normalized
    file bytes with blobs from an explicitly resolved immutable commit.
    """
    commit, committed = _committed_files(root, revision)
    working = _working_files(root)
    missing = sorted(set(committed) - set(working))
    extra = sorted(set(working) - set(committed))
    if missing or extra:
        raise SourceIntegrityError("source inventory differs; missing=%s extra=%s" % (missing, extra))
    changed = [name for name in committed if _normalize(committed[name]) != _normalize(working[name])]
    if changed:
        raise SourceIntegrityError("uncommitted source content differs: " + ", ".join(sorted(changed)))
    records = []
    for name in sorted(committed, key=lambda value: value.encode("utf-8")):
        blob, content = committed[name], working[name]
        records.append({
            "path": name, "canonical_sha256": hashlib.sha256(_normalize(content)).hexdigest(),
            "git_raw_sha256": hashlib.sha256(blob).hexdigest(),
            "worktree_raw_sha256": hashlib.sha256(content).hexdigest(),
            "git_bytes": len(blob), "worktree_bytes": len(content),
            "worktree_crlf": content.count(b"\r\n"),
            "worktree_lf_only": content.count(b"\n") - content.count(b"\r\n"),
        })
    return {"schema_version": 2, "protocol": PROTOCOL, "scope": SCOPE,
            "normalization": NORMALIZATION, "git_commit": commit,
            "source_count": len(working), "canonical_sha256": canonical_digest(working),
            "git_canonical_sha256": canonical_digest(committed),
            "worktree_raw_v1_sha256": _raw_legacy_digest(working),
            "git_raw_v1_sha256": _raw_legacy_digest(committed), "files": records}


def assert_source_unchanged(start, end):
    for field in ("protocol", "git_commit", "canonical_sha256", "worktree_raw_v1_sha256"):
        if start[field] != end[field]:
            raise SourceIntegrityError("source changed during run: " + field)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--git-only", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = (git_source_snapshot if args.git_only else source_snapshot)(args.root, args.revision)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(snapshot["protocol"], snapshot["git_commit"], snapshot["canonical_sha256"])
