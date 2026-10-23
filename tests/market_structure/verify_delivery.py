"""Explicit evidence assembly, outside the production collection. Python 3.9."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

from setuptools import find_packages

from factorlab.exploratory.market_structure.contracts import (
    BASELINE,
    MARKERS,
    SCHEMA,
    json_bytes,
)
from factorlab.exploratory.market_structure.runner import verify_receipt

ROOT = Path(__file__).resolve().parents[2]
PREFIXES = (
    "qbot/research/exploratory/market_structure/",
    "tests/market_structure/",
    "docs/research/market_structure/",
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--formal-reports", type=Path, required=True)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    target = ROOT / "docs/research/market_structure/evidence/v1"
    if target.exists():
        raise ValueError("new evidence directory required")
    coverage = json.loads((artifacts / "coverage.json").read_text())
    coverage["files"] = {k.replace("\\", "/"): v for k, v in coverage["files"].items()}
    expected = {
        p.relative_to(ROOT).as_posix() for p in (ROOT / PREFIXES[0]).glob("*.py")
    }
    measured = {k.replace("\\", "/") for k in coverage["files"]}
    if measured != expected:
        raise ValueError("independent coverage source inventory mismatch")
    xml = ET.parse(artifacts / "coverage.xml").getroot()
    measured_xml = {
        "qbot/research/exploratory/market_structure/"
        + c.attrib["filename"].split("market_structure/")[-1]
        for c in xml.findall(".//class")
    }
    if measured_xml != expected:
        raise ValueError("coverage XML source inventory mismatch")
    junit = ET.parse(artifacts / "new-tests.xml").getroot()
    suites = list(junit.iter("testsuite"))
    counts = {
        k: sum(int(s.attrib[k]) for s in suites)
        for k in ("tests", "failures", "errors", "skipped")
    }
    if counts["tests"] <= 0 or any(
        counts[k] for k in ("failures", "errors", "skipped")
    ):
        raise ValueError("independent tests did not all execute successfully")
    formal = json.loads((artifacts / "frozen-validation.json").read_text())
    if formal["policy"] != "production-complete" or formal["failed"] != 0:
        raise ValueError("formal frozen execution did not validate")
    formal_reports = args.formal_reports.resolve()
    formal_suites = list(
        ET.parse(formal_reports / "research.xml").getroot().iter("testsuite")
    )
    formal_counts = {
        k: sum(int(s.attrib[k]) for s in formal_suites)
        for k in ("tests", "failures", "errors", "skipped")
    }
    expected_count = json.loads(
        (
            ROOT
            / "docs/research/evidence/ci-runtime-optimization-v1/baseline-collection.json"
        ).read_text()
    )["selected_ids_count"]
    if formal_counts["tests"] != expected_count or any(
        formal_counts[k] for k in ("failures", "errors", "skipped")
    ):
        raise ValueError("formal JUnit does not prove full successful execution")
    if formal_counts["tests"] != formal["passed"]:
        raise ValueError("formal JUnit/validator mismatch")
    for filename in ("static.json", "source-cli.json"):
        evidence = json.loads((artifacts / filename).read_text())
        if evidence["exit_code"] != 0:
            raise ValueError("failed explicit command: " + filename)
    receipt = verify_receipt(artifacts / "synthetic")
    if set(receipt["frequencies"]) != {5, 15, 30}:
        raise ValueError("all frequencies required")
    changed = subprocess.check_output(
        ["git", "diff", BASELINE, "--name-status"], cwd=ROOT, text=True
    ).splitlines()
    if any(
        not line.startswith("A\t") or not line.split("\t")[1].startswith(PREFIXES)
        for line in changed
    ):
        raise ValueError("protected files changed")
    files = {}
    for prefix in PREFIXES:
        for path in (ROOT / prefix).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                files[path.relative_to(ROOT).as_posix()] = sha(path)
    target.mkdir(parents=True)
    shutil.copytree(artifacts / "synthetic", target / "synthetic")
    for filename in (
        "new-tests.xml",
        "coverage.xml",
        "coverage.json",
        "new-tests.log",
        "static.json",
        "static.log",
        "source-cli.json",
        "source-cli.log",
        "frozen-validation.json",
        "environment-notes.json",
        "determinism.json",
        "frozen-scope.json",
    ):
        shutil.copyfile(artifacts / filename, target / filename)
    shutil.copyfile(formal_reports / "research.xml", target / "frozen-tests.xml")
    shutil.copyfile(
        formal_reports / "environment.json", target / "frozen-environment.json"
    )
    inventory = {
        name: dict(source_sha256=sha(ROOT / name), **coverage["files"][name]["summary"])
        for name in sorted(expected)
    }
    (target / "coverage-source-inventory.json").write_bytes(json_bytes(inventory))
    packaging = dict(
        python=sys.version,
        namespace_has_init=False,
        source_entry_verified=True,
        found_from_repo="factorlab.exploratory.market_structure"
        in find_packages(where=str(ROOT)),
        found_from_qbot="research.exploratory.market_structure"
        in find_packages(where=str(ROOT / "qbot")),
        packaging_status="NOT_VALIDATED",
        limitation="find_packages omits this namespace; wheel/install entry not accepted",
    )
    (target / "packaging-audit.json").write_bytes(json_bytes(packaging))
    acceptance = dict(
        schema=SCHEMA,
        **MARKERS,
        software_status="SOFTWARE_MVP_SUCCESS",
        real_research_status="REAL_RESEARCH_BLOCKED",
        baseline=BASELINE,
        implementation_commit=receipt["tested_commit"],
        python=sys.version,
        independent_junit=counts,
        independent_coverage=coverage["totals"],
        coverage_inventory_verified=True,
        static_scopes=[PREFIXES[0], PREFIXES[1]],
        source_cli_frequencies=[5, 15, 30],
        formal_validation=formal,
        formal_scope=json.loads((artifacts / "frozen-scope.json").read_text()),
        contract_count=32,
        implemented_count=14,
        contract_only_count=18,
        installation_package_status="NOT_VALIDATED",
        source_and_test_sha256=files,
        claim="software synthetic acceptance only; no predictive ability claim"
    )
    (target / "acceptance.json").write_bytes(json_bytes(acceptance))
    manifest = {}
    for prefix in PREFIXES:
        for path in sorted((ROOT / prefix).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                manifest[path.relative_to(ROOT).as_posix()] = sha(path)
    manifest_path = target / "delivery-manifest.json"
    manifest_path.write_bytes(
        json_bytes(
            dict(
                self_hash_policy="manifest excludes itself; SHA256 reported separately",
                files=manifest,
            )
        )
    )
    print(
        json.dumps(
            dict(
                software_status="SOFTWARE_MVP_SUCCESS",
                real_research_status="REAL_RESEARCH_BLOCKED",
                tests=counts,
                manifest_sha256=sha(manifest_path),
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
