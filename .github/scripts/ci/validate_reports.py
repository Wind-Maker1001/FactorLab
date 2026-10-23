"""Validate generated software coverage and JUnit, without a coverage threshold."""

import hashlib
import json
import math
from pathlib import Path
import posixpath
import sys
import xml.etree.ElementTree as ET


def read_xml(path):
    content = Path(path).read_bytes()
    if not content or len(content) > 64 * 1024 * 1024:
        raise ValueError("Empty or unexpectedly large CI report")
    return ET.fromstring(content), hashlib.sha256(content).hexdigest()


def validate(coverage_path, junit_path):
    coverage, coverage_sha = read_xml(coverage_path)
    if coverage.tag != "coverage" or not coverage.findall(".//class"):
        raise ValueError("Coverage report lacks measured source classes")
    valid = int(coverage.attrib["lines-valid"])
    covered = int(coverage.attrib["lines-covered"])
    rate = float(coverage.attrib["line-rate"])
    if valid <= 0 or not 0 <= covered <= valid:
        raise ValueError("Invalid measured coverage line counts")
    if not math.isfinite(rate) or not 0 <= rate <= 1:
        raise ValueError("Invalid coverage line rate")
    sources = [node.text or "" for node in coverage.findall("./sources/source")]
    for source_class in coverage.findall(".//class"):
        filename = source_class.get("filename", "")
        if not filename or not any(
            "/qbot/research/"
            in "/"
            + posixpath.normpath(
                source.replace("\\", "/") + "/" + filename.replace("\\", "/")
            ).strip("/")
            for source in sources
        ):
            raise ValueError("Coverage classes must belong to qbot/research")
    junit, junit_sha = read_xml(junit_path)
    suites = [junit] if junit.tag == "testsuite" else list(junit.iter("testsuite"))
    if not suites:
        raise ValueError("Missing JUnit suites")
    counts = {
        name: sum(int(suite.attrib[name]) for suite in suites)
        for name in ("tests", "failures", "errors", "skipped")
    }
    if counts["tests"] <= counts["skipped"] or counts["failures"] or counts["errors"]:
        raise ValueError("JUnit must contain actual successful test execution")
    return {
        "kind": "software_coverage_only_not_financial_certification",
        "coverage_lines_valid": valid,
        "coverage_lines_covered": covered,
        "coverage_line_rate": rate,
        "coverage_sha256": coverage_sha,
        "junit_sha256": junit_sha,
        "junit_counts": counts,
        "coverage_threshold": None,
    }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: validate_reports.py coverage.xml research.xml")
    print(json.dumps(validate(sys.argv[1], sys.argv[2]), sort_keys=True))
