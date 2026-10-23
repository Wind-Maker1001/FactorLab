"""Read-only Python 3.9 research checks; not a full Pylint analysis."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType

ROOT = Path(__file__).resolve().parents[3]
SCOPES = ("qbot/research", "tests/research", "examples/research", ".github/scripts/ci")
SECURITY_CHECKER_PATH = ".github/scripts/ci/security_admission_revision.py"
CHECKER_SHA256 = "1ce0b775debcb7961fda9acf3a1b9a329ab21f5d7029fd20d9fcf0fcd7cf219f"
SECURITY_ANCHOR = "docs/research/evidence/c01-status-v1/trust-anchor.json"
SECURITY_WORKFLOW = ".github/workflows/market-admission.yml"
C01_CHECKER_PATH = ".github/scripts/ci/c01_preservation.py"
C01_REGISTRY = "docs/research/evidence/c01-status-v1/version-registry.json"
SECURITY_CONTROL_FILES = frozenset(
    (
        SECURITY_CHECKER_PATH,
        ".github/scripts/ci/research_checks.py",
        C01_CHECKER_PATH,
        C01_REGISTRY,
    )
)


def verified_security_control(root):
    """Trust the reviewed CI workflow, never a candidate checker's own receipt."""
    root = Path(root)
    trusted_workflow = (ROOT / SECURITY_WORKFLOW).read_text(encoding="utf-8")
    pin = re.findall(
        r"SECURITY_TRUST_ANCHOR_SHA256: '([0-9a-f]{64})'", trusted_workflow
    )
    try:
        if len(pin) != 1:
            raise ValueError("missing independent workflow pin")
        raw = (root / SECURITY_ANCHOR).read_bytes()
        if hashlib.sha256(raw).hexdigest() != pin[0]:
            raise ValueError("trust anchor changed")
        anchor = json.loads(raw)
        if (
            set(anchor) != {"schema", "files"}
            or anchor["schema"] != "FACTORLAB_C01_SOLO_CONTROL_ANCHOR_V1"
        ):
            raise ValueError("trust anchor schema changed")
        if set(anchor["files"]) != SECURITY_CONTROL_FILES:
            raise ValueError("trust anchor scope changed")
        bodies = {}
        for name, digest in anchor["files"].items():
            body = (root / name).read_bytes().replace(b"\r\n", b"\n")
            if hashlib.sha256(body).hexdigest() != digest:
                raise ValueError("control bytes changed: " + name)
            bodies[name] = body
        if hashlib.sha256(bodies[SECURITY_CHECKER_PATH]).hexdigest() != CHECKER_SHA256:
            raise ValueError("checker changed")
        return bodies
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(
            "Independent security checker pin mismatch: " + str(exc)
        ) from exc


def load_security_revision(root):
    """Verify exact bytes before executing any candidate authorization code."""
    root = Path(root)
    # Original isolated CI fixtures contain neither a revision nor a checker.
    candidate = (
        root
        if (
            (root / SECURITY_CHECKER_PATH).exists()
            or (
                root
                / "docs/research/evidence/unified-market-admission-v1/security-revision.json"
            ).exists()
        )
        else ROOT
    )
    bodies = verified_security_control(candidate)
    module = ModuleType("independently_verified_security_revision")
    module.__file__ = str(candidate / C01_CHECKER_PATH)
    exec(compile(bodies[C01_CHECKER_PATH], module.__file__, "exec"), module.__dict__)
    module.configure(
        candidate,
        hashlib.sha256(bodies[C01_REGISTRY]).hexdigest(),
        {name: hashlib.sha256(body).hexdigest() for name, body in bodies.items()},
        (ROOT / SECURITY_WORKFLOW).read_bytes().replace(b"\r\n", b"\n"),
    )
    if set(module.FILES) & (
        SECURITY_CONTROL_FILES | {SECURITY_WORKFLOW, SECURITY_ANCHOR}
    ):
        raise ValueError(
            "Security checker cannot authorize its independent trust controls"
        )
    return module


def git(root, *arguments):
    return subprocess.check_output(
        ["git", *arguments], cwd=root, text=True, encoding="utf-8"
    ).strip()


def resolve_base(root, event_name, pr_base="", push_before=""):
    """Use the event's advertised baseline, with an explicit first-push fallback."""
    candidate = pr_base if event_name == "pull_request" else push_before
    if candidate and candidate != "0" * 40:
        if not re.fullmatch(r"[0-9a-f]{40}", candidate):
            raise ValueError("Event baseline must be a full commit SHA")
        git(root, "rev-parse", "--verify", candidate + "^{commit}")
    else:
        candidate = "origin/main"
        git(root, "rev-parse", "--verify", candidate + "^{commit}")
    return git(root, "merge-base", candidate, "HEAD")


def check_whitespace(root, base, scopes):
    # Frozen references deliberately retain CRLF bytes. Recognize the line
    # ending while still rejecting trailing spaces and the other default rules.
    subprocess.run(
        [
            "git",
            "-c",
            "core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol",
            "diff",
            "--check",
            "--ignore-submodules",
            base,
            "HEAD",
            "--",
            *scopes,
        ],
        cwd=root,
        check=True,
    )


def main():
    if sys.version_info[:2] != (3, 9):
        raise RuntimeError("Research syntax checks require Python 3.9")
    load_security_revision(ROOT).load_revision(ROOT)
    base = resolve_base(
        ROOT,
        os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch"),
        os.environ.get("PR_BASE_SHA", ""),
        os.environ.get("PUSH_BEFORE_SHA", ""),
    )
    files = sorted(path for scope in SCOPES for path in (ROOT / scope).rglob("*.py"))
    for path in files:
        compile(path.read_bytes(), str(path), "exec")
    subprocess.run(
        [sys.executable, "-m", "flake8", "--select=E9,F63,F7,F82", *SCOPES],
        cwd=ROOT,
        check=True,
    )
    check_whitespace(ROOT, base, SCOPES)
    print(
        json.dumps(
            {
                "base": base,
                "compiled_python_files": len(files),
                "static_rules": "flake8 E9,F63,F7,F82",
                "scopes": SCOPES,
                "modifies_source": False,
                "full_pylint_analysis": False,
            }
        )
    )


if __name__ == "__main__":
    main()
