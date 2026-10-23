"""Read-only real-asset audit. No acquisition, factors, HFQ generation or admission.

Run from a clean checkout with an explicit private source checkout and a NEW
ignored output directory. Only allowlisted aggregate files may be published.
The companion notebook documents the same entry point; private rows stay local.
"""

import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import time

import numpy as np
import pandas as pd

from examples.research.five_year_supplier_probe import response_rows
from factorlab.data.tdx import audit_day, discover, raw_frame, read_snapshot, safe_path
from factorlab.data.tdx_staging import disjoint, frame_source_sha256, json_bytes, write_new
from factorlab.exploratory.five_year import DEFAULT_SYMBOLS, verify_pilot


START, RESEARCH_START, END = "2021-03-08", "2021-09-13", "2026-09-11"
FIELDS = ("open", "high", "low", "close")
DECODER_COMMIT = "2857fdad08534533610bd2aeca387f760c4baa42"
DECODER_SHA = "e28fae76760b1247a960cab285aa357c2d263b3212e7cabfb3f1ebfcc127d6ba"
PUBLIC_FILES = ("summary.json", "coverage-monthly.csv", "anomalies.csv",
                "frozen-comparison.csv", "gbbq-summary.json", "coverage.svg")
GBBQ_DTYPE = np.dtype([("market", "u1"), ("code", "S7"), ("date", "<u4"),
                       ("category", "u1"), ("c1", "<f4"), ("c2", "<f4"),
                       ("c3", "<f4"), ("c4", "<f4")])


def digest(content):
    return hashlib.sha256(content).hexdigest()


def tracked_digest(path):
    return digest(os.readlink(path).encode("utf-8") if path.is_symlink() else path.read_bytes())


def read_json(path):
    return json.loads(Path(path).read_bytes())


def save_json(path, value):
    write_new(path, json_bytes(value))


def candidate_gbbq(content, decoder_source):
    """Vectorized candidate deciphering, with a pinned external key-table source.

    Only literal key bytes are read from the AST. External Python is NOT executed.
    This follows the pinned pytdx reader's block layout, not a supplier contract.
    No category is mapped to an authenticated action or share-capital definition.
    """
    if digest(decoder_source) != DECODER_SHA:
        raise ValueError("unpinned decoder reference")
    if len(content) < 4:
        raise ValueError("missing GBBQ count")
    count = struct.unpack_from("<I", content)[0]
    if len(content) != 4 + count * 29:
        raise ValueError("GBBQ count/length mismatch")
    tree = ast.parse(decoder_source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "GbbqReader")
    assignment = next(node for node in cls.body if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "hexdump_keys" for t in node.targets))
    keys = np.frombuffer(bytes.fromhex(ast.literal_eval(assignment.value)), dtype="<u4")
    if len(keys) != 1044:
        raise ValueError("unexpected expanded key table")
    encrypted = np.frombuffer(content, dtype="u1", offset=4).reshape(count, 29)
    blocks = encrypted[:, :24].copy().reshape(-1, 8).view("<u4").reshape(-1, 2)
    left, right = blocks[:, 0] ^ keys[17], blocks[:, 1].copy()
    for position in range(16, 0, -1):
        value = keys[274 + ((left >> 16) & 255)] + keys[18 + (left >> 24)]
        value ^= keys[530 + ((left >> 8) & 255)]
        value += keys[786 + (left & 255)]
        left, right = right ^ value ^ keys[position], left
    decoded = np.column_stack((right ^ keys[0], left)).astype("<u4").view("u1").reshape(count, 24)
    clear = np.column_stack((decoded, encrypted[:, 24:])).astype("u1").tobytes()
    return np.frombuffer(clear, dtype=GBBQ_DTYPE).copy()


def exact_ohlc(integers, reference, divisor=100):
    """Existing fixed candidate scale only; no fitted scale or tolerance."""
    if divisor != 100:
        raise ValueError("frozen stock comparison uses the existing fixed candidate divisor 100")
    if len(integers) != 4 or len(reference) != 4:
        raise ValueError("four OHLC values required")
    pairs = [(Decimal(int(a)) / Decimal(divisor), Decimal(str(b)))
             for a, b in zip(integers, reference)]
    if len(pairs) != 4 or any(not b.is_finite() or b <= 0 for _, b in pairs):
        raise ValueError("four finite positive reference prices required")
    return "EXACT_MATCH" if all(a == b for a, b in pairs) else "DIFFERENT"


def compare_status(has_tdx, has_reference, basis, units_verified, numerical):
    if basis != "RAW":
        return "SOURCE_SEMANTICS_NOT_COMPARABLE"
    if not has_tdx or not has_reference:
        return "NO_VALID_REFERENCE"
    if not units_verified:
        return "SOURCE_UNIT_UNVERIFIED"
    return numerical


def supplier_frame(content, vendor, symbol, basis):
    # Reuse the existing strict identity, basis, dates and layout validator.
    response_rows(content, vendor, symbol, basis)
    text = content.decode("gbk" if vendor == "SOHU" else "utf-8")
    if vendor == "SOHU":
        match = re.fullmatch(r"\s*historySearchHandler\((.*)\);?\s*", text, re.DOTALL)
        payload = json.loads(match.group(1) if match else text)
        rows = [(r[0], r[1], r[6], r[5], r[2]) for r in payload[0]["hq"]] if payload else []
    else:
        code = symbol[-2:].lower() + symbol[:6]
        data = json.loads(text)["data"][code]
        rows = [(r[0], r[1], r[3], r[4], r[2]) for r in data.get("day" if basis == "RAW" else "hfqday", [])]
    frame = pd.DataFrame(rows, columns=["date", *FIELDS])
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.set_index("date").sort_index()


def stat_inventory(roots, exclude):
    """Before/after metadata for pre-existing private files, including receipts."""
    found = {}
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and exclude not in path.parents and path != exclude:
                s = path.stat()
                found[str(path)] = [s.st_size, s.st_mtime_ns]
    return found


def preservation_result(before, after):
    """A missing or modified pre-existing file fails preservation."""
    changed = sorted(name for name, value in before.items() if after.get(name) != value)
    return {"preexisting_files": len(before), "changed": changed,
            "status": "FAIL" if changed else "PASS",
            "check": "size_and_mtime; DAY/staging also SHA and full roundtrip"}


def audit_assets(private, output, pilot_manifest, decoder):
    begin = time.perf_counter()
    private, output = safe_path(private), safe_path(output)
    # Output belongs to the source checkout's ignored diagnostics tree only.
    allowed = safe_path(private / "results/diagnostics/tdx-data-quality-audit")
    if allowed not in output.parents:
        raise ValueError("private output must be inside the ignored audit diagnostics tree")
    old = private / "results/diagnostics/tdx-local-data/20261011-tdx-raw-conversion-v1"
    manifest = read_json(old / "manifest.json")
    source = safe_path(Path(manifest["source_root"]))
    staging = safe_path(Path(manifest["configuration"]["staging_root"]))
    for root in (source, staging, old, Path(pilot_manifest).parent):
        disjoint(root, output)
    output.mkdir(parents=True, exist_ok=False)
    before = stat_inventory([private / "data/research", private / "results/diagnostics"], output)
    save_json(output / "private-input-stats-before.json", before)
    git_root = Path(__file__).resolve().parents[2]
    tracked = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=git_root).decode("utf-8").split("\0")
    regular = [entry.split("\t", 1)[1] for entry in tracked if entry and not entry.startswith("160000 ")]
    frozen = {name: tracked_digest(git_root / name) for name in regular}
    print("pre-existing private metadata and tracked-file freeze complete", len(before), len(frozen), flush=True)
    save_json(output / "tracked-baseline.json", frozen)
    verification = verify_pilot(pilot_manifest, with_verification=True)
    pilot = verification["manifest"]
    pilot_root = Path(pilot_manifest).parent
    sample = read_json(pilot_root / "sample.json")
    if tuple(sample["symbols"]) != DEFAULT_SYMBOLS or sample["synthetic_only"] is not False:
        raise ValueError("PR #6 real frozen sample changed")
    sessions = pd.DatetimeIndex(pd.to_datetime(pd.read_csv(pilot_root / "calendar.csv")["date"]))
    sessions = sessions[(sessions >= START) & (sessions <= END)]
    research = sessions[sessions >= RESEARCH_START]
    for name, item in manifest["artifacts"].items():
        data = (old / name).read_bytes()
        if digest(data) != item["sha256"] or len(data) != item["bytes"]:
            raise ValueError("old conversion artifact mismatch: " + name)
    old_inventory = pd.read_csv(old / "inventory.csv").set_index("source_file")
    conversions = pd.read_csv(old / "conversion_status.csv").fillna("")
    converted = conversions[conversions.status.isin(["CONVERTED_RAW_ONLY", "REUSED_VERIFIED"])]
    by_source = converted.set_index("source_file").to_dict("index")
    entries, discovery_issues = discover(source)
    aggregates, anomalies, month_rows, private_rows, missing, frozen_frames = Counter(), Counter(), Counter(), [], [], {}
    integrity_errors, monthly_files = [], Counter()
    for index, entry in enumerate(entries):
        path, market = entry["path"], entry["source_market"]
        audit = audit_day(path, market, source, "2020-01-02", "2026-10-09", sessions,
                          RESEARCH_START, END, 130)
        info, security = audit.inventory, audit.security
        symbol, kind = info["symbol"], security["security_type"]
        aggregates["files"] += 1
        aggregates["records"] += info["record_count"]
        aggregates["audit_" + info["audit_status"]] += 1
        aggregates["market_" + market] += 1
        aggregates["type_" + kind] += 1
        if str(path) not in old_inventory.index or info["source_sha256"] != old_inventory.loc[str(path), "source_sha256"]:
            integrity_errors.append({"file": str(path), "error": "SOURCE_SET_OR_SHA_CHANGED"})
        for item in audit.anomalies:
            anomalies[(market, kind, item["code"], item["severity"])] += item["count"]
        dates = audit.dates
        observed = pd.DatetimeIndex([]) if dates is None else dates.dropna().unique()
        covered = sessions.intersection(observed)
        missed = sessions.difference(observed)
        complete = len(covered) == len(sessions)
        aggregates["full_history_files"] += int(complete)
        aggregates["full_history_" + kind] += int(complete)
        aggregates["observed_grid_" + kind] += len(covered)
        aggregates["missing_grid_" + kind] += len(missed)
        month_index = sessions.to_period("M")
        covered_month = covered.to_period("M")
        for month in month_index.unique():
            key = (market, kind, str(month))
            month_rows[key] += int((covered_month == month).sum())
            monthly_files[key] += 1
        # Every missing session and per-security range remains private.
        if len(missed):
            missing.extend({"symbol": symbol, "date": str(day.date()), "reason": "UNKNOWN"} for day in missed)
        row = {**info, **security, "history_observed": len(covered), "history_expected": len(sessions),
               "missing_reason": "UNKNOWN", "history_complete": complete,
               "min_date": None if not len(observed) else str(observed.min().date()),
               "max_date": None if not len(observed) else str(observed.max().date()),
               "duplicate_symbol_file": False}
        private_rows.append(row)
        if symbol in sample["symbols"] and audit.records is not None and audit.convertible:
            f = pd.DataFrame({"date": dates, **{field: audit.records[field] for field in FIELDS}})
            frozen_frames[symbol] = f.set_index("date")
        previous = by_source.get(str(path))
        if previous is not None:
            try:
                parquet = safe_path(Path(previous["parquet_file"]), staging)
                content, sha = read_snapshot(parquet, staging)
                receipt = read_json(parquet.parent / "receipt.json")
                if not (sha == previous["parquet_sha256"] == receipt["parquet_sha256"]
                        and receipt["source_sha256"] == info["source_sha256"]
                        and receipt["symbol"] == symbol and receipt["rows"] == info["record_count"]
                        and receipt["source_file"] == str(path) and receipt["parquet_bytes"] == len(content)
                        and receipt["contract"]["financial_research_eligible"] is False):
                    raise ValueError("receipt binding mismatch")
                frame = pd.read_parquet(io.BytesIO(content))
                pd.testing.assert_frame_equal(raw_frame(audit), frame, check_exact=True)
                if frame_source_sha256(frame) != info["source_sha256"]:
                    raise ValueError("DAY roundtrip mismatch")
                aggregates["parquet_verified"] += 1
                aggregates["parquet_rows_verified"] += len(frame)
            except (ValueError, AssertionError, OSError, KeyError) as exc:
                integrity_errors.append({"file": str(path), "error": str(exc)})
        elif audit.convertible:
            integrity_errors.append({"file": str(path), "error": "CONVERTIBLE_WITHOUT_OLD_OUTPUT"})
        if (index + 1) % 500 == 0:
            print("read-only DAY + exact Parquet replay", index + 1, "/", len(entries), flush=True)
    inventories = pd.DataFrame(private_rows)
    duplicate_symbols = inventories["symbol"].duplicated(keep=False)
    inventories["duplicate_symbol_file"] = duplicate_symbols
    inventories.to_csv(output / "security-inventory-private.csv", index=False)
    pd.DataFrame(missing).to_csv(output / "missing-sessions-private.csv", index=False)
    save_json(output / "integrity-errors-private.json", integrity_errors)
    old_sources = set(old_inventory.index)
    current_sources = {str(entry["path"]) for entry in entries}
    staging_count = len(list(staging.rglob("data.parquet")))
    receipt_count = len(list(staging.rglob("receipt.json")))
    coverage_rows = [{"market_directory_candidate": market, "security_type_candidate": kind,
                      "month": month, "files": monthly_files[(market, kind, month)],
                      "expected_grid": int((month_index == month).sum()) * monthly_files[(market, kind, month)],
                      "observed_grid": count,
                      "missing_reason": "UNKNOWN"}
                     for (market, kind, month), count in sorted(month_rows.items())]
    pd.DataFrame(coverage_rows).to_csv(output / "coverage-monthly.csv", index=False)
    pd.DataFrame([dict(market_directory_candidate=m, security_type_candidate=k,
                       code=c, severity=s, records=n) for (m, k, c, s), n in sorted(anomalies.items())]
                 ).to_csv(output / "anomalies.csv", index=False)
    comparisons = frozen_comparison(private, output, pilot_root, sample, sessions, frozen_frames)
    gbbq = audit_gbbq(source.parent / "T0002/hq_cache", decoder, output)
    after = stat_inventory([private / "data/research", private / "results/diagnostics"], output)
    preservation = preservation_result(before, after)
    changed = preservation["changed"]
    save_json(output / "private-preservation.json", preservation)
    baseline_changed = [name for name, sha in frozen.items() if tracked_digest(git_root / name) != sha]
    summary = {"schema": "TDX_DATA_QUALITY_AUDIT_V1", "no_live_money": True,
               "financial_admission": False, "hfq_status": "HFQ_UNVERIFIED", "pit_status": "PIT_UNVERIFIED",
               "created_at_utc": datetime.now(timezone.utc).isoformat(),
               "baseline_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_root, text=True).strip(),
               "network_requests_during_audit": 0, "historical_identity": "UNKNOWN",
               "market_identity": "DIRECTORY_CANDIDATE_NOT_AUTHENTICATED", "source_settings": "UNKNOWN",
               "tdx_adjustment_evidence": "UNVERIFIED", "volume_unit": "UNKNOWN", "amount_unit": "UNKNOWN",
               "calendar": {"sessions": len(sessions), "research_sessions": len(research),
                            "warmup_sessions": len(sessions) - len(research), "start": START, "end": END,
                            "native_replay": verification["verification"], "exchange_completeness": "UNVERIFIED"},
               "counts": dict(sorted(aggregates.items())), "duplicate_symbol_files": int(duplicate_symbols.sum()),
               "source_set_added": len(current_sources - old_sources), "source_set_missing": len(old_sources - current_sources),
               "integrity_errors": len(integrity_errors), "discovery_issues": len(discovery_issues),
               "old_artifacts_hash_verified": len(manifest["artifacts"]),
               "existing_parquet_files": staging_count, "existing_staging_receipts": receipt_count,
               "private_preexisting_files": len(before), "private_files_changed": len(changed),
               "private_preservation_status": preservation["status"],
               "tracked_baseline_files": len(frozen), "tracked_baseline_changed": baseline_changed,
               "source_manifest_sha256": digest((old / "manifest.json").read_bytes()),
               "pilot_manifest_sha256": digest(Path(pilot_manifest).read_bytes()),
               "frozen_sample_sha256": digest((pilot_root / "sample.json").read_bytes()),
               "frozen_symbols_unchanged": sample["symbols"] == list(DEFAULT_SYMBOLS),
               "comparison_rows": len(comparisons), "gbbq_status": gbbq["status"],
               "elapsed_seconds": time.perf_counter() - begin,
               "scope": "all local DAY and existing RAW staging; absence outside frozen sample remains UNKNOWN"}
    save_json(output / "summary.json", summary)
    coverage_svg(coverage_rows, output / "coverage.svg")
    files = {name: {"sha256": digest((output / name).read_bytes()), "bytes": (output / name).stat().st_size}
             for name in PUBLIC_FILES}
    save_json(output / "publication-receipt.json", {"schema": "TDX_AUDIT_PUBLICATION_V1", "files": files,
              "audit_script_sha256": digest(Path(__file__).read_bytes()), "financial_admission": False,
              "private_security_rows_published": False, "raw_prices_published": False})
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 1 if (integrity_errors or changed or baseline_changed or old_sources != current_sources
                 or duplicate_symbols.any() or discovery_issues
                 or staging_count != aggregates["parquet_verified"]
                 or receipt_count != aggregates["parquet_verified"]) else 0


def frozen_comparison(private, output, pilot_root, sample, sessions, tdx_frames):
    bars = pd.read_parquet(pilot_root / "bars.parquet")
    references = {}
    for symbol in sample["symbols"]:
        selected = bars[bars.symbol.eq(symbol)].set_index("date")
        references[("EASTMONEY", symbol, "RAW")] = selected[["raw_" + f for f in FIELDS]].rename(columns=lambda x: x[4:]).dropna()
        references[("EASTMONEY", symbol, "HFQ")] = selected[["adj_" + f for f in FIELDS]].rename(columns=lambda x: x[4:]).dropna()
    supplier = private / "data/research/five-year-supplier-audit/20261015-v1/20261015T054543662763Z-061823f6"
    receipt = read_json(supplier / "receipt.json")
    shapes = {}
    for attempt in receipt["attempts"]:
        if attempt.get("vendor") not in {"TENCENT", "SOHU"} or not attempt.get("native_file"):
            continue
        key = (attempt["vendor"], attempt["symbol"], attempt["basis"])
        content, sha = read_snapshot(supplier / attempt["native_file"], supplier)
        if sha != attempt["response_sha256"] or len(content) != attempt["response_bytes"]:
            raise ValueError("supplier bytes do not match archived receipt")
        try:
            references[key] = supplier_frame(content, *key)
        except ValueError:
            shapes[key] = "SOURCE_SEMANTICS_NOT_COMPARABLE"
    detailed, aggregated = [], []
    for symbol in sample["symbols"]:
        tdx = tdx_frames.get(symbol, pd.DataFrame(columns=FIELDS))
        for vendor, basis in (("EASTMONEY", "RAW"), ("TENCENT", "RAW"), ("SOHU", "RAW"), ("EASTMONEY", "HFQ")):
            key = (vendor, symbol, basis)
            ref = references.get(key, pd.DataFrame(columns=FIELDS))
            numerical, qualification = Counter(), Counter()
            for day in sessions:
                ht, hr = day in tdx.index, day in ref.index
                result = exact_ohlc(tdx.loc[day, list(FIELDS)], ref.loc[day, list(FIELDS)]) if ht and hr and basis == "RAW" else "NOT_COMPARED"
                status = shapes.get(key) or compare_status(ht, hr, basis, False, result)
                numerical[result] += 1
                qualification[status] += 1
                detailed.append({"symbol": symbol, "date": str(day.date()), "reference": vendor, "basis": basis,
                                 "tdx_available": ht, "reference_available": hr, "candidate_numerical_status": result,
                                 "status": status, "divisor": 100, "tolerance": 0})
            aggregated.append({"symbol": symbol, "reference": vendor, "basis": basis, "expected_sessions": len(sessions),
                               "tdx_sessions": len(sessions.intersection(tdx.index)),
                               "reference_sessions": len(sessions.intersection(ref.index)),
                               **{s: numerical[s] for s in ("EXACT_MATCH", "DIFFERENT", "NOT_COMPARED")},
                               **{s: qualification[s] for s in ("SOURCE_UNIT_UNVERIFIED", "SOURCE_SEMANTICS_NOT_COMPARABLE", "NO_VALID_REFERENCE")},
                               "scale_basis": "EXISTING_CONVERTER_FIXED_CANDIDATE_100", "financially_verified": False})
    pd.DataFrame(detailed).to_csv(output / "frozen-date-comparisons-private.csv", index=False)
    pd.DataFrame(aggregated).to_csv(output / "frozen-comparison.csv", index=False)
    return aggregated


def audit_gbbq(hq_cache, decoder, output):
    inputs = {}
    for name in ("gbbq", "gbbq.map"):
        content, sha = read_snapshot(hq_cache / name, hq_cache)
        inputs[name] = {"sha256": sha, "bytes": len(content)}
    content, _ = read_snapshot(hq_cache / "gbbq", hq_cache)
    records = candidate_gbbq(content, Path(decoder).read_bytes())
    frame = pd.DataFrame.from_records(records)
    frame["code"] = frame.code.str.decode("ascii")
    frame["parsed_date"] = pd.to_datetime(frame.date.astype(str), format="%Y%m%d", errors="coerce")
    frame.to_parquet(output / "gbbq-candidates-private.parquet", index=False)
    sample = frame[frame.code.eq("600000") & frame.market.eq(1)]
    pilot = sample[sample.date.between(20240101, 20260911)]
    pilot.to_csv(output / "gbbq-600000-events-private.csv", index=False)
    event = pilot[pilot.date.eq(20250716) & pilot.category.eq(1)]
    # One pre-existing issuer-announcement event; float32 is recorded exactly,
    # never rounded into an independently authenticated event.
    cash_difference = None if len(event) != 1 else str(Decimal.from_float(float(event.iloc[0].c1)) / 10 - Decimal("0.41"))
    summary = {"status": "CANDIDATE_LAYOUT_ONLY_HFQ_UNVERIFIED", "inputs": inputs,
               "decoder_reference_commit": DECODER_COMMIT, "decoder_reference_sha256": DECODER_SHA,
               "decoder_reference": "https://github.com/rainx/pytdx/blob/" + DECODER_COMMIT + "/pytdx/reader/gbbq_reader.py",
               "records": len(frame), "layout": "4 + count * 29; first 24 candidate decrypted, last 5 retained",
               "invalid_codes": int((~frame.code.str.fullmatch(r"[0-9]{6}")).sum()),
               "invalid_dates": int(frame.parsed_date.isna().sum()),
               "market_bytes": {str(k): int(v) for k, v in frame.market.value_counts().items()},
               "category_bytes": {str(k): int(v) for k, v in frame.category.value_counts().items()},
               "date_min": str(frame.parsed_date.min().date()), "date_max": str(frame.parsed_date.max().date()),
               "duplicate_market_code_date_category": int(frame.duplicated(["market", "code", "date", "category"]).sum()),
               "finite_four_values": bool(np.isfinite(frame[["c1", "c2", "c3", "c4"]].to_numpy()).all()),
               "market_identity": "UNVERIFIED_ENUMERATION", "event_semantics": "CANDIDATE_ONLY",
               "historical_event_completeness": "UNKNOWN", "corrections_and_vintages": "UNKNOWN",
               "anchor": None, "anchor_status": "UNVERIFIED", "historical_float_shares": "UNVERIFIED",
               "computed_hfq": None, "certified_hfq_sessions": 0,
               "pilot": {"symbol": "600000.SH", "selection": "pre-existing single issuer event; 2024-2026 diagnostic candidates",
                         "candidate_events": len(pilot), "official_event_date": "2025-07-16",
                         "official_cash_per_share": "0.41", "matching_category1_records": len(event),
                         "candidate_cash_minus_official_exact": cash_difference,
                         "float32_rounding_contract": "UNVERIFIED", "hfq": "HFQ_UNVERIFIED"}}
    save_json(output / "gbbq-summary.json", summary)
    return summary


def coverage_svg(rows, target):
    months = sorted({row["month"] for row in rows})
    cells, labels = [], []
    for y, market in enumerate(("sh", "sz", "bj")):
        labels.append('<text x="12" y="%s">%s</text>' % (70 + y * 44, market.upper()))
        for x, month in enumerate(months):
            group = [r for r in rows if r["month"] == month and r["market_directory_candidate"] == market]
            expected = sum(r["expected_grid"] for r in group)
            observed = sum(r["observed_grid"] for r in group)
            ratio = observed / expected if expected else 0
            color = "rgb(%s,%s,180)" % (int(245 - 190 * ratio), int(245 - 70 * ratio))
            cells.append('<rect x="%s" y="%s" width="12" height="30" fill="%s"><title>%s %s: %s/%s</title></rect>' %
                         (60 + x * 13, 48 + y * 44, color, market, month, observed, expected))
            if month.endswith("-01") or x == 0:
                labels.append('<text x="%s" y="200" font-size="11">%s</text>' % (60 + x * 13, month[:4]))
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="260" viewBox="0 0 960 260"><rect width="960" height="260" fill="white"/><g font-family="sans-serif" fill="#182b3a"><text x="12" y="25">Local TDX observed / native benchmark session grid (all instrument candidates)</text>' + ''.join(cells + labels) + '<text x="12" y="230" font-size="12">2021-03-08 to 2026-09-11; darker = higher observed coverage; missing reason UNKNOWN.</text><text x="12" y="250" font-size="12">Directory markets and security identities unverified. This is not CSI500/PIT member coverage.</text></g></svg>'
    write_new(target, svg.encode("utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pilot-manifest", type=Path, required=True)
    parser.add_argument("--decoder-reference", type=Path, required=True)
    args = parser.parse_args(argv)
    def no_network(*unused, **kwargs):
        raise RuntimeError("offline audit prohibits sockets")
    socket.socket.connect = no_network
    socket.create_connection = no_network
    return audit_assets(args.private_root, args.output, args.pilot_manifest, args.decoder_reference)


if __name__ == "__main__":
    raise SystemExit(main())
