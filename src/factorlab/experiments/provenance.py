"""Capture explicit checkout provenance without running experiments."""
import platform
from pathlib import Path
import subprocess
import sys

from factorlab._identity import git_environment
from factorlab.reproducibility import git_worktree_state, package_versions


def capture_provenance(repo_root):
    """Return append_run code/environment fields for an explicit checkout."""
    root = Path(repo_root).resolve()
    def revision(name):
        return subprocess.check_output(["git", "-C", str(root), "rev-parse", name],
                                       env=git_environment(), text=True).strip()
    return {
        "code": {"commit": revision("HEAD"), "tree": revision("HEAD^{tree}"),
                 **git_worktree_state(root)},
        "environment": {"python_version": platform.python_version(),
                        "python_executable": sys.executable,
                        "platform": platform.platform(),
                        "package_versions": package_versions()},
    }
