"""Reproducible run manifests for every FactorLab CLI invocation."""

import importlib.metadata
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional
import uuid

import numpy as np

from qbot.research.config import ResearchConfig


TRACKED_PACKAGES = (
    "numpy",
    "pandas",
    "pyarrow",
    "scipy",
    "scikit-learn",
    "matplotlib",
    "akshare",
    "baostock",
    "lightgbm",
    "xgboost",
    "PyYAML",
    "pytest",
    "tzdata",
)

PROVENANCE_FIELDS = (
    "git_commit",
    "git_dirty",
    "git_diff_sha256",
    "python_version",
    "package_versions",
)

_UNKNOWN_TEXT = frozenset(("unknown", "unavailable", "n/a", "none", "null"))
_MANIFEST_REPLACE_RETRY_DELAYS = (0.025, 0.05, 0.1)
LOGGER = logging.getLogger(__name__)


def _known_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value.strip().lower() not in _UNKNOWN_TEXT
    )


def _hex_digest(value: Any, lengths: tuple) -> bool:
    return (
        isinstance(value, str)
        and len(value) in lengths
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def validate_provenance(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a comparable provenance snapshot or reject unknown values."""

    if not isinstance(payload, Mapping):
        raise ValueError("provenance payload is not a mapping")
    missing = [name for name in PROVENANCE_FIELDS if name not in payload]
    if missing:
        raise ValueError("missing fields: %s" % ", ".join(missing))
    if not _hex_digest(payload["git_commit"], (40, 64)):
        raise ValueError("git_commit is unknown")
    if not _known_text(payload["python_version"]):
        raise ValueError("python_version is unknown")

    dirty = payload["git_dirty"]
    diff_sha256 = payload["git_diff_sha256"]
    if not isinstance(dirty, bool):
        raise ValueError("git_dirty is unknown")
    if dirty and not _hex_digest(diff_sha256, (64,)):
        raise ValueError("git_diff_sha256 is unknown for a dirty worktree")
    if not dirty and diff_sha256 is not None:
        raise ValueError("git_diff_sha256 must be null for a clean worktree")

    versions = payload["package_versions"]
    if not isinstance(versions, Mapping) or not versions:
        raise ValueError("package_versions is missing or unknown")
    for name, version in versions.items():
        if not _known_text(name):
            raise ValueError("package_versions contains an unknown package name")
        # None records a known, reproducible state: the distribution is absent.
        if version is not None and not _known_text(version):
            raise ValueError("package version for %s is unknown" % name)

    return {
        "git_commit": payload["git_commit"],
        "git_dirty": dirty,
        "git_diff_sha256": diff_sha256,
        "python_version": payload["python_version"],
        "package_versions": dict(versions),
    }


def set_random_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)


def git_commit(repo_root: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def git_worktree_state(repo_root: Path) -> Dict[str, Any]:
    """Fingerprint tracked diffs and untracked source files without storing them."""

    root = Path(repo_root).resolve()
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=str(root),
            check=True,
            capture_output=True,
            timeout=20,
        ).stdout
        if not status:
            return {"git_dirty": False, "git_diff_sha256": None}
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD"],
            cwd=str(root),
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=str(root),
            check=True,
            capture_output=True,
            timeout=20,
        ).stdout.split(b"\0")
    except (OSError, subprocess.SubprocessError):
        return {"git_dirty": None, "git_diff_sha256": None}

    digest = hashlib.sha256()
    digest.update(b"STATUS\0")
    digest.update(status)
    digest.update(b"DIFF\0")
    digest.update(diff)
    for encoded_name in sorted(name for name in untracked if name):
        digest.update(b"UNTRACKED\0")
        digest.update(encoded_name)
        candidate = root / os.fsdecode(encoded_name)
        try:
            if candidate.is_symlink():
                digest.update(os.fsencode(os.readlink(str(candidate))))
            else:
                with candidate.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
        except OSError as exc:
            digest.update(("<unreadable:%s>" % type(exc).__name__).encode("ascii"))
    return {"git_dirty": True, "git_diff_sha256": digest.hexdigest()}


def package_versions(names: Iterable[str] = TRACKED_PACKAGES) -> Dict[str, Optional[str]]:
    versions: Dict[str, Optional[str]] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _atomic_json(payload: Dict[str, Any], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(".%s.tmp-%s" % (destination.name, uuid.uuid4().hex))
    owns_temporary = False
    try:
        # Serialize and flush exactly once. Windows readers and virus scanners
        # can briefly hold the destination open; retry only this same atomic
        # replace, never a rewrite or a delete of the previous valid manifest.
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        with temporary.open("x", encoding="utf-8") as stream:
            owns_temporary = True
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(len(_MANIFEST_REPLACE_RETRY_DELAYS) + 1):
            try:
                os.replace(str(temporary), str(destination))
                break
            except OSError as exc:
                if (
                    getattr(exc, "winerror", None) not in (5, 32, 33)
                    or attempt >= len(_MANIFEST_REPLACE_RETRY_DELAYS)
                ):
                    raise
                delay = _MANIFEST_REPLACE_RETRY_DELAYS[attempt]
                LOGGER.warning(
                    "Transient Windows manifest replace failure (winerror=%s); "
                    "retry %d/%d in %.3fs: %s",
                    exc.winerror,
                    attempt + 1,
                    len(_MANIFEST_REPLACE_RETRY_DELAYS),
                    delay,
                    destination,
                )
                time.sleep(delay)
    finally:
        # Only this call owns this UUID temporary. In particular, do not
        # remove old failed-call temporaries or mutate existing run history.
        if owns_temporary:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                LOGGER.warning("Cannot remove current manifest temporary: %s", temporary, exc_info=True)


class RunManifest:
    """Mutable-in-memory, atomic-on-disk lifecycle record."""

    def __init__(
        self,
        config: ResearchConfig,
        command: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.run_id = "%s-%s-%s" % (
            now.strftime("%Y%m%dT%H%M%S.%fZ"),
            command.replace("_", "-"),
            uuid.uuid4().hex[:8],
        )
        self.path = config.paths.runs_dir / self.run_id / "manifest.json"
        universe_manifest = config.paths.universe_dir / "manifest.json"
        universe_version = None
        if universe_manifest.exists():
            try:
                universe_version = json.loads(
                    universe_manifest.read_text(encoding="utf-8")
                ).get("sha256")
            except (OSError, ValueError, AttributeError):
                universe_version = None
        self.payload: Dict[str, Any] = {
            "run_id": self.run_id,
            "timestamp": now.isoformat(),
            "completed_at": None,
            "command": command,
            "arguments": arguments or {},
            "status": "running",
            "git_commit": git_commit(config.repo_root or Path.cwd()),
            "upstream_audit_commit": "f0425ae4ae8bd02b79656b8f7039f4cd6874095e",
            "config_path": str(config.config_path) if config.config_path else None,
            "config": config.to_dict(),
            "random_seed": config.seed,
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "platform": platform.platform(),
            "package_versions": package_versions(),
            "data_start": config.data.start.isoformat(),
            "data_end": config.data.end.isoformat(),
            "universe_version": universe_version,
            "factor_list": list(config.factors),
            "failures": {},
            "outputs": {},
        }
        self.payload.update(git_worktree_state(config.repo_root or Path.cwd()))
        _atomic_json(self.payload, self.path)

    def add_output(self, name: str, value: Any) -> None:
        self.payload["outputs"][name] = str(value)
        _atomic_json(self.payload, self.path)

    def add_failures(self, name: str, values: Any) -> None:
        self.payload["failures"][name] = values
        _atomic_json(self.payload, self.path)

    def finish(self, status: str = "success", error: Optional[str] = None) -> None:
        self.payload["status"] = status
        self.payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        if error is not None:
            self.payload["error"] = error
        _atomic_json(self.payload, self.path)


__all__ = [
    "PROVENANCE_FIELDS",
    "RunManifest",
    "git_commit",
    "git_worktree_state",
    "package_versions",
    "set_random_seed",
    "validate_provenance",
]
