"""Read-only local discovery. Catalog hashes describe observations, not admission.

Workbook parsing, source integrity and identity rules belong to native/binding.
Only reviewed profiles receive coverage claims; other families stay UNKNOWN.
"""
from pathlib import Path
import re

from factorlab.data.evidence import fail, json_bytes, sha256
from factorlab.data.tdx import safe_path
from . import native, profiles
from .binding import reference, scope


def operator_paths(source_root, scratch, output):
    """Reject scratch/output writes anywhere inside the original-data root."""
    root, temporary, destination = [safe_path(Path(p)) for p in (source_root, scratch, output)]
    if not root.is_dir() or not temporary.is_dir():
        fail("existing source and scratch directories required")
    if any(p == root or root in p.parents for p in (temporary, destination)):
        fail("scratch and candidate output must be outside original source root")
    return root, temporary, destination


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return native.digest_stream(stream)


def inspect_workbook(archive, member, digest, family, scratch):
    """Scan all physical rows, bounded memory; never trust XLSX dimensions."""
    _, code_field, date_field = profiles.PROFILES[family]
    result = {"sheet_records": {}, "schemas": {}, "records": 0,
              "date_min": None, "date_max": None, "codes": [], "invalid_dates": 0,
              "duplicate_conflict_status": "NOT_CHECKED_GLOBAL; replay checks selected scope"}
    codes = set()
    with native.workbook(archive, member, digest, scratch) as book:
        strings = native.shared_strings(book)
        for sheet, xml in native.sheets(book).items():
            iterator = iter(native.rows(book, xml, strings))
            metadata = [next(iterator, None) for _ in range(3)]
            if any(r is None or r[0] != i + 1 for i, r in enumerate(metadata)):
                fail("three native metadata rows required")
            headers, names, units = [r[1] for r in metadata]
            if len(set(headers)) != len(headers) or not {code_field, date_field}.issubset(headers):
                fail("missing/duplicate catalog fields")
            result["schemas"][sheet] = dict(headers=headers, names=names, units=units)
            ci, di = headers.index(code_field), headers.index(date_field)
            count = 0
            for row, values in iterator:
                count += 1
                values += [None] * max(0, len(headers) - len(values))
                codes.add(values[ci])
                day = values[di]
                # Dates remain raw lexemes. Invalid source dates are counted,
                # never repaired; binding performs strict selected-date checks.
                if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    result["date_min"] = min(result["date_min"] or day, day)
                    result["date_max"] = max(result["date_max"] or day, day)
                else:
                    result["invalid_dates"] += 1
            result["sheet_records"][sheet] = count
            result["records"] += count
    result["codes"] = sorted(c for c in codes if isinstance(c, str))
    result["security_count"] = len(codes)
    result["coverage_status"] = "OBSERVED_NATIVE_DATE_STRINGS"
    return result


def catalog_source(path, *, scratch, scan_families=()):
    path = safe_path(Path(path))
    digest = file_sha256(path)
    result = dict(path=str(path), name=path.name, sha256=digest, bytes=path.stat().st_size,
                  supplier_vintage="UNKNOWN", members=[])
    with native.source_zip(path.parent, path.name, digest, result["bytes"]) as (archive, _):
        for item in archive.infolist():
            if item.is_dir():
                continue
            with archive.open(item) as stream:
                import hashlib
                h = hashlib.sha256()
                for block in iter(lambda: stream.read(4 * 1024 ** 2), b""):
                    h.update(block)
            entry = dict(name=item.filename, bytes=item.file_size, sha256=h.hexdigest())
            match = re.fullmatch(r"(.+?)[0-9]*\.xlsx", item.filename)
            family = match[1] if match else None
            # Prefer the exact reviewed prefix (some family names end in digits).
            family = next((f for f in profiles.PROFILES
                           if re.fullmatch(re.escape(f) + r"[0-9]*\.xlsx", item.filename)), family)
            entry.update(family=family, profile_available=family in profiles.PROFILES,
                         coverage_status="UNKNOWN", records=None, security_count=None,
                         date_min=None, date_max=None)
            if family in profiles.PROFILES:
                des = family + "[DES][xlsx].txt"
                if archive.getinfo(des).file_size > 1024 ** 2:
                    fail("dictionary byte limit")
                entry["dictionary_sha256"] = sha256(archive.read(des))
                entry["dictionary_reviewed"] = entry["dictionary_sha256"] == profiles.PROFILES[family][0]
                entry["code_field"], entry["date_field"] = profiles.PROFILES[family][1:]
                if family in scan_families:
                    if not entry["dictionary_reviewed"]:
                        fail("unreviewed dictionary; cannot scan as reviewed profile")
                    entry.update(inspect_workbook(archive, item.filename, entry["sha256"], family, scratch))
            result["members"].append(entry)
    return result


def references(catalog, *, family, codes, start, end):
    """Explicit source order; overlap fails later, never select a silent winner."""
    scope(codes, start, end)
    refs = []
    for source in catalog["sources"]:
        for entry in source["members"]:
            if entry.get("family") != family:
                continue
            if not entry.get("sheet_records"):
                fail("family coverage not scanned: " + source["name"])
            if not set(codes).intersection(entry["codes"]):
                continue
            if entry["date_min"] is None or entry["date_max"] < start or entry["date_min"] > end:
                continue
            refs.append(reference(source_name=source["name"], source_sha256=source["sha256"],
                                  source_bytes=source["bytes"], family=family,
                                  member=entry["name"], member_sha256=entry["sha256"],
                                  sheet_records=entry["sheet_records"], codes=codes, start=start, end=end))
    return refs


def write_json(path, value):
    with Path(path).open("xb") as handle:
        handle.write(json_bytes(value))
