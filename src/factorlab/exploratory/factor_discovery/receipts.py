"""Independent tamper-evident version receipts with an exploratory-only schema."""

import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys

from .contracts import DiscoveryError, ErrorCode, SCHEMA


def json_bytes(payload):
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(payload):
    return hashlib.sha256(json_bytes(payload)).hexdigest()


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_evidence():
    research = Path(__file__).resolve().parents[2]
    from factorlab._identity import source_context_root
    repo = source_context_root()
    paths = list(Path(__file__).parent.glob("*.py"))
    paths += list((research / "factors").glob("*.py"))
    paths += [
        research / name
        for name in (
            "data/schema.py",
            "data/calendar.py",
            "metrics/factor.py",
            "metrics/_factor_arrays.py",
            "labels/forward_return.py",
            "exploratory/contract.py",
            "exploratory/runner.py",
        )
    ]
    hashes = {
        path.relative_to(repo).as_posix(): file_sha256(path) for path in sorted(paths)
    }

    from factorlab._identity import package_root, software_identity
    source_git = software_identity()["git_commit"]

    def git(*args):
        if source_git is None:
            return None
        try:
            return subprocess.check_output(
                ["git", *args],
                cwd=repo,
                text=True,
                encoding="utf-8",
                stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.CalledProcessError, OSError):
            return None

    versions = {}
    for name in ("numpy", "pandas", "scipy", "pyarrow", "pytest"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "NOT_INSTALLED"
    return {
        "source_sha256": hashes,
        "source_digest": digest(hashes),
        "git_head": git("rev-parse", "HEAD"),
        "git_branch": git("branch", "--show-current"),
        "source_tree_dirty": (None if source_git is None else bool(
            git(
                "status",
                "--porcelain",
                "--untracked-files=normal",
                "--",
                "src/factorlab",
                "tests/research",
            )
        )),
        "python": sys.version,
        "platform": platform.platform(),
        "dependencies": versions,
        "hash_policy": "EXACT_SOURCE_BYTES_INCLUDING_LINE_ENDINGS",
    }


def assert_discovery_only(receipt, purpose="EXPLORATORY"):
    if (
        purpose != "EXPLORATORY"
        or receipt.get("schema") != SCHEMA
        or receipt.get("purpose") != "EXPLORATORY"
        or receipt.get("qualification") != "UNVERIFIED"
        or receipt.get("formal_admission") is not False
        or receipt.get("forward_allowed") is not False
        or receipt.get("live_allowed") is not False
    ):
        raise DiscoveryError(
            ErrorCode.INVALID_INPUT,
            "discovery cannot authorize formal/Forward/live use",
        )


def write_receipt(destination, payload):
    destination = Path(destination)
    receipt = dict(
        payload,
        schema=SCHEMA,
        purpose="EXPLORATORY",
        qualification="UNVERIFIED",
        formal_admission=False,
        forward_allowed=False,
        live_allowed=False,
        warning="NO LIVE MONEY; SOFTWARE SUCCESS IS NOT TRADABLE ALPHA",
    )
    receipt["artifacts"] = {
        path.name: file_sha256(path)
        for path in sorted(destination.iterdir())
        if path.is_file() and path.name != "receipt.json"
    }
    assert_discovery_only(receipt)
    receipt["receipt_sha256"] = digest(receipt)
    (destination / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return receipt


def verify_receipt(path):
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert_discovery_only(payload)
    advertised = payload.pop("receipt_sha256", None)
    if digest(payload) != advertised:
        raise DiscoveryError(ErrorCode.INVALID_INPUT, "receipt semantic hash mismatch")
    if payload.get("status") not in (
        "SOFTWARE_MVP_SUCCESS",
        "FAILED",
        "BLOCKED",
        "NOT_RUN",
    ):
        raise DiscoveryError(ErrorCode.INVALID_INPUT, "unsupported completion state")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {
        item.name for item in path.parent.iterdir() if item.is_file() and item != path
    }:
        raise DiscoveryError(ErrorCode.INVALID_INPUT, "receipt artifact set mismatch")
    for relative, expected in artifacts.items():
        artifact = path.parent / relative
        if (
            Path(relative).name != relative
            or artifact.is_symlink()
            or artifact.resolve().parent != path.parent.resolve()
            or file_sha256(artifact) != expected
        ):
            raise DiscoveryError(ErrorCode.INVALID_INPUT, "artifact hash/path mismatch")
    payload["receipt_sha256"] = advertised
    return payload
