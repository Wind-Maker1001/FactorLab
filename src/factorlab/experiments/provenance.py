"""Capture environment and available checkout provenance without experiments."""
import platform
from pathlib import Path
import subprocess
import sys

from factorlab._identity import git_environment
from factorlab.reproducibility import git_worktree_state, package_versions


def capture_provenance(repo_root):
    """Return append_run identities, with null Git fields outside a checkout."""
    root = Path(repo_root).resolve()
    def revision(name):
        return subprocess.check_output(["git", "-C", str(root), "rev-parse", name],
                                       env=git_environment(), text=True).strip()
    checkout = (root / ".git").exists()
    return {
        "code": {"commit": revision("HEAD") if checkout else None,
                 "tree": revision("HEAD^{tree}") if checkout else None,
                 **(git_worktree_state(root) if checkout else
                    {"git_dirty": None, "git_diff_sha256": None})},
        "environment": {"python_version": platform.python_version(),
                        "python_executable": sys.executable,
                        "platform": platform.platform(),
                        "package_versions": package_versions()},
    }
