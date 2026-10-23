"""Verify four exact engineering postimages without changing historical evidence."""

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess


ENGINEERING_PATHS = frozenset(
    (
        ".github/workflows/research-tests.yml",
        ".github/workflows/pylint.yml",
        ".github/workflows/ci-workflows.yml",
        "tests/research/test_cli_phase2.py",
    )
)


def git(root, *arguments):
    return subprocess.check_output(
        ["git", *arguments], cwd=root, encoding="utf-8"
    ).strip()


def worktree_blob(root, path):
    return git(root, "hash-object", "--path=" + path, path)


def assert_fixture_functions_equal(original_bytes, candidate_bytes):
    old_functions = {
        node.name: node
        for node in ast.parse(original_bytes).body
        if isinstance(node, ast.FunctionDef)
    }
    new_functions = {
        node.name: node
        for node in ast.parse(candidate_bytes).body
        if isinstance(node, ast.FunctionDef)
    }
    for name, original in old_functions.items():
        candidate = new_functions[
            (
                "_build_inputs"
                if name == "_inputs" and "_build_inputs" in new_functions
                else name
            )
        ]
        original.name = candidate.name
        if ast.dump(original) != ast.dump(candidate):
            raise ValueError("Original fixture/test function changed: " + name)
    return sorted(old_functions)


def validate_migration(root, frozen, manifest_path, manifest_blob, receipt, digest):
    root = Path(root)
    if worktree_blob(root, manifest_path) != manifest_blob:
        raise ValueError("Historical freeze manifest changed")
    receipt_bytes = Path(receipt).read_bytes()
    if hashlib.sha256(receipt_bytes).hexdigest() != digest:
        raise ValueError("Engineering migration receipt digest changed")
    migration = json.loads(receipt_bytes)
    if migration["baseline_commit"] != frozen["baseline_commit"]:
        raise ValueError("Historical baseline was replaced")
    entries = {entry["path"]: entry for entry in frozen["files"]}
    specification = importlib.util.spec_from_file_location(
        "independent_research_checks",
        Path(__file__).with_name("research_checks.py"),
    )
    controller = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(controller)
    security = controller.load_security_revision(root)
    revision = security.load_revision(root)
    approved = revision["files"] if revision else {}
    changes = set(
        git(
            root,
            "-c",
            "core.quotepath=false",
            "diff",
            "--no-renames",
            "--name-only",
            frozen["baseline_commit"],
        ).splitlines()
    )
    unexpected = set(entries).intersection(changes) - ENGINEERING_PATHS - set(approved)
    if unexpected:
        raise ValueError(
            "Frozen research/Gates/Golden evidence changed: " + str(sorted(unexpected))
        )
    if set(migration["files"]) != ENGINEERING_PATHS:
        raise ValueError("Engineering migration scope changed")
    anchor = migration["engineering_commit"]
    if not re.fullmatch("[0-9a-f]{40}", anchor):
        raise ValueError("Engineering anchor is not an exact commit")
    for path in sorted(ENGINEERING_PATHS):
        item = migration["files"][path]
        if item["original_blob"] != entries[path]["baseline_git_blob_sha1"]:
            raise ValueError("Original engineering preimage differs: " + path)
        expected_blob = approved.get(path, {}).get(
            "approved_blob", item["approved_blob"]
        )
        if worktree_blob(root, path) != expected_blob:
            raise ValueError("Unregistered engineering bytes: " + path)
    return {
        "historical_files": len(entries),
        "unchanged_financial_and_other_files": len(entries)
        - len(ENGINEERING_PATHS | (set(approved) & set(entries))),
        "exact_engineering_postimages": sorted(ENGINEERING_PATHS),
        "exact_security_postimages": sorted(set(approved) & set(entries)),
        "engineering_commit": anchor,
        "engineering_anchor_check_phase": "receipt_generation_and_independent_review",
    }
