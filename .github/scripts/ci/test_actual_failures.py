"""Run real synthetic failures and worker crashes, never research success evidence."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ActualFailurePropagationTests(unittest.TestCase):
    def run_failure(self, source, workers):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subject = root / "synthetic-subject"
            tests = subject / "tests/research"
            tests.mkdir(parents=True)
            (subject / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            (tests / "test_failure.py").write_text(source, encoding="utf-8")
            (tests / "test_other.py").write_text(
                "def test_other_worker():\n    assert True\n", encoding="utf-8"
            )
            subprocess.run(["git", "init", "-q", str(subject)], check=True)
            subprocess.run(["git", "add", "."], cwd=subject, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Synthetic failure contract",
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
            output = root / "actual-synthetic-failure"
            process = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("benchmark.py")),
                    "--no-coverage",
                    "--workers",
                    str(workers),
                    "--output",
                    str(output),
                ],
                cwd=subject,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
            )
            self.assertNotEqual(
                process.returncode, 0, process.stdout.decode(errors="replace")
            )
            result = json.loads((output / "result.json").read_text())
            self.assertEqual(result["exit_code"], process.returncode)
            if workers:
                controller = json.loads((output / "pytest-controller.json").read_text())
                self.assertTrue(controller["worker_errors"])

    def test_real_test_failure_reaches_parent_process(self):
        self.run_failure(
            "def test_deliberate_failure():\n    assert False, 'synthetic failure contract'\n",
            0,
        )

    def test_real_worker_crash_reaches_parent_process_without_restart(self):
        self.run_failure(
            "import os\ndef test_deliberate_crash():\n    os._exit(17)\n", 2
        )


if __name__ == "__main__":
    unittest.main()
