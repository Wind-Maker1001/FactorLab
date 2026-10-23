"""Separate-process offline audit review using struct and archived bytes.

Does not import the audit's DAY decoder/comparison functions. Reconciles claims,
all input SHA, exact raw four-price comparisons and sampled pytdx deciphering.
This is an independent implementation check, not external human certification.
"""

import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import runpy
import socket
import struct
import subprocess

import numpy as np
import pandas as pd


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(path):
    return json.loads(path.read_bytes())


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")


def review(audit, private, decoder, output):
    if output.exists():
        raise ValueError("review output must be new")
    allowed = (private / "results/diagnostics/tdx-data-quality-audit").resolve()
    if allowed not in output.resolve().parents:
        raise ValueError("review output must remain private")
    output.mkdir(parents=True)
    summary = load(audit / "summary.json")
    inventory = pd.read_csv(audit / "security-inventory-private.csv")
    old = private / "results/diagnostics/tdx-local-data/20261011-tdx-raw-conversion-v1"
    conversions = pd.read_csv(old / "conversion_status.csv").fillna("")
    by_source = inventory.set_index("source_file")
    conversion_manifest = load(old / "manifest.json")
    # The original run was dirty at 2759870. A later committed snapshot contains
    # the exact module bytes bound in that receipt; do not substitute today's
    # replay-capable decoder or claim this later commit was the acquisition HEAD.
    archived_commit = "46a163aa9e6f635525c29a0daa63408e6cfa983b"
    archived_modules = {name: subprocess.check_output(["git", "show", archived_commit + ":qbot/research/data/" + name])
                        for name in ("tdx_staging.py", "tdx.py")}
    recorded_modules = {Path(name).name: value for name, value in conversion_manifest["code"]["module_sha256"].items()}
    assert {name: sha(data) for name, data in archived_modules.items()} == recorded_modules
    implementation = sha(archived_modules["tdx_staging.py"] + archived_modules["tdx.py"])
    current_implementation = sha(b"".join(subprocess.check_output(["git", "show", "HEAD:qbot/research/data/" + name])
                                          for name in ("tdx_staging.py", "tdx.py")))
    contract = {"schema": "TDX_RAW_STAGING_V1", "format": "tdx-day-le32-raw-candidate-v1",
                "usage": "ENGINEERING_DIAGNOSTIC_ONLY_NO_LIVE_MONEY", "adjustment_status": "RAW_UNADJUSTED",
                "adjustment_evidence_status": "UNVERIFIED", "price_scale_status": "UNVERIFIED_OR_UNKNOWN",
                "volume_unit": "UNKNOWN", "amount_unit": "UNKNOWN", "financial_research_eligible": False,
                "format_verification": "LOCAL_STRUCTURE_PLAUSIBILITY_NOT_INDEPENDENT_REFERENCE",
                "production_schema": "NOT_FACTORLAB_CANONICAL_BARS"}
    counts, raw_by_symbol = Counter(), {}
    duplicates, invalid_ohlc = 0, 0
    samples = []
    symbols = ["600000.SH", "000001.SZ", "600519.SH", "000002.SZ", "300750.SZ",
               "688981.SH", "601059.SH", "001289.SZ", "600112.SH", "000961.SZ"]
    for position, row in enumerate(inventory.itertuples()):
        data = Path(row.source_file).read_bytes()
        assert sha(data) == row.source_sha256 and len(data) % 32 == 0
        records = list(struct.iter_unpack("<IIIIIfII", data))
        dates = [r[0] for r in records]
        duplicates += len(dates) - len(set(dates))
        invalid_ohlc += sum(h < max(o, c) or lo > min(o, c) or h < lo
                            for _, o, h, lo, c, *_ in records)
        counts["day_files"] += 1
        counts["day_records"] += len(records)
        if row.symbol in symbols:
            raw_by_symbol[row.symbol] = {str(r[0]): r[1:5] for r in records}
        if position % 1000 == 0 or row.symbol in symbols:
            samples.append(row.source_file)
        if (position + 1) % 1000 == 0:
            print("independent struct/SHA review", position + 1, flush=True)
    for row in conversions.itertuples():
        if row.status not in {"CONVERTED_RAW_ONLY", "REUSED_VERIFIED"}:
            continue
        data = Path(row.parquet_file).read_bytes()
        receipt = load(Path(row.parquet_file).parent / "receipt.json")
        assert sha(data) == receipt["parquet_sha256"] == row.parquet_sha256
        assert row.source_sha256 == receipt["source_sha256"]
        assert set(receipt) == {"schema", "identity", "contract", "implementation_sha256", "source_file",
                                "source_sha256", "rows", "symbol", "security", "parquet_sha256", "parquet_bytes"}
        assert receipt["contract"] == contract and receipt["schema"] == "TDX_RAW_STAGING_V1"
        assert receipt["implementation_sha256"] == implementation
        original_row = by_source.loc[row.source_file]
        for key, value in receipt["security"].items():
            expected = row.source_file if key == "source_file" else original_row[key]
            assert (pd.isna(expected) and value is None) or expected == value
        identity = sha(canonical_json({"contract": sha(canonical_json(contract)), "implementation": implementation,
                                      "source": row.source_file, "sha256": row.source_sha256,
                                      "parse_status": original_row.audit_status, "security": receipt["security"]}))
        assert receipt["identity"] == Path(row.parquet_file).parent.name == identity
        assert receipt["rows"] == row.rows and receipt["symbol"] == row.symbol and receipt["parquet_bytes"] == len(data)
        counts["receipt_full_contract_and_identity"] += 1
        counts["parquet_hashes"] += 1
        if row.source_file in samples:
            f = pd.read_parquet(Path(row.parquet_file))
            original = Path(row.source_file).read_bytes()
            for i in sorted({0, len(f) // 2, len(f) - 1}):
                expected = struct.unpack_from("<IIIIIfII", original, i * 32)
                r = f.iloc[i]
                actual = tuple([r.tdx_date_integer] + [r["tdx_" + k + "_integer"] for k in ("open", "high", "low", "close")] +
                               [r.amount, r.volume, r.tdx_reserved])
                assert expected == actual
                counts["parquet_struct_sample_records"] += 1
    assert counts["day_files"] == summary["counts"]["files"]
    assert counts["day_records"] == summary["counts"]["records"]
    assert counts["parquet_hashes"] == summary["counts"]["parquet_verified"]
    anomalies = pd.read_csv(audit / "anomalies.csv")
    assert invalid_ohlc == int(anomalies[anomalies.code.eq("INVALID_OHLC")].records.sum())
    assert duplicates == 0
    coverage = pd.read_csv(audit / "coverage-monthly.csv")
    assert (coverage.observed_grid <= coverage.expected_grid).all()
    for kind, group in coverage.groupby("security_type_candidate"):
        assert group.observed_grid.sum() == summary["counts"]["observed_grid_" + kind]
        assert (group.expected_grid - group.observed_grid).sum() == summary["counts"]["missing_grid_" + kind]
    details = pd.read_csv(audit / "frozen-date-comparisons-private.csv")
    public = pd.read_csv(audit / "frozen-comparison.csv")
    assert list(public.symbol.unique()) == symbols
    assert not public.financially_verified.any()
    for keys, group in details.groupby(["symbol", "reference", "basis"], sort=False):
        published = public[public.symbol.eq(keys[0]) & public.reference.eq(keys[1]) & public.basis.eq(keys[2])].iloc[0]
        for label in ("EXACT_MATCH", "DIFFERENT", "NOT_COMPARED"):
            assert (group.candidate_numerical_status == label).sum() == published[label]
        for label in ("SOURCE_UNIT_UNVERIFIED", "SOURCE_SEMANTICS_NOT_COMPARABLE", "NO_VALID_REFERENCE"):
            assert (group.status == label).sum() == published[label]
    # Independently compare all Eastmoney RAW rows to original DAY integer cents.
    # Native request/response-to-cache binding was separately replayed by verify_pilot.
    pilot = private / "data/research/five-year-pilot/20261015-native-calendar-v1/runs/20261015T111701027971Z-d51bb4d1"
    bars = pd.read_parquet(pilot / "bars.parquet")
    for symbol in symbols:
        compared = Counter()
        for row in bars[bars.symbol.eq(symbol)].itertuples():
            day = row.date.strftime("%Y%m%d")
            if day not in raw_by_symbol.get(symbol, {}) or pd.isna(row.raw_open):
                continue
            reference = [row.raw_open, row.raw_high, row.raw_low, row.raw_close]
            match = all(Decimal(v) / 100 == Decimal(str(r)) for v, r in zip(raw_by_symbol[symbol][day], reference))
            compared["EXACT_MATCH" if match else "DIFFERENT"] += 1
        r = public[public.symbol.eq(symbol) & public.reference.eq("EASTMONEY") & public.basis.eq("RAW")].iloc[0]
        assert compared["EXACT_MATCH"] == r.EXACT_MATCH and compared["DIFFERENT"] == r.DIFFERENT
    # Cross-check nine actual encrypted records using the original pinned scalar
    # pytdx implementation, including the pre-selected official 2025 cash event.
    decoder_bytes = decoder.read_bytes()
    assert sha(decoder_bytes) == "e28fae76760b1247a960cab285aa357c2d263b3212e7cabfb3f1ebfcc127d6ba"
    root = Path(load(old / "manifest.json")["source_root"]).parent
    encrypted = (root / "T0002/hq_cache/gbbq").read_bytes()
    decoded = pd.read_parquet(audit / "gbbq-candidates-private.parquet")
    event = decoded.index[decoded.code.eq("600000") & decoded.market.eq(1) & decoded.date.eq(20250716)][0]
    selected = sorted({0, 1, 2, len(decoded) // 3, len(decoded) // 2, len(decoded) - 3,
                       len(decoded) - 2, len(decoded) - 1, int(event)})
    trial = output / "gbbq-subset-private.bin"
    trial.write_bytes(struct.pack("<I", len(selected)) + b"".join(encrypted[4 + i * 29:4 + (i + 1) * 29] for i in selected))
    # Only this SHA-pinned, inspected implementation is executed, offline.
    namespace = runpy.run_path(str(decoder), run_name="_pinned_review_reference")
    reference = namespace["GbbqReader"]().get_df(str(trial))
    candidate = decoded.iloc[selected][["market", "code", "date", "category", "c1", "c2", "c3", "c4"]]
    for a, b in zip(candidate.itertuples(index=False, name=None), reference.itertuples(index=False, name=None)):
        assert a[:4] == b[:4]
        np.testing.assert_array_equal(np.array(a[4:], dtype="float32"), np.array(b[4:], dtype="float32"))
    publication = load(audit / "publication-receipt.json")
    for name, item in publication["files"].items():
        assert sha((audit / name).read_bytes()) == item["sha256"]
    assert summary["integrity_errors"] == summary["private_files_changed"] == 0
    assert summary["tracked_baseline_changed"] == [] and summary["financial_admission"] is False
    gbbq = load(audit / "gbbq-summary.json")
    assert gbbq["computed_hfq"] is None and gbbq["anchor"] is None and gbbq["certified_hfq_sessions"] == 0
    result = {"schema": "TDX_SEPARATE_IMPLEMENTATION_REVIEW_V1", "status": "PASS",
              "review_scope": "separate process/stdlib struct and Decimal; same operator; human review pending",
              "audit_summary_sha256": sha((audit / "summary.json").read_bytes()),
              "review_script_sha256": sha(Path(__file__).read_bytes()), "counts": dict(counts),
              "conversion_source_reference_commit": archived_commit,
              "conversion_original_run_commit": conversion_manifest["code"]["git_commit"],
              "conversion_original_run_dirty": conversion_manifest["code"]["git_dirty"],
              "conversion_module_sha256": recorded_modules,
              "conversion_implementation_sha256": implementation,
              "current_git_blob_implementation_sha256": current_implementation,
              "source_version_binding": "old receipt matches archived original bytes; current decoder roundtrip separately verified; old receipts unchanged",
              "invalid_ohlc_records": invalid_ohlc, "duplicate_security_dates": duplicates,
              "gbbq_upstream_scalar_sample_records": len(selected), "network_requests": 0,
              "financial_admission": False, "hfq": "HFQ_UNVERIFIED",
              "human_independent_review": "PENDING_PR_REVIEW"}
    (output / "review.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("audit", "private-root", "decoder-reference", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    args = p.parse_args()
    def denied(*unused, **kwargs):
        raise RuntimeError("offline review prohibits sockets")
    socket.socket.connect = denied
    socket.create_connection = denied
    review(args.audit, args.private_root, args.decoder_reference, args.output)


if __name__ == "__main__":
    main()
