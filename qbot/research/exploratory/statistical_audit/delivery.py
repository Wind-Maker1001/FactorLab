"""Immutable audit receipts and explicit derivative-only publication."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import platform
import csv
import re

from ..contract import (
    ExploratoryContractError,
    REQUIRED_MARKERS,
    assert_exploratory_only,
    sha256_file,
)
from ..runner import _safe_output_root
from ...reproducibility import package_versions
from .inputs import ROOT, BASELINE, DATA_VERSION
from .statistics import BLOCK, REPEATS, SEED, LAGS, MIN_DAYS
from .schemas import CSV_COLUMNS


MARKERS = tuple(REQUIRED_MARKERS) + ("NON_PIT_CONVENIENCE",)
PUBLIC_FILES = frozenset(
    {
        "report.md",
        "input_audit.json",
        "daily_input_audit.csv",
        "rankic_descriptive.csv",
        "rankic_inference.csv",
        "rankic_method_sensitivity.csv",
        "regime_means.csv",
        "regime_comparisons.csv",
        "regime_intervals.csv",
        "quintile_daily.csv",
        "quintile_spread.csv",
        "quintile_monthly.csv",
        "rankic_intervals.png",
        "regime_intervals.png",
        "quintile_sensitivity.png",
        "temporal_coverage.png",
    }
)
PARAMETERS = dict(
    hac_lags=list(LAGS),
    block_sessions=BLOCK,
    bootstrap_repeats=REPEATS,
    random_seed=SEED,
    minimum_interpretation_days=MIN_DAYS,
    mean_family_size=14,
    state_difference_family_size=42,
    alpha=0.05,
    confidence_level=0.95,
    missing_policy="PRESERVE_FULL_SESSION_AXIS",
    bootstrap="NON_CIRCULAR_OVERLAPPING_MOVING_BLOCKS_JOINT_DATE_TUPLES",
    bootstrap_p="CENTERED_NULL_ABSOLUTE_TAIL_PLUS_ONE",
    ci="HAC_NORMAL_POINTWISE_OR_BOOTSTRAP_PERCENTILE_POINTWISE",
    factor_direction="UNCHANGED",
    label="FROZEN_EXCESS_RET5_T_PLUS_1_TO_6",
)


def identity(payload):
    return hashlib.sha256(
        json.dumps(
            {k: v for k, v in payload.items() if k != "receipt_sha256"},
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def write_json(path, payload):
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def code_evidence():
    paths = list(Path(__file__).parent.glob("*.py"))
    hashes = {p.relative_to(ROOT).as_posix(): sha256_file(p) for p in sorted(paths)}
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", BASELINE, revision], cwd=ROOT, check=True
    )
    return dict(
        git_revision_at_run=revision,
        baseline_commit=BASELINE,
        source_sha256=hashes,
        source_hash_policy="EXACT_SOURCE_BYTES_INCLUDING_LINE_ENDINGS",
        python=platform.python_version(),
        platform=platform.platform(),
        package_versions=package_versions(
            ("numpy", "pandas", "scipy", "pyarrow", "matplotlib")
        ),
    )


def write_receipt(
    destination, status, evidence, code, reasons, public=False, private_hash=None
):
    receipt = dict(
        schema=(
            "EXPLORATORY_STATISTICAL_AUDIT_PUBLIC_V1"
            if public
            else "EXPLORATORY_STATISTICAL_AUDIT_PRIVATE_V1"
        ),
        status=status,
        purpose="EXPLORATORY",
        qualification="UNVERIFIED",
        historical_status="HISTORICALLY_RECONSTRUCTED",
        sample_classification="NON_PIT_CONVENIENCE",
        formal_admission=False,
        forward_allowed=False,
        live_allowed=False,
        synthetic_only=False,
        markers=list(MARKERS),
        raw_source_bytes_published=False,
        data_version=evidence.get("data_version"),
        input_audit=evidence,
        code_and_environment=code,
        parameters=PARAMETERS,
        reasons=reasons,
        private_audit_receipt_sha256=private_hash,
        artifacts={
            p.name: sha256_file(p)
            for p in sorted(Path(destination).iterdir())
            if p.is_file() and p.name != "receipt.json"
        },
    )
    receipt["receipt_sha256"] = identity(receipt)
    write_json(Path(destination) / "receipt.json", receipt)
    return receipt


def verify_audit(path, public=False):
    path = Path(path)
    r = json.loads(path.read_text(encoding="utf-8"))
    assert_exploratory_only(r)
    expected = (
        "EXPLORATORY_STATISTICAL_AUDIT_PUBLIC_V1"
        if public
        else "EXPLORATORY_STATISTICAL_AUDIT_PRIVATE_V1"
    )
    if r.get("schema") != expected or r.get("receipt_sha256") != identity(r):
        raise ExploratoryContractError("audit schema or semantic hash mismatch")
    if (
        r.get("status") not in {"SUCCESS", "BLOCKED"}
        or r.get("forward_allowed") is not False
        or r.get("live_allowed") is not False
        or r.get("synthetic_only") is not False
        or r.get("raw_source_bytes_published") is not False
        or not set(MARKERS).issubset(r.get("markers", []))
    ):
        raise ExploratoryContractError("unsafe audit metadata")
    allowed = (
        PUBLIC_FILES if public else PUBLIC_FILES | {"private_quintile_membership.csv"}
    )
    artifacts = r.get("artifacts", {})
    if not artifacts or not set(artifacts).issubset(allowed):
        raise ExploratoryContractError("non-allowlisted audit artifact")
    if (
        r.get("historical_status") != "HISTORICALLY_RECONSTRUCTED"
        or r.get("sample_classification") != "NON_PIT_CONVENIENCE"
        or r.get("parameters") != PARAMETERS
    ):
        raise ExploratoryContractError("audit frozen metadata/parameters mismatch")
    if r["status"] == "SUCCESS":
        evidence = r.get("input_audit", {})
        if (
            set(artifacts) != set(allowed)
            or r.get("data_version") != DATA_VERSION
            or evidence.get("data_version") != DATA_VERSION
            or evidence.get("source_hashes_verified") is not True
            or evidence.get("security_level_replay_exact") is not True
            or evidence.get("valid_rank_ic_dates") != 236
            or evidence.get("signal_sessions") != 242
            or evidence.get("securities") != 50
            or not r.get("code_and_environment", {}).get("source_sha256")
            or r.get("code_and_environment", {}).get("baseline_commit") != BASELINE
        ):
            raise ExploratoryContractError(
                "complete verified frozen SUCCESS package required"
            )
        if (
            evidence.get("verification_complete") is not True
            or evidence.get("baseline_commit") != BASELINE
            or r["code_and_environment"].get("git_revision_at_run") is None
        ):
            raise ExploratoryContractError("complete input/code verification required")
        hashes = [
            evidence.get(key)
            for key in (
                "manifest_sha256",
                "private_run_receipt_sha256",
                "public_v2_receipt_sha256",
            )
        ]
        if public:
            hashes.append(r.get("private_audit_receipt_sha256"))
        for mapping in (
            evidence.get("input_files"),
            evidence.get("frozen_derived_sha256"),
            evidence.get("frozen_code_and_environment", {}).get("source_sha256"),
            r["code_and_environment"].get("source_sha256"),
        ):
            if not isinstance(mapping, dict) or not mapping:
                raise ExploratoryContractError(
                    "nonempty verified hash collection required"
                )
            hashes.extend(mapping.values())
        if not all(
            re.fullmatch(r"[0-9a-f]{64}", str(h)) for h in hashes
        ) or not re.fullmatch(
            r"[0-9a-f]{40}", str(r["code_and_environment"].get("git_revision_at_run"))
        ):
            raise ExploratoryContractError(
                "valid frozen SHA256 and code revision required"
            )
    if set(p.name for p in path.parent.iterdir() if p.name != "receipt.json") != set(
        artifacts
    ):
        raise ExploratoryContractError("audit artifact collection mismatch")
    for name, digest in artifacts.items():
        artifact = path.parent / name
        if (
            not artifact.is_file()
            or artifact.is_symlink()
            or sha256_file(artifact) != digest
        ):
            raise ExploratoryContractError("audit artifact hash mismatch: " + name)
        if name.endswith(".csv") and name != "private_quintile_membership.csv":
            with artifact.open(encoding="utf-8", newline="") as handle:
                header = next(csv.reader(handle), [])
            if (
                name not in CSV_COLUMNS
                or len(header) != len(set(header))
                or set(header) != set(CSV_COLUMNS[name])
            ):
                raise ExploratoryContractError(
                    "non-public or unexpected CSV columns: " + name
                )
    return r


def publish_audit(private_dir, destination):
    private_dir, destination = Path(private_dir), Path(destination)
    r = verify_audit(private_dir / "receipt.json")
    if destination.exists():
        raise ExploratoryContractError(
            "audit publication is immutable; choose new directory"
        )
    destination.mkdir(parents=True)
    for name in sorted(PUBLIC_FILES.intersection(r["artifacts"])):
        shutil.copyfile(private_dir / name, destination / name)
    write_receipt(
        destination,
        r["status"],
        r["input_audit"],
        r["code_and_environment"],
        r["reasons"],
        public=True,
        private_hash=sha256_file(private_dir / "receipt.json"),
    )
    verify_audit(destination / "receipt.json", public=True)
    return destination


def audit_destination(output_root, evidence, code):
    root = _safe_output_root(output_root)
    key = identity(dict(input=evidence, code=code, parameters=PARAMETERS))[:16]
    destination = root / "statistical-audit" / key
    if destination.exists():
        verify_audit(destination / "receipt.json")
        return destination, True
    destination.mkdir(parents=True)
    return destination, False
