"""CI-specific fixtures only; these are not uploaded as real coverage evidence."""

from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

import yaml

from research_checks import check_whitespace, resolve_base
from validate_reports import validate


ROOT = Path(__file__).resolve().parents[3]


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.git("init", "-q")
        self.git("config", "user.name", "CI contract fixture")
        self.git("config", "user.email", "ci-fixture@example.invalid")
        self.git("config", "core.autocrlf", "false")
        (self.root / "input.txt").write_text("first\n", encoding="utf-8")
        self.git("add", "input.txt")
        self.git("commit", "-qm", "first")
        self.first = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.first)

    def tearDown(self):
        self.directory.cleanup()

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.root, text=True, encoding="utf-8"
        ).strip()

    def test_first_push_uses_default_branch_without_head_parent(self):
        self.assertEqual(
            resolve_base(self.root, "push", push_before="0" * 40), self.first
        )

    def test_pr_uses_advertised_base(self):
        (self.root / "input.txt").write_text("second\n", encoding="utf-8")
        self.git("commit", "-qam", "second")
        self.assertEqual(
            resolve_base(self.root, "pull_request", pr_base=self.first), self.first
        )

    def test_push_uses_advertised_before_sha(self):
        self.assertEqual(
            resolve_base(self.root, "push", push_before=self.first), self.first
        )

    def test_invalid_baseline_fails_instead_of_skipping(self):
        with self.assertRaises(ValueError):
            resolve_base(self.root, "pull_request", pr_base="--all")

    def test_missing_commit_fails_instead_of_skipping(self):
        with self.assertRaises(subprocess.CalledProcessError):
            resolve_base(self.root, "push", push_before="f" * 40)

    def test_crlf_is_a_line_ending_not_a_false_whitespace_error(self):
        (self.root / "input.txt").write_bytes(b"valid CRLF\r\n")
        self.git("commit", "-qam", "CRLF fixture")
        check_whitespace(self.root, self.first, ["input.txt"])

    def test_crlf_does_not_hide_actual_trailing_spaces(self):
        (self.root / "input.txt").write_bytes(b"trailing space \r\n")
        self.git("commit", "-qam", "invalid whitespace fixture")
        with self.assertRaises(subprocess.CalledProcessError):
            check_whitespace(self.root, self.first, ["input.txt"])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.coverage = self.root / "synthetic-unit-coverage.xml"
        self.junit = self.root / "synthetic-unit-junit.xml"
        self.coverage.write_text(
            '<coverage lines-valid="2" lines-covered="1" line-rate="0.5">'
            "<sources><source>/fixture/qbot/research</source></sources>"
            '<packages><package><classes><class filename="fixture.py" /></classes></package></packages>'
            "</coverage>",
            encoding="utf-8",
        )
        self.junit.write_text(
            '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0" /></testsuites>',
            encoding="utf-8",
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_reports_have_counts_and_no_invented_threshold(self):
        result = validate(self.coverage, self.junit)
        self.assertEqual(result["coverage_lines_covered"], 1)
        self.assertIsNone(result["coverage_threshold"])

    def test_checkout_root_and_research_filenames_are_valid_sources(self):
        content = self.coverage.read_text(encoding="utf-8")
        content = content.replace("/fixture/qbot/research", "D:\\checkout")
        content = content.replace(
            'filename="fixture.py"', 'filename="qbot/research/fixture.py"'
        )
        self.coverage.write_text(content, encoding="utf-8")
        self.assertEqual(validate(self.coverage, self.junit)["coverage_lines_valid"], 2)

    def test_unrelated_classes_fail_even_under_checkout_root(self):
        content = self.coverage.read_text(encoding="utf-8")
        content = content.replace("/fixture/qbot/research", "/checkout")
        self.coverage.write_text(content, encoding="utf-8")
        with self.assertRaises(ValueError):
            validate(self.coverage, self.junit)

    def test_similar_source_names_are_not_research_coverage(self):
        content = self.coverage.read_text(encoding="utf-8")
        content = content.replace(
            "/fixture/qbot/research", "/fixture/qbot-other/research"
        )
        self.coverage.write_text(content, encoding="utf-8")
        with self.assertRaises(ValueError):
            validate(self.coverage, self.junit)

    def test_path_traversal_cannot_claim_research_source(self):
        content = self.coverage.read_text(encoding="utf-8")
        content = content.replace(
            'filename="fixture.py"', 'filename="../../unrelated.py"'
        )
        self.coverage.write_text(content, encoding="utf-8")
        with self.assertRaises(ValueError):
            validate(self.coverage, self.junit)

    def test_missing_report_fails(self):
        with self.assertRaises(FileNotFoundError):
            validate(self.root / "absent.xml", self.junit)

    def test_placeholder_report_fails(self):
        self.coverage.write_text('<coverage lines-valid="0" />', encoding="utf-8")
        with self.assertRaises(ValueError):
            validate(self.coverage, self.junit)

    def test_failed_tests_cannot_be_published_as_pass(self):
        self.junit.write_text(
            '<testsuite tests="1" failures="1" errors="0" skipped="0" />',
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            validate(self.coverage, self.junit)


class WorkflowTests(unittest.TestCase):
    def load(self, name):
        return yaml.load(
            (ROOT / ".github/workflows" / name).read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )

    def test_core_research_acceptance_is_preserved(self):
        core = self.load("research-tests.yml")
        self.assertEqual(core["permissions"], {"contents": "read"})
        steps = core["jobs"]["phase-1-offline"]["steps"]
        self.assertTrue(
            any(
                step.get("with", {}).get("python-version") == "3.9.25" for step in steps
            )
        )
        run = "\n".join(step.get("run", "") for step in steps)
        execution = next(
            step
            for step in steps
            if step.get("name") == "Run all offline research tests with coverage"
        )
        self.assertEqual(
            shlex.split(execution["run"]),
            [
                "python",
                ".github/scripts/ci/benchmark.py",
                "--output",
                "results/diagnostics/ci/reports",
                "--workers",
                "0",
            ],
        )
        self.assertIn("validate_execution.py", run)
        self.assertIn("--policy production-complete", run)
        self.assertIn("validate_reports.py", run)
        self.assertIn(
            '--subject-sha "$CHECKED_SHA" --controller-sha "$CHECKED_SHA"', run
        )
        self.assertEqual(
            core["jobs"]["phase-1-offline"]["env"]["CI_CONTROLLER_SHA"],
            "${{ github.sha }}",
        )
        self.assertIn("cli audit", run)
        self.assertNotIn("--cov-fail-under", run)
        self.assertFalse(any("continue-on-error" in step for step in steps))

    def test_label_writer_does_not_checkout_pr_head(self):
        labels = self.load("labeler.yml")
        job = labels["jobs"]["triage"]
        self.assertEqual(
            job["permissions"], {"contents": "read", "pull-requests": "write"}
        )
        self.assertTrue(all("run" not in step for step in job["steps"]))
        script = job["steps"][0]["with"]["script"]
        self.assertIn("pull_request.base.sha", script)
        self.assertIn("context.sha", script)
        self.assertNotIn("pull_request.head", script)
        self.assertIn("repos.getContent", script)
        self.assertTrue(all("checkout@" not in step["uses"] for step in job["steps"]))

    def test_checkouts_use_exact_sha_full_history_and_no_stored_credentials(self):
        commands = []
        for name in ("research-tests.yml", "pylint.yml", "ci-workflows.yml"):
            workflow = self.load(name)
            job = next(iter(workflow["jobs"].values()))
            checkout = job["steps"][0]
            command = checkout["run"]
            self.assertEqual(checkout["env"]["CHECKOUT_SHA"], "${{ github.sha }}")
            self.assertIn("GIT_CONFIG_COUNT=1", command)
            self.assertIn("--no-recurse-submodules", command)
            self.assertIn("+refs/heads/*:refs/remotes/origin/*", command)
            self.assertIn('git checkout --detach "$CHECKOUT_SHA"', command)
            self.assertIn("GIT_LFS_SKIP_SMUDGE=1 git checkout", command)
            self.assertNotIn("--depth", command)
            self.assertNotIn("git config", command)
            self.assertNotIn("submodule foreach", command)
            commands.append(command)
        self.assertEqual(len(set(commands)), 1)

    def test_maintained_actions_are_commit_pinned(self):
        for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
            workflow = self.load(path.name)
            for job in workflow["jobs"].values():
                for step in job["steps"]:
                    if "uses" in step:
                        self.assertRegex(step["uses"], r"^[\w./-]+@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
