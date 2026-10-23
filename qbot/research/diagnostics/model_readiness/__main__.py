"""python -m qbot.research.diagnostics.model_readiness --output <new-directory>"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pandas as pd

from .runner import run_acceptance, save_json, set_row, write_report
from .source_identity import SourceIntegrityError, assert_source_unchanged, source_snapshot
from .synthetic import WARNING


def main():
    parser = argparse.ArgumentParser(description=WARNING)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--size", choices=("small", "scale"), default="small")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--probes", action="store_true", help="also rerun future/reproducibility probes in scale mode")
    parser.add_argument("--skip-comparison-tests", action="store_true")
    args = parser.parse_args()
    report = run_acceptance(args.output, args.size, args.seed, probes=args.size == "small" or args.probes)
    if not args.skip_comparison_tests:
        root = Path(__file__).resolve().parents[4]
        junit = args.output.resolve() / "comparison-tests.xml"
        command = [sys.executable, "-B", "-X", "utf8", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                   "--basetemp=" + str(args.output.resolve() / "pytest-tmp"),
                   "--junitxml=" + str(junit), "tests/model_readiness/test_comparison_contract.py"]
        with (args.output / "comparison-tests.log").open("w", encoding="utf-8") as log:
            completed = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
        suites = list(ET.parse(junit).getroot().iter("testcase")) if junit.exists() else []
        passed = completed.returncode == 0 and len(suites) >= 10 and not any(
            child.tag in ("failure", "error", "skipped") for case in suites for child in case)
        set_row(report, "comparison_counterexamples", "PASS" if passed else "FAIL",
                "真实比较入口反例：%d项；见comparison-tests.xml/log" % len(suites))
        try:
            start = json.loads((args.output / "source-start.json").read_text(encoding="utf-8"))
            final = source_snapshot(root)
            save_json(args.output / "source-final.json", final)
            assert_source_unchanged(start, final)
            report["source_verification"]["final"] = "source-final.json"
        except (SourceIntegrityError, OSError) as exc:
            set_row(report, "input_identity", "FAIL", str(exc))
            report["source_verification"] = {"status": "FAIL", "reason": str(exc)}
            save_json(args.output / "source-final-error.json", report["source_verification"])
        rows = pd.read_csv(args.output / "resources.csv").where(pd.notna, None).to_dict("records")
        write_report(args.output, report, rows)
    print(WARNING)
    print(args.output / "readiness.md")
    return int(any(row["status"] == "FAIL" for row in report["matrix"]))


if __name__ == "__main__":
    raise SystemExit(main())
