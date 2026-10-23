"""Supplemental offline reference comparison; never promotes RAW data.

Use a CSV with actual, independently observed prices and explicit display
units/tolerances. Blank template rows are not reference evidence.
"""

import argparse
import io
import json
from pathlib import Path
import re
import uuid

import numpy as np
import pandas as pd

from factorlab.data.tdx import TdxError, read_snapshot, safe_path
from factorlab.data.tdx_staging import disjoint, json_bytes, sha256, write_csv, write_new


REFERENCE_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount",
                     "price_unit", "volume_unit", "amount_unit", "adjustment",
                     "price_tolerance", "volume_tolerance", "amount_tolerance", "source", "evidence"]


def compare_samples(report_dir: Path, reference: Path, output_root: Path,
                    run_id=None):
    report_dir, reference, output_root = safe_path(report_dir), safe_path(reference), safe_path(output_root)
    disjoint(report_dir, output_root)
    manifest_bytes, manifest_sha = read_snapshot(report_dir / "manifest.json", report_dir)
    manifest = json.loads(manifest_bytes)
    if manifest.get("status") not in {"engineering_complete", "partial"}:
        raise TdxError("SOURCE_AUDIT_NOT_COMPLETE")
    sample_bytes, sample_sha = read_snapshot(report_dir / "verification_samples.csv", report_dir)
    if sample_sha != manifest["artifacts"]["verification_samples.csv"]["sha256"]:
        raise TdxError("SAMPLE_HASH_MISMATCH")
    samples = pd.read_csv(io.BytesIO(sample_bytes))
    reference_bytes, reference_sha = read_snapshot(reference, reference.parent)
    values = pd.read_csv(io.BytesIO(reference_bytes), dtype={"symbol": str, "date": str})
    if set(values.columns) != set(REFERENCE_COLUMNS) or values.empty or values.isna().any().any():
        raise TdxError("COMPLETE_EXPLICIT_REFERENCE_COLUMNS_REQUIRED")
    if values.duplicated(["symbol", "date"]).any():
        raise TdxError("DUPLICATE_REFERENCE_KEY")
    for key in ("open", "high", "low", "close", "volume", "amount", "price_tolerance", "volume_tolerance", "amount_tolerance"):
        values[key] = pd.to_numeric(values[key], errors="raise")
        if not np.isfinite(values[key]).all() or (values[key] < 0).any():
            raise TdxError("INVALID_REFERENCE_VALUE: %s" % key)
    if not values.symbol.str.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)").all() or not values.date.str.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}").all():
        raise TdxError("INVALID_REFERENCE_SYMBOL_OR_DATE")
    pd.to_datetime(values.date, format="%Y-%m-%d", errors="raise")
    if not values.adjustment.eq("UNADJUSTED").all():
        raise TdxError("UNADJUSTED_REFERENCE_REQUIRED")
    for key in ("source", "evidence"):
        if values[key].str.strip().isin(["", "UNKNOWN", "UNVERIFIED"]).any():
            raise TdxError("EXPLICIT_REFERENCE_EVIDENCE_REQUIRED")
    joined = values.merge(samples, on=["date", "symbol"], how="left", suffixes=("_reference", "_candidate"), validate="one_to_one", indicator=True)
    rows = []
    for _, row in joined.iterrows():
        result = {"symbol": row.symbol, "date": row.date, "source": row.source, "evidence": row.evidence,
                  "financial_research_eligible": False, "scope": "ONLY_THIS_SYMBOL_DATE_REFERENCE_ROW"}
        if row["_merge"] != "both":
            result["status"] = "NO_AUDITED_SAMPLE"
            rows.append(result)
            continue
        result["status"] = "CANDIDATE_COMPARISON_ONLY"
        result["source_sha256"] = row.source_sha256
        for field in ("open", "high", "low", "close"):
            delta = row[field + "_candidate"] - row[field + "_reference"]
            result[field + "_difference"] = delta
            result[field + "_candidate_match"] = "UNKNOWN" if pd.isna(delta) or row.price_unit not in {"CNY", "POINTS", "USD", "HKD"} else bool(abs(delta) <= row.price_tolerance)
        volume_factor = {"SHARES": 1, "LOTS_100_SHARES": 100}.get(row.volume_unit)
        amount_factor = {"CNY": 1, "TEN_THOUSAND_CNY": 10000, "HUNDRED_MILLION_CNY": 100000000}.get(row.amount_unit)
        for hypothesis in (1, 100):
            result["volume_candidate_multiplier_%s_match" % hypothesis] = "UNKNOWN" if volume_factor is None else bool(abs(row.volume_candidate * hypothesis - row.volume_reference * volume_factor) <= row.volume_tolerance * volume_factor)
        result["amount_candidate_CNY_match"] = "UNKNOWN" if amount_factor is None else bool(abs(row.amount_candidate - row.amount_reference * amount_factor) <= row.amount_tolerance * amount_factor)
        result.update(price_unit_reference=row.price_unit, volume_unit_reference=row.volume_unit, amount_unit_reference=row.amount_unit)
        rows.append(result)
    run_id = run_id or "reference-" + uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
        raise TdxError("INVALID_REFERENCE_RUN_ID")
    output = safe_path(output_root / run_id, output_root)
    output.mkdir(parents=True, exist_ok=False)
    write_new(output / "reference_input.csv", reference_bytes)
    write_csv(output / "comparisons.csv", rows)
    write_new(output / "manifest.json", json_bytes({"schema": "TDX_SUPPLEMENTAL_REFERENCE_V1", "status": "comparisons_complete",
              "financial_research_eligible": False, "network_requests": 0,
              "source_audit_manifest_sha256": manifest_sha, "source_samples_sha256": sample_sha,
              "reference_sha256": reference_sha, "reference_source_file": str(reference),
              "comparison_sha256": sha256((output / "comparisons.csv").read_bytes()),
              "implementation_sha256": sha256(Path(__file__).read_bytes()),
              "scope": "Candidate numeric matches do not establish all-file units, adjustment, historical identities, PIT or status"}))
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description="补充独立TDX样本核验；不升级RAW或正式研究资格")
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--output-root", type=Path, default=Path("results/diagnostics/tdx-reference-checks"))
    args = parser.parse_args(argv)
    output = compare_samples(args.report_dir, args.reference, args.output_root)
    print("补充比较报告：%s；正式研究合格：False" % output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
