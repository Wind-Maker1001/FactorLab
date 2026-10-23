"""Read-only public-copy and historical Git receipt check; full replay NOT_RUN.

Private source commits are local dependencies, never fetched or published here.
The redacted publication deliberately has different bytes and Git ancestry.
"""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import runpy
import subprocess
import sys


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
PUBLICATION = "docs/research/evidence/pit-publication-20261018-v1/"
RECEIPT = "docs/research/evidence/pit-release-migration-20261018-v1/validation.json"
VERIFIER = "docs/research/evidence/pit-release-migration-20261018-v1/verify_migration.py"
PROTECTED = {"qbot/research", "configs/research", "tests/research",
             ".github/workflows", ".github/scripts"}


class MissingGitObjects(Exception):
    """Local object availability is a prerequisite, not an integrity PASS."""


def require(condition, message):
    if not condition:
        raise ValueError(message)


def git(*args, allowed_returncodes=(0,)):
    # Ignore inherited Git repository/configuration overrides. Git still finds
    # ROOT and ordinary default configuration, including line-ending policy.
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environment.update(GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0",
                       GIT_CONFIG_COUNT="0", GIT_NO_REPLACE_OBJECTS="1",
                       GIT_OPTIONAL_LOCKS="0")
    result = subprocess.run(
        ["git", *args], cwd=ROOT, env=environment, capture_output=True, timeout=30,
    )
    # Some Git versions also use status 1 for object-read errors. Only a clean
    # nonzero result can represent a caller-authorized semantic outcome.
    if (result.returncode not in allowed_returncodes
            or (result.returncode and result.stderr.strip())):
        raise MissingGitObjects("Required Git object or history unavailable: " + args[0])
    return result.stdout


def safe_path(name):
    require(isinstance(name, str) and name and "\\" not in name
            and not PurePosixPath(name).is_absolute() and not PureWindowsPath(name).drive
            and all(part not in ("", ".", "..") for part in name.split("/")),
            "Unsafe receipt path")
    return name


def oid(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value),
            "Expected a full Git object ID")
    return value


def object_bytes(value, kind):
    oid(value)
    require(git("cat-file", "-t", value).decode().strip() == kind,
            "Git object type mismatch: " + value)
    return git("cat-file", kind, value)


def entry(commit, path, kind, expected):
    raw = git("ls-tree", "-z", commit, "--", safe_path(path))
    require(raw.endswith(b"\0") and raw.count(b"\0") == 1,
            "Missing or ambiguous Git tree entry: " + path)
    metadata, actual_path = raw[:-1].split(b"\t", 1)
    mode, actual_kind, actual_oid = metadata.decode().split()
    require(actual_path.decode("utf-8") == path and actual_kind == kind
            and actual_oid == oid(expected)
            and mode == ("040000" if kind == "tree" else "100644"),
            "Recorded Git entry mismatch: " + path)
    return object_bytes(expected, kind)


def check_bytes(raw, item, hash_key="sha256", size_key="bytes"):
    require(hashlib.sha256(raw).hexdigest() == item[hash_key]
            and len(raw) == item[size_key], "Recorded original bytes mismatch: " + item["path"])


def verify_recorded_migration(receipt, manifest):
    require(receipt["schema"] == "PIT_EVIDENCE_MAIN_MIGRATION_V1", "Unexpected migration schema")
    require(receipt["formal_admission"] is False and receipt["no_live_money"] is True,
            "Migration admission boundary changed")
    require(receipt["main_base_commit"] == manifest["main_base_commit"], "Main base binding changed")
    files = receipt["delivery_files"]
    require(len(files) == 43 and len({item["path"] for item in files}) == 43,
            "Expected 43 distinct historical delivery files")
    published = {item["path"]: item for item in manifest["files"]}
    for item in files:
        safe_path(item["path"])
        original = published[item["path"]]
        require(item["sha256"] == original["original_sha256"]
                and item["bytes"] == original["original_bytes"],
                "Receipt/publication original binding mismatch: " + item["path"])
    require(set(receipt["protected_main_trees"]) == PROTECTED, "Protected tree inventory changed")
    require(set(receipt["migration_files"]) == set(published) - {item["path"] for item in files},
            "Migration artifact inventory changed")
    require(receipt["migration_verifier_sha256"] == published[VERIFIER]["original_sha256"],
            "Historical verifier binding changed")

    commits = {key: oid(receipt[key]) for key in
               ("main_base_commit", "source_commit", "cherry_pick_commit")}
    commits["private_source_commit"] = oid(manifest["private_source_commit"])
    headers = {key: object_bytes(value, "commit").split(b"\n\n", 1)[0]
               for key, value in commits.items()}
    if git("rev-parse", "--is-shallow-repository").strip() != b"false":
        raise MissingGitObjects("Shallow checkout cannot verify the recorded migration history")
    parents = [line.split()[1].decode() for line in headers["cherry_pick_commit"].splitlines()
               if line.startswith(b"parent ")]
    require(parents == [commits["main_base_commit"]], "Cherry-pick parent differs from recorded main")
    # No common ancestor (clean status 1) is a history mismatch. Object-read
    # errors, including status 1 with stderr, still require the NOT_RUN result.
    require(git("merge-base", commits["main_base_commit"], "HEAD",
                allowed_returncodes=(0, 1)).decode().strip()
            == commits["main_base_commit"], "Publication is not based on recorded main")

    changed = set(git("diff", "--name-only", commits["main_base_commit"],
                      commits["cherry_pick_commit"]).decode().splitlines())
    require(changed.issubset({item["path"] for item in files}),
            "Unexpected changes in historical cherry-pick")

    for item in files:
        for key in ("source_commit", "cherry_pick_commit"):
            check_bytes(entry(commits[key], item["path"], "blob", item["git_blob"]), item)
    # Bind the original implementation and receipt to the private publication
    # source, never to their intentionally redacted current postimages.
    for path in (VERIFIER, RECEIPT):
        original = published[path]
        blob = git("rev-parse", commits["private_source_commit"] + ":" + path).decode().strip()
        raw = entry(commits["private_source_commit"], path, "blob", blob)
        check_bytes(raw, original, "original_sha256", "original_bytes")
        if path == RECEIPT:
            historical = json.loads(raw)
            for key in ("schema", "formal_admission", "no_live_money", "main_base_commit",
                        "source_commit", "cherry_pick_commit", "delivery_files",
                        "protected_main_trees", "migration_files", "migration_verifier_sha256"):
                require(historical[key] == receipt[key], "Historical receipt field changed: " + key)
    for path, expected in receipt["protected_main_trees"].items():
        for commit in (commits["main_base_commit"], commits["cherry_pick_commit"], "HEAD"):
            entry(commit, path, "tree", expected)
        require(not git("diff", "HEAD", "--name-only", "--", path).strip()
                and not git("ls-files", "--others", "--exclude-standard", "--", path).strip(),
                "Working tree differs from protected main: " + path)
    base_attributes = git("show", commits["main_base_commit"] + ":.gitattributes")
    require(git("show", commits["cherry_pick_commit"] + ":.gitattributes").startswith(base_attributes),
            "Historical migration overwrote main attributes")
    require((ROOT / ".gitattributes").read_bytes().replace(b"\r\n", b"\n") == base_attributes,
            "Frozen main attributes changed")


def verify():
    result = runpy.run_path(str(ROOT / PUBLICATION / "verify_publication.py"))["verify"]()
    receipt = json.loads((ROOT / RECEIPT).read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / PUBLICATION / "manifest.json").read_text(encoding="utf-8"))
    result["full_migration_verification"] = "NOT_RUN_PRIVATE_ORIGINAL_EVIDENCE_REQUIRED"
    result["historical_full_verifier"] = "NOT_RUN_PRIVATE_ORIGINAL_BYTES_AND_HISTORY_REQUIRED"
    try:
        verify_recorded_migration(receipt, manifest)
    except MissingGitObjects as error:
        result["migration_consistency"] = "NOT_RUN_REQUIRED_GIT_OBJECTS_OR_HISTORY_UNAVAILABLE"
        result["reason"] = str(error)
    else:
        result["migration_consistency"] = "PASS_RECORDED_GIT_OBJECTS_ONLY"
        result["historical_delivery_objects_checked"] = len(receipt["delivery_files"])
        result["protected_main_trees_unchanged"] = sorted(receipt["protected_main_trees"])
    return result


def main():
    try:
        result = verify()
    except (ValueError, KeyError, TypeError) as error:
        result = {"migration_consistency": "FAIL", "reason": str(error),
                  "full_migration_verification": "NOT_RUN", "formal_admission": False,
                  "no_live_money": True}
    except (OSError, subprocess.TimeoutExpired):
        result = {"migration_consistency": "NOT_RUN_REQUIRED_LOCAL_DEPENDENCY_UNAVAILABLE",
                  "full_migration_verification": "NOT_RUN", "formal_admission": False,
                  "no_live_money": True}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["migration_consistency"] == "PASS_RECORDED_GIT_OBJECTS_ONLY" else 1


if __name__ == "__main__":
    sys.exit(main())
