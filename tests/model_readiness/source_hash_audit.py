"""Independent v1 forensic reproduction; does not import the production hasher.

Run on the original or corrected tree with --revision and three checkout roots.
The EOL map can reconstruct the original mixed-EOL Windows bytes from Git blobs.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def legacy_digest(files):
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode("utf-8"))
        digest.update(content)
    return digest.hexdigest()


def normalized(content):
    return content.replace(b"\r\n", b"\n")


def eol_map(content):
    # Zero-based LF ordinal, including files mixing LF and CRLF.
    return [number for number, end in enumerate(
        index for index, byte in enumerate(content) if byte == 10
    ) if end and content[end - 1] == 13]


def reconstruct(lf_content, crlf_ordinals):
    selected = set(crlf_ordinals)
    parts = lf_content.split(b"\n")
    result = bytearray(parts[0])
    for number, part in enumerate(parts[1:]):
        result.extend(b"\r\n" if number in selected else b"\n")
        result.extend(part)
    return bytes(result)


def audit(repository, revision, worktrees, output):
    revision = git(repository, "rev-parse", revision + "^{commit}").decode().strip()
    names = sorted(name.decode("utf-8") for name in git(
        repository, "ls-tree", "-rz", "--name-only", revision, "--", "qbot/research"
    ).split(b"\0") if name.endswith(b".py"))
    objects = {name: git(repository, "show", revision + ":" + name) for name in names}
    views = {"git_objects": objects}
    extras, status = {}, {}
    for label, root in worktrees.items():
        actual = {path.relative_to(root).as_posix(): path.read_bytes()
                  for path in (root / "qbot/research").rglob("*.py")}
        extras[label] = {"extra": sorted(set(actual) - set(names)),
                         "missing": sorted(set(names) - set(actual))}
        status[label] = git(root, "status", "--porcelain=v1", "--", "qbot/research").decode()
        views[label] = actual
    records, maps = [], {}
    for name in names:
        row = {"path": name}
        for label, files in views.items():
            data = files[name]
            row[label] = {
                "raw_sha256": hashlib.sha256(data).hexdigest(),
                "lf_sha256": hashlib.sha256(normalized(data)).hexdigest(),
                "bytes": len(data), "crlf": data.count(b"\r\n"),
                "lf_only": data.count(b"\n") - data.count(b"\r\n"),
                "lone_cr": data.count(b"\r") - data.count(b"\r\n"),
                "lf_equal_git": normalized(data) == normalized(objects[name]),
            }
        original = views["windows_worktree"][name]
        maps[name] = eol_map(original)
        assert reconstruct(normalized(objects[name]), maps[name]) == original, name
        records.append(row)
    replay = {name: reconstruct(normalized(data), maps[name]) for name, data in objects.items()}
    payload = {
        "schema_version": 1, "revision": revision, "source_count": len(names),
        "v1_algorithm": "SHA256(concat(sorted UTF-8 POSIX path + raw file bytes)); no delimiters",
        "views": {label: {"raw_legacy_sha256": legacy_digest(files),
                           "lf_legacy_sha256": legacy_digest({n: normalized(b) for n, b in files.items()}),
                           "source_count": len(files)} for label, files in views.items()},
        "extra_missing": extras, "source_git_status": status,
        "files": records, "windows_crlf_lf_ordinals": maps,
        "replayed_windows_legacy_sha256": legacy_digest(replay),
        "limits": "Retrospective EOL equivalence; v1 did not capture an end-of-run snapshot. Cannot prove absence of transient code changes during the old runs.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"revision": revision, "count": len(names), "views": payload["views"],
                      "raw_different_files": {label: sum(objects[n] != files[n] for n in names)
                                              for label, files in views.items()},
                      "non_eol_differences": sum(not row["windows_worktree"]["lf_equal_git"] for row in records),
                      "replayed": payload["replayed_windows_legacy_sha256"]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--windows-worktree", type=Path, required=True)
    parser.add_argument("--clean-windows", type=Path, required=True)
    parser.add_argument("--clean-lf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit(args.repository, args.revision, {
        "windows_worktree": args.windows_worktree.resolve(),
        "clean_windows": args.clean_windows.resolve(), "clean_lf": args.clean_lf.resolve(),
    }, args.output)
