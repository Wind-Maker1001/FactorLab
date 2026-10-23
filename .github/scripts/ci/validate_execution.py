"""Cross-check real pytest observations, frozen collection, JUnit and coverage."""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import xml.etree.ElementTree as ET

from benchmark import canonical_ids, write_json
from validate_reports import validate as validate_reports
from research_checks import load_security_revision


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def junit_address(nodeid):
    address, bracket, parameters = nodeid.partition("[")
    parts = address.split("::")
    module = parts[0].replace("/", ".")[:-3]
    return ".".join([module] + parts[1:-1]), parts[-1] + bracket + parameters


def coverage_inventory(path):
    result = {}
    root = ET.parse(path).getroot()
    for source in root.findall(".//class"):
        name = source.attrib["filename"].replace("\\", "/")
        if "qbot/research/" in name:
            name = name.split("qbot/research/", 1)[1]
        if name in result:
            raise ValueError("Duplicate coverage source")
        lines = source.findall("./lines/line")
        result[name] = {
            "valid": sorted(int(line.attrib["number"]) for line in lines),
            "covered": sorted(
                int(line.attrib["number"])
                for line in lines
                if int(line.attrib["hits"]) > 0
            ),
        }
        if len(result[name]["valid"]) != len(set(result[name]["valid"])):
            raise ValueError("Duplicate coverage line")
        if any(int(line.attrib["hits"]) < 0 for line in lines):
            raise ValueError("Negative coverage hits")
    valid = sum(len(value["valid"]) for value in result.values())
    covered = sum(len(value["covered"]) for value in result.values())
    if valid != int(root.attrib["lines-valid"]) or covered != int(
        root.attrib["lines-covered"]
    ):
        raise ValueError("Coverage aggregates do not match actual line hits")
    if valid <= 0 or abs(float(root.attrib["line-rate"]) - covered / valid) > 0.000051:
        raise ValueError("Coverage rate does not match actual line hits")
    return result


def validate(
    directory,
    expected,
    reference=None,
    subject_sha=None,
    controller_sha=None,
    subject_tree=None,
    policy="experimental-strict",
):
    if policy not in ("experimental-strict", "production-complete"):
        raise ValueError("Unknown execution validation policy")
    if policy == "production-complete" and reference is not None:
        raise ValueError("Production validation cannot substitute for an experiment")
    directory = Path(directory)
    # The legacy collection, skip identities and results stay frozen. Only
    # exact P0 source postimages can update their executable-line inventory.
    result = load(directory / "result.json")
    environment = load(directory / "environment.json")
    expected = load_security_revision(
        Path(environment["subject_root"])
    ).coverage_contract(Path(environment["subject_root"]), expected)
    if environment["subject_dirty"]:
        raise ValueError("Measured subject was not clean")
    for key, wanted in (
        ("subject_sha", subject_sha),
        ("controller_sha", controller_sha),
        ("subject_tree", subject_tree),
    ):
        if wanted is not None and environment[key] != wanted:
            raise ValueError("Evidence belongs to a different " + key)
    controller = load(directory / "pytest-controller.json")
    source_path = (
        PureWindowsPath
        if environment["platform"].startswith("Windows-")
        else PurePosixPath
    )
    if controller["rootpath"] != environment["subject_root"] or controller[
        "inipath"
    ] != str(source_path(environment["subject_root"]) / "pytest.ini"):
        raise ValueError("pytest root or configuration did not belong to subject")
    if (
        controller["pytest_ini_sha256"] != expected["pytest_ini_sha256"]
        or environment["pytest_ini_sha256"] != expected["pytest_ini_sha256"]
    ):
        raise ValueError("pytest configuration differs from frozen baseline")
    if result["exit_code"] or controller["exitstatus"] or controller["worker_errors"]:
        raise ValueError("Actual pytest or worker failed")
    if environment["collect_only"]:
        raise ValueError("Collection is not test execution")
    if environment.get("execution_scope", "full_offline_suite") != "full_offline_suite":
        raise ValueError("Hotspot profiling is not full-suite acceptance")
    collectors = [load(path) for path in sorted(directory.glob("pytest-*.json"))]
    collector = next((row for row in collectors if row["all_ids"]), None)
    if collector is None:
        raise ValueError("Missing actual collection")
    for key in ("all_ids", "selected_ids", "deselected_ids"):
        ids = collector[key]
        data = canonical_ids(ids)
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate collected node ID")
        if (
            len(ids) != expected[key + "_count"]
            or hashlib.sha256(data).hexdigest() != expected[key + "_sha256"]
        ):
            raise ValueError("Frozen test collection changed: " + key)
        if (directory / (key + ".txt")).read_bytes() != data:
            raise ValueError("Collection receipt does not match actual observation")
    selected = set(collector["selected_ids"])
    if set(collector["all_ids"]) != selected | set(collector["deselected_ids"]):
        raise ValueError("Selection and deselection do not partition collection")
    workers = environment["workers"]
    if policy == "production-complete" and workers:
        raise ValueError("Production policy requires the adopted serial backend")
    if environment.get("worker_resources_plugin_sha256"):
        resource_root = source_path(environment["worker_state_root"])
        identities = {"controller"} | {"gw" + str(index) for index in range(workers)}
        resource_files = list(directory.glob("worker-resources-*.json"))
        if len(resource_files) != len(identities):
            raise ValueError("Missing or unexpected worker resource evidence")
        for identity in identities:
            resources = load(directory / ("worker-resources-" + identity + ".json"))
            isolated = str(resource_root / identity)
            matplotlib_cache = str(resource_root / identity / "matplotlib")
            if (
                resources["worker"] != identity
                or resources["directory"] != isolated
                or resources["MPLCONFIGDIR"] != matplotlib_cache
                or resources["matplotlib_cache"] not in (None, matplotlib_cache)
                or not resources["configured_before_matplotlib_import"]
                or not resources["finished"]
                or resources["pytest_cache_writer"] != (identity == "controller")
            ):
                raise ValueError("Worker resource isolation was not preserved")
    if environment["coverage"] and controller.get("coverage_failed_workers"):
        raise ValueError("Coverage plugin reported workers without coverage data")
    if workers and environment["coverage"]:
        wanted = {"gw" + str(index) for index in range(workers)}
        receipts = controller.get("coverage_worker_receipts", {})
        if set(receipts) != wanted or any(receipts[key] != key for key in wanted):
            raise ValueError("Every worker must contribute its genuine coverage data")
    worker_rows = [row for row in collectors if row["worker"] != "controller"]
    if len(worker_rows) != workers:
        raise ValueError("Missing or unexpected worker evidence")
    if workers:
        if len(controller["worker_collections"]) != workers:
            raise ValueError("Controller did not observe every worker collection")
        for row in worker_rows:
            if row["exitstatus"] or row["worker_errors"]:
                raise ValueError("Worker failed")
            if any(
                row[key] != controller[key]
                for key in ("rootpath", "inipath", "pytest_ini_sha256")
            ):
                raise ValueError("Worker used a different pytest root or configuration")
            for key in ("all_ids", "selected_ids", "deselected_ids"):
                if row[key] != collector[key]:
                    raise ValueError("Worker collection differs")
            if (
                controller["worker_collections"].get(row["worker"])
                != row["selected_ids"]
            ):
                raise ValueError("Controller and worker collection differ")
        fields = ("nodeid", "when", "outcome", "seconds")
        observed = Counter(
            tuple(report[key] for key in fields) for report in controller["reports"]
        )
        produced = Counter(
            tuple(report[key] for key in fields)
            for row in worker_rows
            for report in row["reports"]
        )
        if observed != produced:
            raise ValueError("Controller reports do not match worker reports")
    phases = defaultdict(dict)
    for report in controller["reports"]:
        nodeid, phase = report["nodeid"], report["when"]
        if nodeid not in selected or phase not in ("setup", "call", "teardown"):
            raise ValueError("Unexpected executed test or phase")
        if phase in phases[nodeid]:
            raise ValueError("Duplicate executed test phase")
        if report["outcome"] not in ("passed", "skipped") or report["wasxfail"]:
            raise ValueError("Failure or unexpected expected-failure marker")
        phases[nodeid][phase] = report
    if set(phases) != selected:
        raise ValueError("Actual execution omitted selected tests")
    outcomes = {}
    skips = {}
    for nodeid, reports in phases.items():
        if "setup" not in reports or "teardown" not in reports:
            raise ValueError("Incomplete test lifecycle")
        setup_skipped = reports["setup"]["outcome"] == "skipped"
        if not setup_skipped and "call" not in reports:
            raise ValueError("Selected test did not execute its call")
        skipped = [row for row in reports.values() if row["outcome"] == "skipped"]
        outcomes[nodeid] = "skipped" if skipped else "passed"
        if skipped:
            skips[nodeid] = [row["skip_reason"] for row in skipped]
    junit = ET.parse(directory / "research.xml").getroot()
    cases = junit.findall(".//testcase")
    addresses = [(case.attrib["classname"], case.attrib["name"]) for case in cases]
    if Counter(addresses) != Counter(junit_address(nodeid) for nodeid in selected):
        raise ValueError("JUnit has missing, duplicate or unexpected tests")
    skip_addresses = {junit_address(key) for key in skips}
    for case in cases:
        if case.find("failure") is not None or case.find("error") is not None:
            raise ValueError("JUnit contains failed test execution")
        address = case.attrib["classname"], case.attrib["name"]
        if case.find("skipped") is not None and address not in skip_addresses:
            raise ValueError("JUnit skip does not match actual execution")
    skipped_cases = sum(case.find("skipped") is not None for case in cases)
    if skipped_cases != len(skips):
        raise ValueError("JUnit and actual skip counts differ")
    suites = [junit] if junit.tag == "testsuite" else list(junit.iter("testsuite"))
    counts = {
        key: sum(int(suite.attrib[key]) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    if counts != {
        "tests": len(selected),
        "failures": 0,
        "errors": 0,
        "skipped": len(skips),
    }:
        raise ValueError("JUnit summary does not match actual execution")
    validation = {
        "kind": "actual_software_execution_equivalence_only",
        "policy": policy,
        "coverage_floor_applied": environment["coverage"]
        and policy == "experimental-strict",
        "subject_sha": environment["subject_sha"],
        "workers": workers,
        "passed": sum(value == "passed" for value in outcomes.values()),
        "failed": 0,
        "skipped": len(skips),
        "deselected": len(collector["deselected_ids"]),
        "skips": skips,
        "phase_count": sum(len(value) for value in phases.values()),
        "outcomes_sha256": hashlib.sha256(
            json.dumps(outcomes, sort_keys=True).encode()
        ).hexdigest(),
    }
    family = environment["platform"].split("-", 1)[0]
    contract = expected["execution_by_platform"][family]
    if validation["outcomes_sha256"] != contract["outcomes_sha256"]:
        raise ValueError("Execution outcomes differ from frozen platform baseline")
    if sorted(skips) != contract["skipped_nodeids"]:
        raise ValueError("Unexpected skipped tests")
    normalized_skips = {
        key: [
            reason.removeprefix("Skipped: ") if reason else reason for reason in reasons
        ]
        for key, reasons in skips.items()
    }
    if normalized_skips != {
        key: [value] for key, value in contract["junit_skip_messages"].items()
    }:
        raise ValueError("Skip reasons differ from frozen platform baseline")
    if environment["coverage"]:
        validation.update(
            validate_reports(directory / "coverage.xml", directory / "research.xml")
        )
        inventory = coverage_inventory(directory / "coverage.xml")
        validation["coverage_source_count"] = len(inventory)
        validation["coverage_line_differences"] = {
            key: {
                "lost": sorted(
                    set(contract["coverage_covered_lines"].get(key, []))
                    - set(value["covered"])
                ),
                "gained": sorted(
                    set(value["covered"])
                    - set(contract["coverage_covered_lines"].get(key, []))
                ),
            }
            for key, value in inventory.items()
            if value["covered"] != contract["coverage_covered_lines"].get(key, [])
        }
        if {key: value["valid"] for key, value in inventory.items()} != contract[
            "coverage_valid_lines"
        ]:
            raise ValueError("Coverage source inventory differs from frozen baseline")
        if (
            policy == "experimental-strict"
            and validation["coverage_lines_covered"]
            < contract["coverage_lines_covered"]
        ):
            raise ValueError("Covered-line regression requires investigation")
        if reference:
            old_inventory = coverage_inventory(Path(reference) / "coverage.xml")
            if {key: value["valid"] for key, value in inventory.items()} != {
                key: value["valid"] for key, value in old_inventory.items()
            }:
                raise ValueError("Coverage lost or changed measured source lines")
            old_validation = load(Path(reference) / "validation.json")
            if (
                validation["coverage_lines_covered"]
                < old_validation["coverage_lines_covered"]
            ):
                raise ValueError("Coverage regression requires investigation")
    if reference:
        old = load(Path(reference) / "validation.json")
        if (
            validation["outcomes_sha256"] != old["outcomes_sha256"]
            or skips != old["skips"]
        ):
            raise ValueError("Test outcomes or skip reasons changed")
    write_json(directory / "validation.json", validation)
    return validation


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--subject-sha")
    parser.add_argument("--controller-sha")
    parser.add_argument("--subject-tree")
    parser.add_argument(
        "--policy",
        choices=("experimental-strict", "production-complete"),
        default="experimental-strict",
    )
    arguments = parser.parse_args()
    print(
        json.dumps(
            validate(
                arguments.directory,
                load(arguments.expected),
                arguments.reference,
                arguments.subject_sha,
                arguments.controller_sha,
                arguments.subject_tree,
                arguments.policy,
            ),
            sort_keys=True,
        )
    )
