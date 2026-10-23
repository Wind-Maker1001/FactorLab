"""Exercise real 2/3-worker collection, coverage merging and private resources."""

import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from benchmark import write_json
from validate_execution import coverage_inventory, load, validate


class ParallelExecutionContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        subject = cls.root / "synthetic-subject"
        (subject / "tests/research").mkdir(parents=True)
        (subject / "qbot/research").mkdir(parents=True)
        (subject / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (subject / ".gitignore").write_text(
            ".coverage*\n__pycache__/\n.pytest_cache/\n", encoding="utf-8"
        )
        (subject / "qbot/__init__.py").write_text("", encoding="utf-8")
        (subject / "qbot/research/__init__.py").write_text(
            "def ping(value):\n    return value + 1\n", encoding="utf-8"
        )
        (subject / "qbot/research/uncovered.py").write_text(
            "def not_called():\n    return False\n", encoding="utf-8"
        )
        for name in ("first", "second", "third"):
            (subject / ("tests/research/test_" + name + ".py")).write_text(
                "from qbot.research import ping\n"
                "def test_actual_call():\n    assert ping(3) == 4\n",
                encoding="utf-8",
            )
        subprocess.run(["git", "init", "-q", str(subject)], check=True)
        subprocess.run(["git", "add", "."], cwd=subject, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Synthetic parallel contract",
                "-c",
                "user.email=ci@example.invalid",
                "commit",
                "-qm",
                "synthetic source",
            ],
            cwd=subject,
            check=True,
            stdout=subprocess.PIPE,
        )
        cls.runs = {}
        for workers in (0, 2, 3):
            output = cls.root / ("actual-synthetic-workers-" + str(workers))
            process = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("benchmark.py")),
                    "--workers",
                    str(workers),
                    "--output",
                    str(output),
                ],
                cwd=subject,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=90,
            )
            if process.returncode:
                raise AssertionError(process.stdout.decode(errors="replace"))
            cls.runs[workers] = output
        serial = cls.runs[0]
        result = load(serial / "result.json")
        environment = load(serial / "environment.json")
        controller = load(serial / "pytest-controller.json")
        inventory = coverage_inventory(serial / "coverage.xml")
        outcomes = {nodeid: "passed" for nodeid in controller["selected_ids"]}
        family = environment["platform"].split("-", 1)[0]
        cls.expected = {key: value for key, value in result.items() if "_ids_" in key}
        cls.expected.update(
            pytest_ini_sha256=environment["pytest_ini_sha256"],
            execution_by_platform={
                family: {
                    "outcomes_sha256": hashlib.sha256(
                        json.dumps(outcomes, sort_keys=True).encode()
                    ).hexdigest(),
                    "skipped_nodeids": [],
                    "junit_skip_messages": {},
                    "coverage_valid_lines": {
                        key: value["valid"] for key, value in inventory.items()
                    },
                    "coverage_covered_lines": {
                        key: value["covered"] for key, value in inventory.items()
                    },
                    "coverage_lines_covered": sum(
                        len(value["covered"]) for value in inventory.values()
                    ),
                }
            },
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.copy = tempfile.TemporaryDirectory()
        self.evidence = Path(self.copy.name) / "synthetic-evidence-copy"
        shutil.copytree(self.runs[2], self.evidence)

    def tearDown(self):
        self.copy.cleanup()

    def test_two_and_three_workers_preserve_every_actual_test_and_coverage_file(self):
        for workers in (0, 2, 3):
            with self.subTest(workers=workers):
                result = validate(self.runs[workers], copy.deepcopy(self.expected))
                self.assertEqual(
                    (result["passed"], result["failed"], result["skipped"]), (3, 0, 0)
                )
                self.assertIn(
                    "uncovered.py",
                    coverage_inventory(self.runs[workers] / "coverage.xml"),
                )

    def test_missing_worker_coverage_is_rejected_even_when_total_coverage_is_unchanged(
        self,
    ):
        path = self.evidence / "pytest-controller.json"
        record = load(path)
        record["coverage_worker_receipts"]["gw1"] = None
        write_json(path, record)
        with self.assertRaisesRegex(ValueError, "contribute.*coverage"):
            validate(self.evidence, self.expected)

    def test_coverage_plugin_failed_worker_warning_becomes_failure(self):
        path = self.evidence / "pytest-controller.json"
        record = load(path)
        record["coverage_failed_workers"] = ["gw1"]
        write_json(path, record)
        with self.assertRaisesRegex(ValueError, "without coverage data"):
            validate(self.evidence, self.expected)

    def test_shared_matplotlib_cache_is_rejected(self):
        path = self.evidence / "worker-resources-gw1.json"
        record = load(path)
        record["MPLCONFIGDIR"] = load(self.evidence / "worker-resources-gw0.json")[
            "MPLCONFIGDIR"
        ]
        write_json(path, record)
        with self.assertRaisesRegex(ValueError, "resource isolation"):
            validate(self.evidence, self.expected)

    def test_missing_worker_resource_evidence_is_rejected(self):
        (self.evidence / "worker-resources-gw1.json").unlink()
        with self.assertRaisesRegex(ValueError, "resource evidence"):
            validate(self.evidence, self.expected)


if __name__ == "__main__":
    unittest.main()
