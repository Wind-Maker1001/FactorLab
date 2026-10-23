"""Real Git fixtures prove exact migration cannot widen the financial freeze."""

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from frozen_engineering_migration import (
    ENGINEERING_PATHS,
    assert_fixture_functions_equal,
    git,
    validate_migration,
    worktree_blob,
)


class ExactEngineeringMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Synthetic migration contract")
        git(self.root, "config", "user.email", "ci@example.invalid")
        git(self.root, "config", "core.autocrlf", "false")
        paths = ENGINEERING_PATHS | {"qbot/research/financial_gate.py"}
        for path in paths:
            destination = self.root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"original\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "synthetic historical baseline")
        baseline = git(self.root, "rev-parse", "HEAD")
        self.frozen = {
            "baseline_commit": baseline,
            "files": [
                {"path": path, "baseline_git_blob_sha1": worktree_blob(self.root, path)}
                for path in sorted(paths)
            ],
        }
        self.manifest = "historical-freeze.json"
        (self.root / self.manifest).write_bytes(json.dumps(self.frozen).encode())
        self.manifest_blob = worktree_blob(self.root, self.manifest)
        for path in ENGINEERING_PATHS:
            (self.root / path).write_bytes(b"reviewed engineering change\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "synthetic engineering anchor")
        self.receipt = self.root / "engineering-evidence/migration.json"
        self.receipt.parent.mkdir()
        original = {entry["path"]: entry for entry in self.frozen["files"]}
        self.registry = {
            "baseline_commit": baseline,
            "engineering_commit": git(self.root, "rev-parse", "HEAD"),
            "files": {
                path: {
                    "original_blob": original[path]["baseline_git_blob_sha1"],
                    "approved_blob": worktree_blob(self.root, path),
                }
                for path in sorted(ENGINEERING_PATHS)
            },
        }
        self.write_receipt()

    def tearDown(self):
        self.temporary.cleanup()

    def write_receipt(self):
        self.receipt.write_bytes((json.dumps(self.registry, indent=2) + "\n").encode())
        self.digest = hashlib.sha256(self.receipt.read_bytes()).hexdigest()

    def validate(self):
        return validate_migration(
            self.root,
            self.frozen,
            self.manifest,
            self.manifest_blob,
            self.receipt,
            self.digest,
        )

    def test_only_exact_reviewed_postimages_are_accepted(self):
        self.assertEqual(self.validate()["unchanged_financial_and_other_files"], 1)

    def test_any_fifth_frozen_file_change_is_rejected(self):
        (self.root / "qbot/research/financial_gate.py").write_bytes(b"weakened\n")
        with self.assertRaisesRegex(ValueError, "Frozen research"):
            self.validate()

    def test_renaming_a_frozen_financial_file_cannot_hide_the_original_path(self):
        git(
            self.root,
            "mv",
            "qbot/research/financial_gate.py",
            "qbot/research/relabeled.py",
        )
        with self.assertRaisesRegex(ValueError, "Frozen research"):
            self.validate()

    def test_deleting_a_frozen_financial_file_is_rejected(self):
        (self.root / "qbot/research/financial_gate.py").unlink()
        with self.assertRaisesRegex(ValueError, "Frozen research"):
            self.validate()

    def test_any_registered_file_byte_drift_is_rejected(self):
        for path in ENGINEERING_PATHS:
            original = (self.root / path).read_bytes()
            (self.root / path).write_bytes(original + b"drift")
            with self.assertRaisesRegex(ValueError, "Unregistered engineering"):
                self.validate()
            (self.root / path).write_bytes(original)

    def test_historical_manifest_tampering_is_rejected(self):
        (self.root / self.manifest).write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "Historical freeze manifest"):
            self.validate()

    def test_changing_migration_receipt_does_not_authorize_bytes(self):
        self.receipt.write_bytes(self.receipt.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "receipt digest"):
            self.validate()

    def test_expanding_receipt_scope_fails_even_with_new_digest(self):
        self.registry["files"]["qbot/research/financial_gate.py"] = {}
        self.write_receipt()
        with self.assertRaisesRegex(ValueError, "scope changed"):
            self.validate()

    def test_changed_postimage_cannot_change_the_pinned_receipt_digest(self):
        self.registry["files"][next(iter(ENGINEERING_PATHS))]["approved_blob"] = (
            "0" * 40
        )
        self.receipt.write_bytes((json.dumps(self.registry, indent=2) + "\n").encode())
        with self.assertRaisesRegex(ValueError, "receipt digest"):
            self.validate()

    def test_squashed_offline_clone_needs_no_engineering_branch_object(self):
        git(self.root, "add", ".")
        tree = git(self.root, "write-tree")
        squashed = git(
            self.root,
            "commit-tree",
            tree,
            "-p",
            self.frozen["baseline_commit"],
            "-m",
            "synthetic squashed main",
        )
        git(self.root, "update-ref", "refs/heads/squashed", squashed)
        clone = self.root / "offline-single-branch-clone"
        git(
            self.root,
            "clone",
            "--no-local",
            "--single-branch",
            "--branch",
            "squashed",
            "-c",
            "core.autocrlf=false",
            str(self.root),
            str(clone),
        )
        with self.assertRaises(subprocess.CalledProcessError):
            git(
                clone,
                "cat-file",
                "-e",
                self.registry["engineering_commit"] + "^{commit}",
            )
        copied_receipt = clone / self.receipt.relative_to(self.root)
        validate_migration(
            clone,
            self.frozen,
            self.manifest,
            self.manifest_blob,
            copied_receipt,
            self.digest,
        )
        changed = clone / next(iter(ENGINEERING_PATHS))
        changed.write_bytes(changed.read_bytes() + b"unapproved drift")
        with self.assertRaisesRegex(ValueError, "Unregistered engineering"):
            validate_migration(
                clone,
                self.frozen,
                self.manifest,
                self.manifest_blob,
                copied_receipt,
                self.digest,
            )
        copied_receipt.write_bytes(copied_receipt.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "receipt digest"):
            validate_migration(
                clone,
                self.frozen,
                self.manifest,
                self.manifest_blob,
                copied_receipt,
                self.digest,
            )

    def test_git_clean_filter_preserves_crlf_compatibility(self):
        git(self.root, "config", "core.autocrlf", "true")
        for path in ENGINEERING_PATHS:
            (self.root / path).write_bytes(b"reviewed engineering change\r\n")
        self.validate()

    def test_real_crlf_checkout_preserves_exact_receipt_digest(self):
        attributes = (
            Path(__file__).resolve().parents[3]
            / "docs/research/evidence/ci-runtime-optimization-v1/.gitattributes"
        )
        (self.receipt.parent / ".gitattributes").write_bytes(attributes.read_bytes())
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "synthetic pinned migration receipt")
        clone = self.root / "actual-crlf-checkout"
        git(self.root, "clone", "--shared", "--no-checkout", str(self.root), str(clone))
        git(clone, "config", "core.autocrlf", "true")
        git(clone, "checkout", "--detach", "HEAD")
        copied_receipt = clone / self.receipt.relative_to(self.root)
        self.assertEqual(copied_receipt.read_bytes(), self.receipt.read_bytes())
        self.assertIn(b"\r\n", (clone / next(iter(ENGINEERING_PATHS))).read_bytes())
        validate_migration(
            clone,
            self.frozen,
            self.manifest,
            self.manifest_blob,
            copied_receipt,
            self.digest,
        )


class FixtureBodyPreservationTests(unittest.TestCase):
    ORIGINAL = "def _inputs():\n    return list(range(390))\ndef test_assertions():\n    assert _inputs()[0] == 0\n"
    CANDIDATE = ORIGINAL.replace("def _inputs():", "def _build_inputs():")

    def test_renaming_only_the_recipe_preserves_original_bodies(self):
        self.assertEqual(
            len(assert_fixture_functions_equal(self.ORIGINAL, self.CANDIDATE)), 2
        )

    def test_changed_assertion_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "test_assertions"):
            assert_fixture_functions_equal(
                self.ORIGINAL, self.CANDIDATE.replace("== 0", "== 1")
            )

    def test_reduced_input_size_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "_inputs"):
            assert_fixture_functions_equal(
                self.ORIGINAL, self.CANDIDATE.replace("390", "39")
            )


if __name__ == "__main__":
    unittest.main()
