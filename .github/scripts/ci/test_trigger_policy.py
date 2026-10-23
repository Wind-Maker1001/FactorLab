"""Event/branch/path contracts; remote event receipts remain separately required."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ("research-tests.yml", "pylint.yml", "ci-workflows.yml")
PATHS = (
    "qbot/research/models/ridge.py",
    "configs/research/csi500_smoke.yaml",
    "dev/requirements-research-ci.txt",
    ".github/workflows/research-tests.yml",
    "docs/research/CI_RUNTIME_OPTIMIZATION.md",
    "README.md",
)


class TriggerPolicyTests(unittest.TestCase):
    def workflows(self):
        for name in WORKFLOWS:
            yield name, yaml.load(
                (ROOT / ".github/workflows" / name).read_text(encoding="utf-8"),
                Loader=yaml.BaseLoader,
            )

    def test_every_relevant_event_has_real_path_independent_entry(self):
        cases = (
            ("push", "main", True),
            ("push", "codex/ci-runtime-optimization", False),
            ("push", "feature/example", False),
            ("push", "refs/tags/v1", False),
            ("pull_request", "main", True),
            ("pull_request", "develop", False),
            ("workflow_dispatch", "codex/ci-runtime-optimization", True),
        )
        for name, workflow in self.workflows():
            events = workflow["on"]
            self.assertEqual(set(events), {"push", "pull_request", "workflow_dispatch"})
            for event, branch, expected in cases:
                for path in PATHS:
                    with self.subTest(
                        workflow=name, event=event, branch=branch, path=path
                    ):
                        config = events[event]
                        if event == "workflow_dispatch":
                            actual = True
                        else:
                            self.assertNotIn("paths", config)
                            self.assertNotIn("paths-ignore", config)
                            self.assertEqual(config["branches"], ["main"])
                            actual = branch in config["branches"]
                        self.assertEqual(actual, expected)

    def test_pr_changes_and_retargeting_are_not_missed(self):
        for name, workflow in self.workflows():
            with self.subTest(workflow=name):
                self.assertEqual(
                    set(workflow["on"]["pull_request"]["types"]),
                    {"opened", "reopened", "synchronize", "edited"},
                )

    def test_only_superseded_same_pr_can_be_cancelled(self):
        for name, workflow in self.workflows():
            with self.subTest(workflow=name):
                concurrency = workflow["concurrency"]
                self.assertEqual(
                    concurrency["group"],
                    "factorlab-${{ github.workflow }}-${{ github.event_name }}-${{ github.event.pull_request.number || github.run_id }}",
                )
                self.assertEqual(
                    concurrency["cancel-in-progress"],
                    "${{ github.event_name == 'pull_request' && github.event.action == 'synchronize' }}",
                )

                def group(event, pr, run):
                    return (name, event, pr or run)

                self.assertEqual(
                    group("pull_request", 9, 1), group("pull_request", 9, 2)
                )
                self.assertNotEqual(
                    group("pull_request", 9, 1), group("pull_request", 10, 2)
                )
                self.assertNotEqual(group("push", None, 1), group("push", None, 2))
                self.assertNotEqual(
                    group("push", None, 1), group("workflow_dispatch", None, 1)
                )

    def test_checks_keep_existing_names_and_direct_failure_propagation(self):
        names = {
            "research-tests.yml": ("phase-1-offline", "Python 3.9 offline suite"),
            "pylint.yml": ("research-static", "Research syntax and static checks"),
            "ci-workflows.yml": ("actionlint", None),
        }
        for name, workflow in self.workflows():
            job_id, check_name = names[name]
            job = workflow["jobs"][job_id]
            self.assertEqual(job.get("name"), check_name)
            self.assertNotIn("continue-on-error", job)
            for step in job["steps"]:
                self.assertNotIn("continue-on-error", step)
            if name != "research-tests.yml":
                self.assertNotIn("if", job)
            else:
                self.assertEqual(
                    job["if"],
                    "github.event_name != 'workflow_dispatch' || inputs.mode != 'benchmark'",
                )


if __name__ == "__main__":
    unittest.main()
