"""Seal committed public file bytes, then independently verify a checkout.

V2 deliberately makes no persistent worktree SHA promise: Git text filters can
produce LF or CRLF checkouts. Every authoritative SHA256 is freshly computed
from an explicit immutable commit's blob bytes, never copied from an old index.
"""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


SCHEMA = "TDX_DELIVERY_FILE_INDEX_V2"


def git(root, *args, content=None):
    return subprocess.check_output(["git", *args], cwd=root, input=content)


def public_path(name):
    if not isinstance(name, str):
        raise ValueError("delivery path must be text")
    path = PurePosixPath(name)
    if ("\\" in name or ":" in name or
            path.is_absolute() or ".." in path.parts or path.as_posix() != name or
            not name.startswith(("docs/research/", "examples/research/", "tests/research/"))):
        raise ValueError("not an allowed public delivery path: " + str(name))
    return name


def committed_file(root, commit, name):
    name = public_path(name)
    content = git(root, "cat-file", "blob", commit + ":" + name)
    oid = git(root, "rev-parse", commit + ":" + name).decode("ascii").strip()
    return content, {"git_blob_sha256": hashlib.sha256(content).hexdigest(),
                     "git_blob_bytes": len(content), "git_blob_oid": oid}


def build_index(root, revision, paths):
    commit = git(root, "rev-parse", "--verify", revision + "^{commit}").decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("a full immutable SHA1 commit is required")
    names = sorted(set(paths))
    if not names:
        raise ValueError("empty public delivery")
    return {"schema": SCHEMA, "source_commit": commit,
            "hash_contract": "SHA256 and byte count of Git blob bytes at source_commit; no worktree_sha256 contract",
            "checkout_contract": "independently read worktree bytes; Git clean filters must reproduce the committed blob OID",
            "files": {name: committed_file(root, commit, name)[1] for name in names},
            "native_data_published": False}


def verify_index(root, index):
    if index.get("schema") != SCHEMA or not re.fullmatch(r"[0-9a-f]{40}", index.get("source_commit", "")):
        raise ValueError("invalid committed-byte delivery contract")
    if not index.get("files"):
        raise ValueError("empty public delivery")
    results = {}
    for name, expected in index["files"].items():
        blob, actual = committed_file(root, index["source_commit"], name)
        if actual != expected:
            raise ValueError("committed bytes differ: " + name)
        path = Path(root) / name
        if path.is_symlink():
            raise ValueError("delivery checkout must be a regular file: " + name)
        worktree = path.read_bytes()  # Independent filesystem read, not the Git blob.
        cleaned_oid = git(root, "hash-object", "--path=" + name, "--stdin", content=worktree).decode("ascii").strip()
        if cleaned_oid != actual["git_blob_oid"]:
            raise ValueError("checkout content differs after Git clean filters: " + name)
        results[name] = {"git_blob_sha256": actual["git_blob_sha256"],
                         "worktree_raw_sha256_diagnostic": hashlib.sha256(worktree).hexdigest(),
                         "raw_bytes_equal": worktree == blob, "cleaned_blob_matches": True}
    return {"schema": "TDX_DELIVERY_VERIFICATION_V2", "source_commit": index["source_commit"],
            "status": "PASS", "verified_files": len(results), "files": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    seal = sub.add_parser("seal")
    seal.add_argument("--revision", required=True)
    seal.add_argument("--paths-from", type=Path, required=True, help="JSON object with a files mapping")
    seal.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--index", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "seal":
        index = build_index(args.repository, args.revision, json.loads(args.paths_from.read_bytes())["files"])
        # Verify the current checkout before sealing; no stale pre-edit snapshot.
        verify_index(args.repository, index)
        with args.output.open("xb") as stream:
            stream.write((json.dumps(index, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    else:
        print(json.dumps(verify_index(args.repository, json.loads(args.index.read_bytes())), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
