"""Inspect the full release diff for raw artifacts, secrets and machine paths."""

import argparse
import ast
from collections import Counter
import csv
import hashlib
import io
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


URL = re.compile(r"\b(?!file:)[A-Za-z][A-Za-z0-9+.-]*:\x2f\x2f[^\s<>\"'`]*", re.I)
ABS = re.compile(r"(?<![\w])[A-Za-z]:[\\/][^\s\"'<>\[\](){}]+|(?<![\w\\])\\\\[\w.-]+\\[^\s\"'<>\[\](){}]+"
                 r"|(?<![\w.\x2f\\~>*%}-])\x2f[^\s\"'<>\[\](){}|,;]+")
SECRET = re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
GATES = ("qbot/research/data/schema.py", "qbot/research/data/providers/local.py",
         "qbot/research/data/market_qualification.py", "qbot/research/forward/", "qbot/research/config/",
         ".github/workflows/research-tests.yml")

# Audited, immutable V1-V9 provenance and synthetic path vectors. This pin
# permits only their existing path strings/encoding, never secrets or raw data.
PATH_AUDIT = "docs/research/market_structure/evidence/v9/delivery-manifest.json"
PATH_AUDIT_SHA256 = "63144f07169740b78f33873947f28fa8294d856ea028c53e7785f12daea2165a"
PATH_AUDIT_BASELINE = "7300afdb92127aec83f03a5f5fa91b7f71fe94bb"
PATH_RULES = "docs/research/market_structure/PUBLICATION_PATH_RULES.json"
PATH_RULES_SHA256 = "956cf7d1276857b9f65eeed2763c49bf02fab75efb09a12fb66aa672015db851"
LEGACY_LOG = "docs/research/market_structure/evidence/v2/red/red-tests.log"


def minute_release_path(name):
    """Identify minute ownership; safety checks still inspect the full diff."""
    return (name.startswith(("qbot/engineering/yhh30m/", "tests/minute_data/",
                             "docs/research/evidence/yhh30m-", "docs/research/YHH_30M_"))
            or name == ".github/workflows/minute-data-evidence.yml"
            or (name.startswith("examples/research/") and "yhh30m" in Path(name).name))


def strings(value, context=""):
    if isinstance(value, str):
        yield context, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield context + "|KEY", key
            yield from strings(item, context + "|" + key)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item, context)


def canonical_path(name):
    """Git-relative POSIX names only; no aliases, roots or object syntax."""
    return (isinstance(name, str) and name not in ("", ".", "..") and
            not any(c in name for c in (chr(92), ":", "\n", "\r", "\x00")) and
            not PurePosixPath(name).is_absolute() and
            PurePosixPath(name).as_posix() == name and
            not any(part in (".", "..") for part in PurePosixPath(name).parts))


def path_kind(token):
    if re.match(r"^[A-Za-z]:", token):
        return "WINDOWS_DRIVE"
    return "UNC" if token.startswith(chr(92) * 2) else "POSIX_OR_FRAGMENT"


def load_path_rules(repo, blocked):
    """Authenticate frozen provenance without requiring discarded Git objects."""
    path = repo / PATH_RULES
    if not path.exists():
        return {}
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != PATH_RULES_SHA256:
            raise ValueError("rules digest")
        policy = json.loads(raw)
        if (policy["schema"] != "PUBLICATION_PATH_RULES_V1" or
                policy["baseline_commit"] != PATH_AUDIT_BASELINE or
                policy["evidence_manifest_sha256"] != PATH_AUDIT_SHA256):
            raise ValueError("baseline binding")
        manifest = (repo / PATH_AUDIT).read_bytes()
        if hashlib.sha256(manifest).hexdigest() != PATH_AUDIT_SHA256:
            raise ValueError("evidence digest")
        # The pinned rule bytes bind the historical version identifier to the
        # pinned V9 manifest digest. That manifest authenticates file membership
        # and published byte hashes even after a squash discards the old graph.
        published = json.loads(manifest)["git_blob_files"]
        if (not isinstance(published, dict) or
                any(not canonical_path(name) or not isinstance(digest, str) or
                    not re.fullmatch(r"[0-9a-f]{64}", digest)
                    for name, digest in published.items())):
            raise ValueError("frozen source inventory")
        for name, rule in policy["files"].items():
            if (not canonical_path(name) or rule["sha256"] != published.get(name) or
                    not re.fullmatch(r"[0-9a-f]{64}", rule["sha256"]) or
                    rule["encoding"] != ("cp936" if name == LEGACY_LOG else "utf-8-sig") or
                    not rule["path_match_counts"] or
                    any(kind not in {"WINDOWS_DRIVE", "UNC", "POSIX_OR_FRAGMENT"} or
                        type(count) is not int or count <= 0
                        for kind, count in rule["path_match_counts"].items())):
                raise ValueError("noncanonical rule")
        return policy["files"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        blocked.append({"file": PATH_RULES, "reason": "PATH_AUDIT_MANIFEST_MISMATCH"})
        return {}


def matches_frozen_file(raw, rule):
    """Rule membership was authenticated above; verify the current bytes."""
    return bool(rule) and hashlib.sha256(raw).hexdigest() == rule["sha256"]


def c01_literal_inventory(repo, name, revision, security, matches):
    """Exact authenticated source literals, never arbitrary path exemptions.

    The three implementation fragments and the synthetic rejection fixture
    are unchanged bytes from the finite C01 source snapshot. Registry entries
    are the same two collected adversarial node IDs, in the two collection
    lists only. A path elsewhere in the registry or any added token fails.
    """
    if not revision or not security or name not in revision["files"]:
        return False
    source_files = {
        "qbot/research/data/csmar/binding.py",
        "qbot/research/data/csmar/fingerprint.py",
        "qbot/research/data/csmar/native.py",
        "tests/research/test_csmar_source_binding.py",
    }
    if name in source_files:
        old = security.source_content(repo, name).decode("utf-8-sig")
        values = [n.value for n in ast.walk(ast.parse(old))
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        original = Counter(m.group() for value in values
                           for m in ABS.finditer(URL.sub("REMOTE_URL", value))
                           if m.group() != chr(47))
        return Counter(token for _, _, token in matches) == original
    if name in {
        getattr(security, "REGISTRY", None),
        "docs/research/evidence/c01-solo-v1/version-registry.json",
    }:
        prefix = ("tests/research/test_csmar_source_binding.py::"
                  "test_reject_malicious_zip_paths[")
        values = {prefix + part + "evil]" for part in (chr(47), "C:" + chr(47))}
        contexts = {"|execution_delta|added|" + key
                    for key in ("all_ids", "selected_ids")}
        return (len(matches) == 4 and
                Counter((context, value) for context, value, _ in matches) ==
                Counter((context, value) for context in contexts for value in values))
    return False


def review(repo, base, scope_yhh30m=False):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True, encoding="utf-8").splitlines()
    def filenames(*args):
        raw = subprocess.check_output(["git", *args, "-z"], cwd=repo)
        return raw.decode("utf-8").rstrip("\x00").split("\x00") if raw else []
    all_files = sorted(set(filenames("diff", "--name-only", base)) |
                       set(filenames("ls-files", "--others", "--exclude-standard")))
    files = [name for name in all_files if not scope_yhh30m or minute_release_path(name)]
    blocked, fixture_vectors = [], Counter()
    path_exceptions, encoding_exceptions = Counter(), []
    rules = load_path_rules(repo, blocked)
    revision, security = None, None
    revision_checker = repo / ".github/scripts/ci/security_admission_revision.py"
    if revision_checker.exists():
        spec = importlib.util.spec_from_file_location("independent_control", repo / ".github/scripts/ci/research_checks.py")
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        security = controller.load_security_revision(repo)
        revision = security.load_revision(repo)
    prefixes = [str(Path.home()).replace(chr(92), chr(47)).lower(),
                str(repo.parent).replace(chr(92), chr(47)).lower()]
    sizes = {}
    literal_cache = {}
    for name in all_files:
        if not canonical_path(name):
            blocked.append({"file": name, "reason": "NON_CANONICAL_PUBLICATION_PATH"})
            continue
        path = repo / name
        if not path.exists():
            continue
        sizes[name] = path.stat().st_size
        if path.suffix.lower() in {".parquet", ".zip", ".xlsx", ".bin", ".pkl", ".pyc"} or sizes[name] > 2_000_000:
            blocked.append({"file": name, "reason": "RAW_OR_LARGE_ARTIFACT"})
            continue
        raw = path.read_bytes()
        rule = rules.get(name)
        candidate = matches_frozen_file(raw, rule)
        legacy_decoded = False
        if b"\x00" in raw or raw.startswith((b"\x89PNG\r\n\x1a\n", b"%PDF-", b"GIF8", b"\xff\xd8\xff")):
            blocked.append({"file": name, "reason": "BINARY_ARTIFACT_REQUIRES_REVIEW"})
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            if not candidate or name != LEGACY_LOG or rule["encoding"] != "cp936":
                blocked.append({"file": name, "reason": "NON_UTF8_CONTENT"})
                continue
            attrs = subprocess.check_output(["git", "check-attr", "-z", "text", "--", name], cwd=repo)
            if attrs != (name + "\x00text\x00unset\x00").encode("utf-8"):
                blocked.append({"file": name, "reason": "LEGACY_TEXT_ATTRIBUTES_MISMATCH"})
                continue
            try:
                text = raw.decode("cp936")
                if text.encode("cp936") != raw:
                    raise UnicodeError("legacy text roundtrip")
            except UnicodeError:
                blocked.append({"file": name, "reason": "NON_UTF8_CONTENT"})
                continue
            legacy_decoded = True
        secret_found = bool(SECRET.search(text))
        if secret_found:
            blocked.append({"file": name, "reason": "SECRET_SHAPED_VALUE"})
        if path.suffix == ".csv":
            header = next(csv.reader(io.StringIO(text)))
            if any(c in header for c in ("open", "close", "volume_vendor", "开盘价（元）", "分钟时间")):
                blocked.append({"file": name, "reason": "RAW_MARKET_CSV_COLUMNS"})
        if path.suffix == ".json":
            entries = list(strings(json.loads(text)))
        elif path.suffix == ".py":
            tree = ast.parse(text)
            suffixes = set()
            # A single filename appended to the explicit relative evidence/
            # prefix and a Path.name is a relative fragment, not a local root.
            for node in ast.walk(tree):
                if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and
                        isinstance(node.right, ast.Constant) and isinstance(node.right.value, str) and
                        re.fullmatch(chr(47) + r"[A-Za-z0-9_.-]+", node.right.value) and
                        isinstance(node.left, ast.BinOp) and isinstance(node.left.op, ast.Add) and
                        isinstance(node.left.left, ast.Constant) and node.left.left.value == "evidence" + chr(47) and
                        isinstance(node.left.right, ast.Attribute) and node.left.right.attr == "name"):
                    suffixes.add(id(node.right))
            entries = [("PYTHON_STRING", n.value) for n in ast.walk(tree)
                       if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in suffixes]
        else:
            entries = [("TEXT", text)]
        matches = []
        for context, value in entries:
            if not secret_found and SECRET.search(value):
                blocked.append({"file": name, "reason": "SECRET_SHAPED_VALUE"})
                secret_found = True
            value = URL.sub("REMOTE_URL", value)
            for match in ABS.finditer(value):
                token = match.group()
                if token == chr(47):
                    continue
                matches.append((context, value, token))
        observed = dict(Counter(path_kind(token) for _, _, token in matches))
        if matches and c01_literal_inventory(repo, name, revision, security, matches):
            path_exceptions[name] = len(matches)
            continue
        # These two security postimages retain existing anti-path test vectors
        # and a workflow URL fragment. Authenticate the entire current file,
        # then require exactly the old fixed-baseline path token inventory.
        # Raw artifacts and secrets have already been checked above.
        if (revision and name in revision["files"] and name in {
                "tests/evidence/test_pit_archive_verifiers.py",
                ".github/workflows/pit-archive-evidence.yml"}):
            old = subprocess.check_output(["git", "show", security.BASE + ":" + name], cwd=repo).decode("utf-8-sig")
            values = [n.value for n in ast.walk(ast.parse(old))
                      if isinstance(n, ast.Constant) and isinstance(n.value, str)] if name.endswith(".py") else [old]
            original_tokens = Counter(m.group() for value in values for m in ABS.finditer(URL.sub("REMOTE_URL", value))
                                      if m.group() != chr(47))
            if Counter(token for _, _, token in matches) == original_tokens:
                path_exceptions[name] = len(matches)
                continue
        audited_path = candidate and observed == rule["path_match_counts"]
        if candidate and not audited_path:
            blocked.append({"file": name, "reason": "PATH_EXCEPTION_COUNT_MISMATCH"})
        if legacy_decoded and audited_path:
            encoding_exceptions.append(name)
        if audited_path:
            path_exceptions[name] = len(matches)
            continue
        for context, value, token in matches:
            normalized = token.replace(chr(92), chr(47)).lower()
            if any(normalized.startswith(prefix) for prefix in prefixes):
                blocked.append({"file": name, "reason": "MACHINE_PATH"})
                continue
            historical_case = (name.endswith("yhh30m-conversion-v1/verification.json") and
                               context.endswith("|KEY") and "resolved_case_statuses" in context and
                               value.startswith("tests.research."))
            if historical_case:
                if token not in literal_cache:
                    result = subprocess.run(["git", "grep", "-F", "-e", token, base, "--", "tests", "qbot", "examples"],
                                            cwd=repo, capture_output=True, text=True)
                    literal_cache[token] = result.returncode == 0
                if literal_cache[token]:
                    fixture_vectors[name] += 1
                    continue
            blocked.append({"file": name, "reason": "UNEXPLAINED_ABSOLUTE_PATH", "context": context.split("|")[0]})
    # Protected gates remain global even for a module-scoped publication scan.
    gate_changes = [name for name in all_files if any(name == gate or name.startswith(gate) for gate in GATES)]
    authorized = []
    if revision:
        authorized = sorted(set(gate_changes) & set(revision["files"]))
    return {"schema": "YHH_RELEASE_DIFF_REVIEW_V1", "base_commit": git("rev-parse", base)[0],
            "head_commit": git("rev-parse", "HEAD")[0], "changed_files": files, "file_bytes": sizes,
            "total_bytes": sum(sizes.values()), "blocked_findings": blocked, "protected_gate_changes": gate_changes,
            "authorized_security_gate_changes": authorized,
            "publication_scope": "YHH30M" if scope_yhh30m else "FULL_DIFF",
            "out_of_scope_files": [name for name in all_files if name not in files],
            "safety_scope": "FULL_DIFF", "safety_checked_files": sorted(sizes),
            "audited_path_exceptions": dict(path_exceptions),
            "audited_encoding_exceptions": encoding_exceptions,
            "exception_baseline_commit": PATH_AUDIT_BASELINE,
            "exception_rules_sha256": PATH_RULES_SHA256,
            "exception_authentication": "PINNED_MANIFEST_SHA256",
            "historical_test_vectors_defined_in_main": dict(fixture_vectors),
            "status": "PASS" if not blocked and not (set(gate_changes) - set(authorized)) else "FAIL"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--scope-yhh30m", action="store_true",
                        help="Report minute ownership; scan safety and protected gates globally")
    args = parser.parse_args()
    result = review(args.repo.resolve(), args.base, scope_yhh30m=args.scope_yhh30m)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
