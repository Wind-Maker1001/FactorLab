"""Exact authorized P0 postimages; historical freezes remain immutable.

This revision changes financial admission, not factor/model/statistical logic.
Any further source change requires a new reviewed receipt rather than growing
an exception based on whatever happens to be in the working tree.
"""

import copy
import hashlib
import json
from pathlib import Path
import subprocess


BASE = "657486f8ca928c1e66b430f471cfe5ce96a613ab"
RECEIPT = "docs/research/evidence/unified-market-admission-v1/security-revision.json"
RECEIPT_SHA256 = "f09871e930ff269ecf27e8b03aa5003ef48bfc87f8c12300c4390dfc55107ec2"
FILES = frozenset(
    (
        "qbot/research/cli.py",
        "qbot/research/data/providers/base.py",
        "qbot/research/data/providers/local.py",
        "qbot/research/data/admission.py",
        "tests/conftest.py",
        "tests/research/test_cli_phase2.py",
        "tests/research/test_cli_forward_evaluation.py",
        "tests/model_readiness/test_comparison_contract.py",
        "tests/market_admission/test_admission.py",
        ".github/scripts/ci/frozen_engineering_migration.py",
        ".github/scripts/ci/validate_execution.py",
        "tests/research/test_five_year_frozen_baseline.py",
        "tests/market_structure/test_delivery.py",
        "examples/research/review_yhh30m_release.py",
        "tests/market_admission/test_revision_contract.py",
        "tests/evidence/test_pit_archive_verifiers.py",
        "tests/research/test_phase1_file_pipeline.py",
        ".github/workflows/pit-archive-evidence.yml",
        "tests/market_admission/test_p1_review.py",
    )
)
COVERAGE_FILES = frozenset(
    name.removeprefix("qbot/research/")
    for name in FILES
    if name.startswith("qbot/research/")
)


def blob(root, name):
    return subprocess.check_output(
        ["git", "hash-object", "--path=" + name, name], cwd=root, text=True
    ).strip()


def load_revision(root):
    root = Path(root)
    receipt = root / RECEIPT
    if not receipt.exists():
        return None
    content = receipt.read_bytes()
    if hashlib.sha256(content).hexdigest() != RECEIPT_SHA256:
        raise ValueError("Security admission receipt digest changed")
    revision = json.loads(content)
    if (
        set(revision) != {"schema", "base_commit", "files", "coverage_valid_lines"}
        or revision["schema"] != "FACTORLAB_SECURITY_ADMISSION_REVISION_V1"
        or revision["base_commit"] != BASE
        or set(revision["files"]) != FILES
        or set(revision["coverage_valid_lines"]) != COVERAGE_FILES
    ):
        raise ValueError("Security admission revision scope changed")
    for name, item in revision["files"].items():
        original = subprocess.run(
            ["git", "rev-parse", "--verify", BASE + ":" + name],
            cwd=root,
            text=True,
            capture_output=True,
        )
        before = original.stdout.strip() if original.returncode == 0 else None
        if (
            set(item) != {"before_blob", "approved_blob"}
            or item["before_blob"] != before
        ):
            raise ValueError("Security admission preimage changed: " + name)
        if blob(root, name) != item["approved_blob"]:
            raise ValueError("Unregistered security admission bytes: " + name)
    return revision


def coverage_contract(root, expected):
    """Replace only exact approved executable-line inventories, never outcomes."""
    revision = load_revision(root)
    if revision is None:
        return expected
    result = copy.deepcopy(expected)
    for contract in result["execution_by_platform"].values():
        contract["coverage_valid_lines"].update(revision["coverage_valid_lines"])
    return result


def historical_protected_view(root, output):
    """Replay archival text without pretending current security code is old.

    The immutable archival verifier still checks its original hashes. First
    verify every authorized current postimage, then materialize only approved
    historical preimages. All other files retain the verifier's current reads.
    No financial assertion or archival manifest is replaced.
    """
    root, output = Path(root), Path(output)
    revision = load_revision(root)
    if revision is None:
        raise ValueError("Missing current security revision for archival view")
    release = json.loads(
        (
            root
            / "docs/research/evidence/audit-pit-release-20261018-v1/release-manifest.json"
        ).read_bytes()
    )
    snapshots = {}
    for item in release["protected_contracts"]:
        name = item["path"]
        if name not in revision["files"]:
            continue
        content = subprocess.check_output(["git", "show", BASE + ":" + name], cwd=root)
        normalized = content.replace(b"\r\n", b"\n")
        if hashlib.sha256(normalized).hexdigest() != item["normalized_text_sha256"]:
            raise ValueError(
                "Archived security preimage does not match publication: " + name
            )
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(normalized)
        snapshots[name] = target
    return snapshots
