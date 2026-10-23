"""Synthetic contract failures are isolated from real research CI evidence."""

import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

from benchmark import write_json
from validate_execution import load, validate


class ExecutionEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.TemporaryDirectory()
        cls.root = Path(cls.sandbox.name)
        subject = cls.root / "subject"
        (subject / "tests/research").mkdir(parents=True)
        # An existing parent config and an external output directory reproduced
        # a real root-discovery bug when the observer option used a split value.
        (cls.root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (subject / "pytest.ini").write_text(
            "[pytest]\nmarkers =\n    network: synthetic contract only\n",
            encoding="utf-8",
        )
        (subject / "tests/research/test_synthetic_contract.py").write_text(
            "import pytest\n"
            '@pytest.mark.parametrize("value", [1], ids=["nested::[id]"])\n'
            "def test_actual_call(value):\n    assert value == 1\n"
            '@pytest.mark.skip(reason="synthetic contract skip")\n'
            "def test_actual_skip():\n    raise AssertionError\n"
            "@pytest.mark.network\n"
            "def test_deselected_network():\n    raise AssertionError\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", str(subject)], check=True)
        subprocess.run(["git", "add", "."], cwd=subject, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Synthetic CI contract",
                "-c",
                "user.email=ci@example.invalid",
                "commit",
                "-qm",
                "synthetic fixture",
            ],
            cwd=subject,
            check=True,
            stdout=subprocess.PIPE,
        )
        cls.original = cls.root / "actual-synthetic-execution"
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("benchmark.py")),
                "--no-coverage",
                "--output",
                str(cls.original),
            ],
            cwd=subject,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        result = load(cls.original / "result.json")
        cls.expected = {key: value for key, value in result.items() if "_ids_" in key}
        cls.expected["pytest_ini_sha256"] = load(cls.original / "environment.json")[
            "pytest_ini_sha256"
        ]
        controller = load(cls.original / "pytest-controller.json")
        skip = next(
            nodeid
            for nodeid in controller["selected_ids"]
            if nodeid.endswith("::test_actual_skip")
        )
        outcomes = {
            nodeid: "skipped" if nodeid == skip else "passed"
            for nodeid in controller["selected_ids"]
        }
        family = load(cls.original / "environment.json")["platform"].split("-", 1)[0]
        cls.expected["execution_by_platform"] = {
            family: {
                "outcomes_sha256": hashlib.sha256(
                    json.dumps(outcomes, sort_keys=True).encode()
                ).hexdigest(),
                "skipped_nodeids": [skip],
                "junit_skip_messages": {skip: "synthetic contract skip"},
                "coverage_valid_lines": {},
                "coverage_covered_lines": {},
                "coverage_lines_covered": 0,
            }
        }

    @classmethod
    def tearDownClass(cls):
        cls.sandbox.cleanup()

    def setUp(self):
        self.expected = copy.deepcopy(type(self).expected)
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "copied-synthetic-evidence"
        shutil.copytree(self.original, self.directory)

    def tearDown(self):
        self.temporary.cleanup()

    def mutate(self, filename, mutation):
        path = self.directory / filename
        value = load(path)
        mutation(value)
        write_json(path, value)

    def reject(self):
        for policy in ("experimental-strict", "production-complete"):
            with self.subTest(policy=policy):
                with self.assertRaises((ValueError, FileNotFoundError, ET.ParseError)):
                    validate(self.directory, self.expected, policy=policy)

    def test_real_observer_and_junit_agree_on_nested_parameter_ids(self):
        result = validate(self.directory, self.expected)
        self.assertTrue(
            all(
                nodeid.startswith("tests/research/")
                for nodeid in load(self.directory / "pytest-controller.json")[
                    "selected_ids"
                ]
            )
        )
        self.assertEqual(
            (result["passed"], result["skipped"], result["deselected"]), (1, 1, 1)
        )

    def test_pytest_failure_cannot_be_relabelled(self):
        self.mutate("result.json", lambda value: value.update(exit_code=1))
        self.reject()

    def test_worker_crash_is_a_failure(self):
        self.mutate(
            "pytest-controller.json",
            lambda value: value["worker_errors"].append(
                {"worker": "gw0", "error": "crashed"}
            ),
        )
        self.reject()

    def test_missing_worker_evidence_fails(self):
        self.mutate("environment.json", lambda value: value.update(workers=2))
        self.reject()

    def test_different_frozen_collection_fails(self):
        self.mutate(
            "pytest-controller.json",
            lambda value: value["all_ids"].append(
                "tests/research/test_omitted.py::test_missing"
            ),
        )
        self.reject()

    def test_missing_execution_fails(self):
        def remove(value):
            nodeid = value["reports"][0]["nodeid"]
            value["reports"] = [
                row for row in value["reports"] if row["nodeid"] != nodeid
            ]

        self.mutate("pytest-controller.json", remove)
        self.reject()

    def test_duplicate_execution_fails(self):
        self.mutate(
            "pytest-controller.json",
            lambda value: value["reports"].append(copy.deepcopy(value["reports"][0])),
        )
        self.reject()

    def test_a_failed_test_phase_fails(self):
        self.mutate(
            "pytest-controller.json",
            lambda value: value["reports"][0].update(outcome="failed"),
        )
        self.reject()

    def test_missing_junit_fails(self):
        (self.directory / "research.xml").unlink()
        self.reject()

    def test_damaged_junit_fails(self):
        (self.directory / "research.xml").write_text("<broken", encoding="utf-8")
        self.reject()

    def test_duplicate_junit_case_fails(self):
        path = self.directory / "research.xml"
        root = ET.parse(path)
        suite = root.getroot().find("testsuite")
        suite.append(copy.deepcopy(suite.find("testcase")))
        root.write(path)
        self.reject()

    def test_false_junit_totals_fail(self):
        path = self.directory / "research.xml"
        root = ET.parse(path)
        root.getroot().find("testsuite").set("tests", "9000")
        root.write(path)
        self.reject()

    def test_collection_receipt_tampering_fails(self):
        (self.directory / "selected_ids.txt").write_bytes(b"placeholder\n")
        self.reject()

    def test_collection_only_is_not_acceptance(self):
        self.mutate("environment.json", lambda value: value.update(collect_only=True))
        self.reject()

    def test_hotspot_subset_is_not_acceptance(self):
        self.mutate(
            "environment.json",
            lambda value: value.update(
                execution_scope="hotspot_profile_not_full_acceptance"
            ),
        )
        self.reject()

    def test_missing_coverage_fails(self):
        self.mutate("environment.json", lambda value: value.update(coverage=True))
        self.reject()

    def test_dirty_subject_is_rejected(self):
        self.mutate("environment.json", lambda value: value.update(subject_dirty=True))
        self.reject()

    def test_linux_artifact_can_be_verified_on_windows(self):
        contract = next(iter(self.expected["execution_by_platform"].values()))
        self.expected["execution_by_platform"] = {"Linux": contract}
        self.mutate(
            "environment.json",
            lambda value: value.update(
                platform="Linux-synthetic",
                subject_root="/synthetic/subject",
                worker_state_root="/synthetic/resources",
            ),
        )
        self.mutate(
            "worker-resources-controller.json",
            lambda value: value.update(
                directory="/synthetic/resources/controller",
                MPLCONFIGDIR="/synthetic/resources/controller/matplotlib",
            ),
        )
        self.mutate(
            "pytest-controller.json",
            lambda value: value.update(
                rootpath="/synthetic/subject", inipath="/synthetic/subject/pytest.ini"
            ),
        )
        self.assertEqual(validate(self.directory, self.expected)["passed"], 1)

    def test_parent_pytest_configuration_is_rejected(self):
        self.mutate(
            "pytest-controller.json",
            lambda value: value.update(inipath="/parent/pytest.ini"),
        )
        self.reject()

    def test_wrong_subject_is_rejected(self):
        with self.assertRaises(ValueError):
            validate(self.directory, self.expected, subject_sha="0" * 40)

    def test_wrong_controller_is_rejected(self):
        with self.assertRaises(ValueError):
            validate(self.directory, self.expected, controller_sha="0" * 40)

    def test_wrong_tree_is_rejected(self):
        with self.assertRaises(ValueError):
            validate(self.directory, self.expected, subject_tree="0" * 40)

    def synthetic_coverage(self):
        """Deliberate unit-only XML, never uploaded as research measurement."""
        self.mutate("environment.json", lambda value: value.update(coverage=True))
        (self.directory / "coverage.xml").write_text(
            '<coverage lines-valid="2" lines-covered="1" line-rate="0.5">'
            "<sources><source>/synthetic/qbot/research</source></sources>"
            '<packages><package><classes><class filename="synthetic_contract.py">'
            '<lines><line number="1" hits="1"/><line number="2" hits="0"/></lines>'
            "</class></classes></package></packages></coverage>",
            encoding="utf-8",
        )
        contract = next(iter(self.expected["execution_by_platform"].values()))
        contract.update(
            coverage_valid_lines={"synthetic_contract.py": [1, 2]},
            coverage_covered_lines={"synthetic_contract.py": [1]},
            coverage_lines_covered=1,
        )

    def test_coverage_inventory_and_aggregates_agree(self):
        self.synthetic_coverage()
        self.assertEqual(
            validate(self.directory, self.expected)["coverage_lines_covered"], 1
        )

    def test_false_coverage_aggregate_fails(self):
        self.synthetic_coverage()
        path = self.directory / "coverage.xml"
        path.write_text(
            path.read_text().replace('hits="1"', 'hits="0"'), encoding="utf-8"
        )
        self.reject()

    def test_omitted_coverage_file_fails(self):
        self.synthetic_coverage()
        contract = next(iter(self.expected["execution_by_platform"].values()))
        contract["coverage_valid_lines"]["omitted_source.py"] = [1]
        self.reject()

    def test_production_records_coverage_differences_without_a_new_numeric_floor(self):
        self.synthetic_coverage()
        contract = next(iter(self.expected["execution_by_platform"].values()))
        contract["coverage_lines_covered"] = 2
        contract["coverage_covered_lines"]["synthetic_contract.py"] = [1, 2]
        with self.assertRaisesRegex(ValueError, "Covered-line regression"):
            validate(self.directory, self.expected)
        self.assertFalse((self.directory / "validation.json").exists())
        result = validate(self.directory, self.expected, policy="production-complete")
        self.assertEqual(result["policy"], "production-complete")
        self.assertFalse(result["coverage_floor_applied"])
        self.assertEqual(
            result["coverage_line_differences"],
            {"synthetic_contract.py": {"lost": [2], "gained": []}},
        )

    def test_production_cannot_replace_a_reference_experiment_or_adopt_workers(self):
        with self.assertRaisesRegex(ValueError, "substitute"):
            validate(
                self.directory,
                self.expected,
                reference=self.original,
                policy="production-complete",
            )
        self.mutate("environment.json", lambda value: value.update(workers=2))
        with self.assertRaisesRegex(ValueError, "serial backend"):
            validate(self.directory, self.expected, policy="production-complete")

    def test_changed_skip_reason_fails(self):
        def change(value):
            for report in value["reports"]:
                if report["outcome"] == "skipped":
                    report["skip_reason"] = "Skipped: another reason"

        self.mutate("pytest-controller.json", change)
        self.reject()

    def test_additional_skip_fails(self):
        def change(value):
            for report in value["reports"]:
                if report["when"] == "call":
                    report.update(
                        outcome="skipped", skip_reason="Skipped: unauthorized skip"
                    )

        self.mutate("pytest-controller.json", change)
        path = self.directory / "research.xml"
        root = ET.parse(path)
        suite = root.getroot().find("testsuite")
        suite.set("skipped", "2")
        for case in suite.findall("testcase"):
            if case.find("skipped") is None:
                ET.SubElement(case, "skipped", message="unauthorized skip")
        root.write(path)
        self.reject()


if __name__ == "__main__":
    unittest.main()
