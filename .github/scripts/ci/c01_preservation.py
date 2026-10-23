"""Finite Solo C01 preservation contract; never a financial qualification issuer.

The caller verifies this module and the registry against the workflow-pinned
anchor before execution. Git objects are immutable inputs; worktree bytes are
always reread. The legacy checker runs only after all current postimages pass.
"""

import copy
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
from types import ModuleType


LEGACY = "cc13795d144592b360e560a9b979e183d6b01907"
SOURCE = "dabdc87b80d3c88ccf56034981a3c5cf0710ed63"
REVIEWED_SOURCE = "2198d393b75fbccfa05d49ada57cdf6703c12bb8"
VERSION = "STATUS_ADAPTER_SOLO_PRESERVATION_V1"
REGISTRY = "docs/research/evidence/c01-status-v1/version-registry.json"
ANCHOR = "docs/research/evidence/c01-status-v1/trust-anchor.json"
OLD_CHECKER = ".github/scripts/ci/security_admission_revision.py"
SCOPES = (
    "qbot/research",
    "tests/research",
    "tests/market_admission",
    "tests/model_readiness",
    "tests/market_structure",
    "tests/evidence",
    "tests/minute_data",
    "tests/conftest.py",
    "docs/research",
    "configs/research",
    "examples/research",
    ".github/scripts/ci",
    ".github/workflows",
    "pytest.ini",
    "dev/requirements-research.txt",
    "dev/requirements-research-ci.txt",
    "dev/requirements-evidence-ci.txt",
)
# These are finite control/test deltas, not directory exemptions. The anchor
# separately authenticates the loader, this module, registry and legacy checker.
STATUS_FILES = frozenset(
    (
        "docs/research/evidence/c01-solo-v1/.gitattributes",
        "docs/research/evidence/c01-solo-v1/trust-anchor.json",
        "docs/research/evidence/c01-solo-v1/version-registry.json",
        "docs/research/evidence/c01-status-v1/.gitattributes",
        "qbot/research/universe/historical_status.py",
        "qbot/research/universe/.gitattributes",
        "qbot/research/universe/status_candidate/.gitattributes",
        "examples/research/diagnose_historical_status.py",
        "qbot/research/universe/status_candidate/adapter.py",
        "qbot/research/universe/status_candidate/ingest.py",
        "qbot/research/universe/status_candidate/model.py",
        "qbot/research/universe/status_candidate/__init__.py",
        "qbot/research/universe/status_candidate/__main__.py",
        "tests/research/status_candidate/conftest.py",
        "tests/research/status_candidate/test_integration.py",
        "tests/research/status_candidate/test_preservation.py",
        "tests/research/status_candidate/test_toctou.py",
        "tests/research/status_candidate/__init__.py",
    )
)

MIGRATION_FILES = STATUS_FILES | frozenset(
    (
        "qbot/research/data/csmar/.gitattributes",
        "docs/research/evidence/c01-status-v1/.gitattributes",
        "tests/research/test_five_year_frozen_baseline.py",
        "tests/research/test_c01_source_reachability.py",
        "docs/architecture/decisions/ADR-C01-SOLO-MAINTAINER.md",
        "tests/market_structure/test_delivery.py",
        "tests/market_admission/test_revision_contract.py",
        "tests/research/test_c01_preservation.py",
        "examples/research/review_yhh30m_release.py",
        "tests/market_structure/test_c01_publication_safety.py",
    )
)
CONTROL_FILES = frozenset(
    (
        ".github/scripts/ci/research_checks.py",
        OLD_CHECKER,
        ".github/scripts/ci/c01_preservation.py",
        REGISTRY,
    )
)
WORKFLOW = ".github/workflows/market-admission.yml"


def digest(body):
    return hashlib.sha256(body).hexdigest()


def normalized(body):
    return body.replace(b"\r\n", b"\n")


def blob_identity(body):
    return hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()


def canonical_name(name):
    return (
        isinstance(name, str)
        and name not in ("", ".")
        and PurePosixPath(name).as_posix() == name
        and not PurePosixPath(name).is_absolute()
        and not any(part in ("", ".", "..") for part in name.split("/"))
        and not any(char in name for char in ("\\", ":", "\0", "\n", "\r"))
    )


def checked_entry(name, item):
    if (
        not canonical_name(name)
        or not isinstance(item, dict)
        or set(item) != {"blob", "sha256", "mode"}
        or not isinstance(item["blob"], str)
        or re.fullmatch(r"[0-9a-f]{40}", item["blob"]) is None
        or not isinstance(item["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
        or item["mode"] not in ("100644", "100755")
    ):
        raise ValueError("Invalid C01 authenticated file entry: " + str(name))


def checked_mode(path, name, wanted, index):
    if index.get(name) != wanted:
        raise ValueError("C01 registered Git file mode mismatch: " + name)
    if os.name != "nt" and (
        path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    ) != (0o111 if wanted == "100755" else 0):
        raise ValueError("C01 runtime file mode mismatch: " + name)


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate C01 JSON key: " + name)
        result[name] = value
    return result


def git(root, *args):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GIT_"):
            env.pop(key)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return subprocess.check_output(["git", *args], cwd=root, env=env)


def safe_file(root, name):
    if not canonical_name(name):
        raise ValueError("Noncanonical C01 path")
    path = PurePosixPath(name)
    target = root / name
    current = target
    while current != root:
        if current.is_symlink():
            raise ValueError("C01 symlink is forbidden: " + name)
        current = current.parent
    if not target.is_file() or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Missing C01 protected file: " + name)
    return target


@lru_cache(maxsize=16)
def object_files(common, commit):
    """Cache only exact immutable Git objects, never observed working bytes."""
    if commit != LEGACY:
        raise ValueError("C01 arbitrary Git ref forbidden")
    freeze = json.loads(
        git(
            common,
            "show",
            LEGACY
            + ":docs/research/evidence/five-year-data-feasibility-v1/frozen-baseline.json",
        )
    )
    frozen_paths = tuple(item["path"] for item in freeze["files"])
    if len(frozen_paths) != 470 or len(set(frozen_paths)) != 470:
        raise ValueError("C01 predecessor freeze inventory changed")
    listing = git(common, "ls-tree", "-r", "-z", commit, "--", *SCOPES, *frozen_paths)
    objects = {}
    for row in listing.split(b"\0"):
        if not row:
            continue
        metadata, name = row.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ValueError("Unsupported C01 Git entry")
        objects[name.decode("utf-8")] = {"blob": oid, "mode": mode}
    ordered = sorted(objects)
    request = "".join(objects[p]["blob"] + "\n" for p in ordered).encode()
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GIT_"):
            env.pop(key)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    result = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=common,
        input=request,
        stdout=subprocess.PIPE,
        env=env,
        check=True,
    )
    payload, cursor, files = result.stdout, 0, {}
    for name in ordered:
        end = payload.index(b"\n", cursor)
        oid, kind, length = payload[cursor:end].decode().split()
        length = int(length)
        body = payload[end + 1 : end + 1 + length]
        if (
            oid != objects[name]["blob"]
            or kind != "blob"
            or len(body) != length
            or blob_identity(body) != oid
        ):
            raise ValueError("C01 Git object stream mismatch")
        files[name] = {
            "blob": oid,
            "sha256": digest(normalized(body)),
            "mode": objects[name]["mode"],
        }
        cursor = end + length + 2
    if cursor != len(payload):
        raise ValueError("Trailing C01 Git object data")
    return files


def source_inventory(root, value):
    """Authenticate LEGACY objects and overlay the reviewed finite manifest.

    SOURCE is provenance metadata, not a required or replayed Git object. The
    independent workflow pin authenticates this manifest and controller. This
    proves registered content preservation, not the missing commit's ancestry.
    """
    common = (
        git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        .decode()
        .strip()
    )
    before = object_files(common, LEGACY)
    changes = value["source_changes"]
    if not isinstance(changes, dict) or len(changes) != 29:
        raise ValueError("C01 source change inventory changed")
    for name, item in changes.items():
        checked_entry(name, item)
        if (
            not any(name == scope or name.startswith(scope + "/") for scope in SCOPES)
            or before.get(name) == item
        ):
            raise ValueError("C01 source change scope changed")
    after = {**before, **changes}
    authentication = value["source_authentication"]
    if authentication != {
        "scheme": "WORKFLOW_PINNED_LEGACY_PLUS_29_POSTIMAGES_V2",
        "source_commit_role": "PROVENANCE_METADATA_NOT_REPLAYED",
        "git_object_replay_required": False,
        "inventory_sha256": digest(
            json.dumps(after, sort_keys=True, separators=(",", ":")).encode()
        ),
    }:
        raise ValueError("C01 authenticated source inventory mismatch")
    return before, after


def source_content(root, name):
    """Return actual content only after matching its authenticated source entry."""
    _, after = source_inventory(root, read_registry(root))
    if name not in after:
        raise ValueError("Unknown C01 source path")
    body = normalized(safe_file(Path(root), name).read_bytes())
    if (
        digest(body) != after[name]["sha256"]
        or blob_identity(body) != after[name]["blob"]
    ):
        raise ValueError("C01 source content identity mismatch")
    return body


def configure(root, registry_sha256, control_hashes, workflow_body):
    """Called only by the already authenticated controller."""
    global _registry_sha256, _control_hashes, _workflow_body, _anchor_body, _legacy, BASE
    global FILES, RECEIPT, RECEIPT_SHA256, COVERAGE_FILES, PRESERVATION_FILES
    _registry_sha256 = registry_sha256
    _control_hashes = dict(control_hashes)
    _workflow_body = workflow_body
    _anchor_body = normalized((Path(root) / ANCHOR).read_bytes())
    _legacy = ModuleType("c01_immutable_legacy_checker")
    _legacy.__file__ = str(Path(root) / OLD_CHECKER)
    exec(
        compile((Path(root) / OLD_CHECKER).read_bytes(), _legacy.__file__, "exec"),
        _legacy.__dict__,
    )
    BASE, FILES = _legacy.BASE, _legacy.FILES
    RECEIPT, RECEIPT_SHA256 = _legacy.RECEIPT, _legacy.RECEIPT_SHA256
    COVERAGE_FILES = _legacy.COVERAGE_FILES
    before, after = source_inventory(root, read_registry(root))
    PRESERVATION_FILES = (
        frozenset(before)
        | frozenset(after)
        | MIGRATION_FILES
        | CONTROL_FILES
        | {ANCHOR}
    )


def read_registry(root, version=VERSION):
    if version != VERSION:
        raise ValueError("Unregistered C01 version; no fallback or Git refs")
    raw = safe_file(root, REGISTRY).read_bytes()
    if digest(normalized(raw)) != _registry_sha256:
        raise ValueError("C01 version registry digest changed")
    value = json.loads(raw, object_pairs_hook=unique_object)
    if (
        set(value)
        != {
            "schema",
            "version",
            "governance",
            "legacy_commit",
            "source_commit",
            "source_changes",
            "source_authentication",
            "migration_postimages",
            "source_closure",
            "legacy_qualification_identity",
            "execution_delta",
            "coverage_valid_lines",
            "financial_recertification",
            "publication_derivatives",
            "status_increment",
        }
        or value["schema"] != "FACTORLAB_C01_STATUS_REGISTRY_V1"
        or value["version"] != VERSION
        or value["governance"] != "ADR-C01-SOLO-MAINTAINER"
        or value["legacy_commit"] != LEGACY
        or value["source_commit"] != SOURCE
        or value["financial_recertification"] is not False
        or set(value["migration_postimages"]) != MIGRATION_FILES
    ):
        raise ValueError("C01 version registry schema/scope changed")
    increment = value["status_increment"]
    if (
        set(increment)
        != {
            "predecessor_registry_sha256",
            "predecessor_anchor_sha256",
            "source_files",
            "migration_origin",
        }
        or digest(
            normalized(
                safe_file(
                    root, "docs/research/evidence/c01-solo-v1/version-registry.json"
                ).read_bytes()
            )
        )
        != increment["predecessor_registry_sha256"]
        or digest(
            normalized(
                safe_file(
                    root, "docs/research/evidence/c01-solo-v1/trust-anchor.json"
                ).read_bytes()
            )
        )
        != increment["predecessor_anchor_sha256"]
    ):
        raise ValueError("C01 STATUS predecessor identity changed")
    sources = {
        name
        for name in STATUS_FILES
        if name.startswith("qbot/") and name.endswith(".py")
    }
    if set(increment["source_files"]) != sources:
        raise ValueError("C01 STATUS source closure changed")
    for name, wanted in increment["source_files"].items():
        if digest(safe_file(root, name).read_bytes()) != wanted:
            raise ValueError("C01 STATUS runtime bytes changed: " + name)
    return value


def validate_preservation(root, version=VERSION):
    root = Path(root).resolve()
    value = read_registry(root, version)
    if normalized(safe_file(root, ANCHOR).read_bytes()) != _anchor_body:
        raise ValueError("C01 control anchor changed")
    for name, wanted in _control_hashes.items():
        if digest(normalized(safe_file(root, name).read_bytes())) != wanted:
            raise ValueError("C01 control bytes changed: " + name)
    if normalized(safe_file(root, WORKFLOW).read_bytes()) != _workflow_body:
        raise ValueError("C01 workflow differs from trusted controller")
    before, after = source_inventory(root, value)
    publication = value["publication_derivatives"]
    if (
        set(publication) != {"reviewed_source_commit", "accepted_c01_commit", "files"}
        or publication["reviewed_source_commit"] != REVIEWED_SOURCE
        or publication["accepted_c01_commit"]
        != "f7a4490f5ecd6c64fda96794d343b51e1caa307c"
        or len(publication["files"]) != 17
    ):
        raise ValueError("C01 publication origin changed")
    originals, copies = set(), set()
    for item in publication["files"]:
        original, public = item["original_path"], item["public_path"]
        if (
            original in originals
            or public in copies
            or original in before
            or original in after
            or original in value["source_closure"]
            or item["original_commit"] != REVIEWED_SOURCE
            or not original.startswith("docs/research/csmar-source-binding-v2/")
            or not public.startswith("docs/research/csmar-source-binding-v2-public/")
            or after.get(public, {}).get("sha256") != item["public_sha256"]
        ):
            raise ValueError("C01 publication derivative mismatch")
        originals.add(original)
        copies.add(public)
    expected = dict(after)
    for name, item in value["migration_postimages"].items():
        checked_entry(name, item)
    expected.update(value["migration_postimages"])
    # Anchor/loader/registry are authenticated as a chain; never self-hashed in
    # the registry. All predecessor anchors and receipts remain in expected.
    for name in CONTROL_FILES | {WORKFLOW}:
        expected.pop(name, None)
    index = {}
    for row in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if row:
            metadata, name = row.split(b"\t", 1)
            mode, _, stage = metadata.decode().split()
            if stage != "0":
                raise ValueError("C01 unmerged Git index is forbidden")
            index[name.decode("utf-8")] = mode
    for name, item in expected.items():
        path = safe_file(root, name)
        body = normalized(path.read_bytes())
        if digest(body) != item["sha256"]:
            raise ValueError("Unregistered security admission bytes: " + name)
        if name in value["source_changes"] or name in value["migration_postimages"]:
            if blob_identity(body) != item["blob"]:
                raise ValueError("C01 registered blob identity mismatch: " + name)
        checked_mode(path, name, item["mode"], index)
    for name in CONTROL_FILES | {WORKFLOW, ANCHOR}:
        checked_mode(safe_file(root, name), name, "100644", index)
    # New protected files require finite registration too. Git ignore rules do
    # not hide unknown files; only Python bytecode/cache files are non-source.
    allowed = set(expected) | CONTROL_FILES | {WORKFLOW, ANCHOR}
    for scope in SCOPES:
        directory = root / scope
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValueError("C01 protected symlink forbidden")
            if path.is_file() and "__pycache__" not in path.parts:
                name = path.relative_to(root).as_posix()
                if name not in allowed:
                    raise ValueError("Unregistered C01 protected addition: " + name)
    closure = value["source_closure"]
    if len(closure) != 35 or len(set(closure)) != 35:
        raise ValueError("C01 source closure scope changed")
    for name, item in closure.items():
        if {key: after.get(name, {}).get(key) for key in ("blob", "sha256")} != {
            key: item[key] for key in ("blob", "sha256")
        }:
            raise ValueError("C01 source closure object mismatch")
        wanted = item["runtime_sha256"].get(platform.system())
        if not wanted or digest(safe_file(root, name).read_bytes()) != wanted:
            raise ValueError("C01 exact runtime source bytes mismatch: " + name)
    return value


def legacy_revision(root):
    """Fixed predecessor control view, only after current bytes pass."""
    if digest(safe_file(Path(root), RECEIPT).read_bytes()) != RECEIPT_SHA256:
        raise ValueError("Security admission receipt digest changed")
    value = validate_preservation(root)
    original_blob = _legacy.blob
    common = (
        git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        .decode()
        .strip()
    )
    before = object_files(common, LEGACY)
    overrides = set(value["migration_postimages"]) & FILES
    _legacy.blob = lambda r, name: (
        before[name]["blob"] if name in overrides else original_blob(r, name)
    )
    try:
        return _legacy.load_revision(root)
    finally:
        _legacy.blob = original_blob


def load_revision(root):
    root = Path(root)
    # The pre-existing independent CI unit fixtures have no admission control.
    # This is absence of a contract in a synthetic repo, never version fallback.
    if not (root / RECEIPT).exists() and not (root / OLD_CHECKER).exists():
        return None
    revision = legacy_revision(root)
    if revision is None:
        raise ValueError("Missing C01 predecessor receipt")
    result = copy.deepcopy(revision)
    value = read_registry(root)
    for name, item in {
        **value["source_changes"],
        **value["migration_postimages"],
    }.items():
        result["files"][name] = {"approved_blob": item["blob"]}
    for name in CONTROL_FILES | {WORKFLOW, ANCHOR}:
        body = normalized((root / name).read_bytes())
        oid = hashlib.sha1(
            b"blob " + str(len(body)).encode() + b"\0" + body
        ).hexdigest()
        result["files"][name] = {"approved_blob": oid}
    return result


def historical_protected_view(root, output):
    """Archival text only, including the exact original qualification source."""
    root, output = Path(root), Path(output)
    revision = legacy_revision(root)
    release = json.loads(
        (
            root
            / "docs/research/evidence/audit-pit-release-20261018-v1/release-manifest.json"
        ).read_bytes()
    )
    names = set(revision["files"]) | {"qbot/research/data/market_qualification.py"}
    snapshots = {}
    for item in release["protected_contracts"]:
        name = item["path"]
        if name not in names:
            continue
        commit = LEGACY if name.endswith("market_qualification.py") else BASE
        content = normalized(git(root, "show", commit + ":" + name))
        if digest(content) != item["normalized_text_sha256"]:
            raise ValueError("C01 archived preimage mismatch: " + name)
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        snapshots[name] = target
    return snapshots


def coverage_contract(root, expected):
    """Append fixed tests/lines without removing legacy nodes or skip rules."""
    if not (Path(root) / RECEIPT).exists() and not (Path(root) / OLD_CHECKER).exists():
        return expected
    legacy_revision(root)
    value = read_registry(Path(root))
    result = copy.deepcopy(expected)
    delta = value["execution_delta"]
    for key in ("all_ids", "selected_ids", "deselected_ids"):
        if (
            expected[key + "_sha256"] != delta["legacy"][key + "_sha256"]
            or expected[key + "_count"] != delta["legacy"][key + "_count"]
        ):
            raise ValueError("C01 collection predecessor mismatch")
        added = delta["added"][key]
        old_path = (
            Path(root)
            / "docs/research/evidence/ci-runtime-optimization-v1"
            / (key + ".txt")
        )
        old = old_path.read_text(encoding="utf-8").splitlines()
        combined = sorted(old + added)
        if set(old) & set(added) or len(combined) != len(set(combined)):
            raise ValueError("C01 duplicate/removed legacy test")
        result[key + "_count"] = len(combined)
        result[key + "_sha256"] = digest(("\n".join(combined) + "\n").encode())
    for contract in result["execution_by_platform"].values():
        contract["coverage_valid_lines"].update(
            json.loads((Path(root) / RECEIPT).read_bytes())["coverage_valid_lines"]
        )
        contract["coverage_valid_lines"].update(value["coverage_valid_lines"])
        outcomes = {
            node: "passed"
            for node in (
                (
                    Path(root)
                    / "docs/research/evidence/ci-runtime-optimization-v1/selected_ids.txt"
                )
                .read_text(encoding="utf-8")
                .splitlines()
                + delta["added"]["selected_ids"]
            )
        }
        for node in contract["skipped_nodeids"]:
            outcomes[node] = "skipped"
        contract["outcomes_sha256"] = digest(
            json.dumps(outcomes, sort_keys=True).encode()
        )
    return result
