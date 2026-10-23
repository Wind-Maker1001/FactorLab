"""FactorLab V1 fixed verifier. Run a separately retained, reviewed copy.

This module never imports or executes candidate code. V1 has no approval issuer
or approval importer: even exact integrity is BLOCKED for formal admission.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

PROTOCOL = "FACTORLAB_INDEPENDENT_SECURITY_V1"
ANCHOR = "docs/security/v1/trust-anchor.json"
DIRECTORIES = ("src/factorlab", "tests", "configs", "requirements", "tools/security", "tools/ci",
               "tools/migration", "examples/research", ".github/scripts/ci", "docs/user")
TOP_FILES = ("pyproject.toml", "MANIFEST.in", ".gitattributes", "SECURITY.md", "LICENSE",
             ".github/workflows/factorlab.yml", "README.md", "CONTRIBUTING.md", ".gitignore")


class SecurityViolation(ValueError):
    pass


def canonical(name):
    if (not isinstance(name, str) or not name or PurePosixPath(name).as_posix() != name
            or name.startswith("/") or any(c in name for c in '\\:<>"|?*')
            or any(ord(c) < 32 for c in name)
            or any(p in ("", ".", "..") or p != p.rstrip(" .")
                   or re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", p)
                   for p in name.split("/"))):
        raise SecurityViolation("NONCANONICAL_PATH")
    return name


def read_bound(root, name):
    path = root / canonical(name)
    for part in (path, *path.parents):
        if part == root:
            break
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SecurityViolation("LINK_OR_REPARSE_PATH: " + name)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SecurityViolation("NOT_SINGLE_LINK_REGULAR_FILE: " + name)
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        raw = handle.read()
        after = os.fstat(handle.fileno())
    end = path.stat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if not identity(before) == identity(opened) == identity(after) == identity(end):
        raise SecurityViolation("CHANGED_DURING_READ: " + name)
    return raw


def inventory(root):
    names = set(TOP_FILES)
    for directory in DIRECTORIES:
        for base, dirs, files in os.walk(root / directory, followlinks=False):
            dirs[:] = [d for d in dirs if d != "__pycache__" and not d.endswith(".egg-info")]
            for d in dirs:
                p = Path(base) / d
                info = p.lstat()
                if p.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise SecurityViolation("LINK_OR_REPARSE_DIRECTORY")
            for name in files:
                if not name.endswith((".pyc", ".pyo")):
                    names.add((Path(base) / name).relative_to(root).as_posix())
    return {n: hashlib.sha256(read_bound(root, n)).hexdigest() for n in sorted(names)}


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SecurityViolation("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def validate(candidate, trusted_anchor, anchor_sha256):
    """The caller supplies an out-of-band pin, never one read from candidate."""
    root = Path(candidate).absolute()
    for part in (root, *root.parents):
        if part.is_symlink() or getattr(part.lstat(), "st_file_attributes", 0) & 0x400:
            raise SecurityViolation("CANDIDATE_ROOT_LINK")
    if not isinstance(anchor_sha256, str) or not re.fullmatch("[0-9a-f]{64}", anchor_sha256):
        raise SecurityViolation("EXTERNAL_PIN_REQUIRED")
    raw = Path(trusted_anchor).read_bytes()
    if hashlib.sha256(raw).hexdigest() != anchor_sha256:
        raise SecurityViolation("EXTERNAL_ANCHOR_PIN_MISMATCH")
    if read_bound(root, ANCHOR) != raw:
        raise SecurityViolation("CANDIDATE_ANCHOR_CHANGED")
    anchor = strict_json(raw)
    if (set(anchor) != {"schema", "version", "project", "baseline", "approval", "files"}
            or anchor["schema"] != PROTOCOL or anchor["version"] != 1
            or anchor["project"] != "FactorLab" or anchor["approval"] is not None
            or anchor["baseline"] != "98b133841a5cd5ecffcf715e8626b41db2c5b8b6"
            or not isinstance(anchor["files"], dict)):
        raise SecurityViolation("UNSUPPORTED_TRUST_CONTRACT")
    for name, digest in anchor["files"].items():
        canonical(name)
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            raise SecurityViolation("INVALID_SOURCE_DIGEST")
    observed = inventory(root)
    if observed.keys() != anchor["files"].keys():
        raise SecurityViolation("SOURCE_SCOPE_CHANGED")
    changed = [n for n in observed if observed[n] != anchor["files"][n]]
    if changed:
        raise SecurityViolation("SOURCE_IDENTITY_CHANGED: " + ", ".join(changed))
    # Reobserve anchor after reading the complete closure. No candidate imports.
    if read_bound(root, ANCHOR) != raw:
        raise SecurityViolation("ANCHOR_CHANGED_DURING_CHECK")
    if inventory(root) != observed:
        raise SecurityViolation("SOURCE_CHANGED_DURING_CHECK")
    return {"schema": PROTOCOL, "integrity": "PASS", "files_checked": len(observed),
            "anchor_sha256": anchor_sha256, "independent_review": "BLOCKED",
            "authorization": "BLOCKED", "formal_admission": False,
            "execution_admission": False, "no_live_money": True,
            "pit": "PIT_UNVERIFIED", "financial_status": "FORMAL_ADMISSION_BLOCKED",
            "reason": "V1_HAS_NO_INDEPENDENT_APPROVAL_MECHANISM"}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--trusted-anchor", type=Path, required=True)
    p.add_argument("--anchor-sha256", required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    try:
        result = validate(a.candidate, a.trusted_anchor, a.anchor_sha256)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result = {"integrity": "FAIL", "authorization": "BLOCKED", "error": str(exc),
                  "formal_admission": False, "no_live_money": True}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    # Exact bytes do not constitute release authorization.
    return 2 if result["integrity"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
