"""Reuse the prior audit; bind streaming reads to its original byte hashes."""

from contextlib import contextmanager
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

from qbot.research.forward.integrity import assert_no_link_components
from .contract import IntradayDataError, json_bytes


def file_sha(path):
    digest = hashlib.sha256()
    with assert_no_link_components(path).open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def signature(path):
    s = assert_no_link_components(path).stat()
    return [s.st_size, s.st_mtime_ns, s.st_dev, s.st_ino]


class HashReader(io.RawIOBase):
    def __init__(self, raw):
        super().__init__()
        self.raw = raw
        self.digest = hashlib.sha256()

    def readable(self):
        return True

    def read(self, size=-1):
        data = self.raw.read(size)
        self.digest.update(data)
        return data

    def readinto(self, target):
        data = self.read(len(target))
        target[:len(data)] = data
        return len(data)


@contextmanager
def open_source(root, item):
    path = assert_no_link_components(root / item["container"])
    before = signature(path)
    with path.open("rb") as handle:
        if item["container"].endswith(".zip"):
            with zipfile.ZipFile(handle) as archive:
                with archive.open(item["locator"]) as member:
                    yield HashReader(member)
        else:
            yield HashReader(handle)
    if signature(path) != before:
        raise IntradayDataError("SOURCE_CHANGED_DURING_READ: " + path.name)


def load_inventory(source, audit):
    """Metadata-only inventory; no second price-content audit or ZIP extraction."""
    source, audit = assert_no_link_components(source), assert_no_link_components(audit)
    evidence = {}
    final = json.loads((audit / "final-verification.json").read_text(encoding="utf-8"))
    evidence["final-verification.json"] = file_sha(audit / "final-verification.json")
    for name in ("summary.json", "daily-counts.csv", "input-hashes.json"):
        path = assert_no_link_components(audit / name)
        digest = file_sha(path)
        expected = [v for k, v in final["output_sha256"].items() if k.replace("\\", "/").endswith("/" + name)]
        if expected != [digest]:
            # The repository explicitly retains old original-byte receipts.
            # Accept ONLY a recorded exact original->public byte mapping, never
            # a guessed path replacement or silently recomputed old receipt.
            publication = audit.parent / "pit-publication-20261018-v1" / "manifest.json"
            if not publication.is_file():
                raise IntradayDataError("AUDIT_EVIDENCE_HASH_MISMATCH: " + name)
            published = json.loads(publication.read_text(encoding="utf-8"))
            matches = [entry for entry in published["files"]
                       if entry["path"].endswith("/" + audit.name + "/" + name)]
            if (len(matches) != 1 or expected != [matches[0]["original_sha256"]] or
                    matches[0]["public_sha256"] != digest or matches[0]["public_bytes"] != path.stat().st_size):
                raise IntradayDataError("PUBLIC_AUDIT_BINDING_MISMATCH: " + name)
            evidence["publication_manifest_sha256"] = file_sha(publication)
            evidence[name + ":original_sha256"] = expected[0]
        evidence[name] = digest
    summary = json.loads((audit / "summary.json").read_text(encoding="utf-8"))
    hashes = json.loads((audit / "input-hashes.json").read_text(encoding="utf-8"))
    with (audit / "daily-counts.csv").open(encoding="utf-8-sig", newline="") as handle:
        days = list(csv.DictReader(handle))
    if len(days) != summary["csv_days"] or sum(int(d["rows"]) for d in days) != summary["total_rows"]:
        raise IntradayDataError("AUDIT_COUNTS_DO_NOT_RECONCILE")
    containers = {k.replace("\\", "/").rsplit("/", 1)[-1]: v for k, v in hashes["containers"].items()}
    actual = {p.name for p in source.iterdir() if p.is_file() and p.suffix.lower() in {".csv", ".zip"}}
    if actual != set(containers):
        raise IntradayDataError("SOURCE_INVENTORY_CHANGED")
    indexed = {(d["archive"], d["member"]): d for d in hashes["archive_members"]}
    observed = set()
    for day in days:
        if day["container"] == "loose_2026_csv":
            day["container"] = day["locator"]
    for name in containers:
        if Path(name).name != name:
            raise IntradayDataError("UNSAFE_CONTAINER")
        path = assert_no_link_components(source / name)
        if name.endswith(".zip"):
            with zipfile.ZipFile(path) as archive:
                members = [i.filename for i in archive.infolist() if not i.is_dir()]
                if len(set(members)) != len(members) or set(members) != {m for a, m in indexed if a == name}:
                    raise IntradayDataError("ZIP_MEMBER_INVENTORY_CHANGED: " + name)
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    old = indexed[name, info.filename]
                    if info.file_size != old["bytes"] or "%08x" % info.CRC != old["crc32"]:
                        raise IntradayDataError("ZIP_MEMBER_METADATA_CHANGED")
    for day in days:
        key = (day["container"], day["locator"])
        if key in observed or day["container"] not in containers:
            raise IntradayDataError("DUPLICATE_OR_UNKNOWN_SOURCE")
        observed.add(key)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day["date"]):
            raise IntradayDataError("INVALID_AUDIT_DATE")
        if day["container"].endswith(".zip"):
            if indexed[key]["sha256"] != day["content_sha256"]:
                raise IntradayDataError("AUDIT_MEMBER_BINDING_MISMATCH")
        elif day["locator"] != day["container"] or containers[day["container"]] != day["content_sha256"]:
            raise IntradayDataError("AUDIT_LOOSE_BINDING_MISMATCH")
    if len({d["date"] for d in days}) != len(days):
        raise IntradayDataError("DUPLICATE_AUDIT_DATES")
    return sorted(days, key=lambda d: d["date"]), containers, evidence


def verify_containers(source, containers):
    result = {}
    for name, expected in sorted(containers.items()):
        path = source / name
        before = signature(path)
        if file_sha(path) != expected or signature(path) != before:
            raise IntradayDataError("SOURCE_CONTAINER_HASH_MISMATCH: " + name)
        result[name] = {"sha256": expected, "stat": before}
    return result
