"""Independent read-only acceptance of existing YHH Parquet and exceptional rows.

Does not call the converter, normalization validator or adapter verifier. Raw
CSV re-reads are restricted to exceptional dates and stratified late samples.
Full raw values stay in the caller-selected ignored diagnostics directory.
"""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import zipfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


COLUMNS = ["symbol", "vendor_symbol", "security_name", "trade_date", "timestamp_local", "open", "high", "low",
           "close", "volume_vendor", "amount_vendor", "adjustment_factor_vendor", "source_id", "source_row", "quality_flags"]
NUMERIC = COLUMNS[5:12]
RAW = ["证券代码", "证券名称", "交易日期", "分钟时间", "开盘价（元）", "最高价（元）", "最低价（元）", "收盘价（元）", "成交量", "成交额", "复权因子"]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def need(condition, message):
    if not condition:
        raise ValueError(message)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def output_csv(path, rows):
    if not rows:
        return
    with Path(path).open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def review(repo, dataset, source, audit, output, producer_commit):
    need(not output.exists(), "NEW_REVIEW_OUTPUT_DIRECTORY_REQUIRED")
    output.mkdir(parents=True)
    catalog = load(dataset / "dataset.json")
    original = load(repo / "docs/research/evidence/yhh30m-conversion-v1/summary.json")
    need(catalog.get("complete") is True and sha(dataset / "dataset.json") == original["dataset_sha256"], "PINNED_CATALOG_MISMATCH")
    contract, selection = catalog["contract"], catalog["selection"]
    need(contract == selection["contract"] and contract["formal_admission"] is False and
         contract["financial_research_eligible"] is False and contract["daily_aggregation"] == "DISABLED", "QUALIFICATION_MARKER_CHANGED")
    need(hashlib.sha256(encoded(selection)).hexdigest() == catalog["selection_id"], "SELECTION_HASH_MISMATCH")
    producer = hashlib.sha256()
    names = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", producer_commit,
                                     "qbot/research/data/yhh30m"], cwd=repo, text=True).splitlines()
    for name in sorted(n for n in names if n.endswith(".py")):
        producer.update(Path(name).name.encode())
        producer.update(subprocess.check_output(["git", "show", producer_commit + ":" + name], cwd=repo))
    need(producer.hexdigest() == selection["implementation_sha256"] == original["implementation_sha256"], "HISTORICAL_PRODUCER_CODE_MISMATCH")
    with (audit / "daily-counts.csv").open(encoding="utf-8-sig", newline="") as handle:
        day_audit = {r["date"]: r for r in csv.DictReader(handle)}
    old_hashes = load(audit / "input-hashes.json")
    containers = {k.replace("\\", "/").rsplit("/", 1)[-1]: v for k, v in old_hashes["containers"].items()}
    for name, expected in containers.items():
        need(sha(source / name) == expected, "ORIGINAL_CONTAINER_HASH_CHANGED: " + name)
    counters, late_year, late_exchange, late_source = Counter(), Counter(), Counter(), Counter()
    late_symbols, late_days, labels = set(), set(), Counter()
    daily_late, affected, negative, raw_requests = [], [], [], defaultdict(set)
    totals = Counter()
    partition_results, all_dates, all_late = [], [], []
    def inspect_day(frame, receipt_day):
        day = receipt_day["date"]
        audit_day = day_audit[day]
        need(len(frame) == int(audit_day["rows"]) == receipt_day["rows"], "DAILY_ROWS_MISMATCH: " + day)
        need(frame.source_id.eq(audit_day["content_sha256"]).all(), "DAILY_SOURCE_SHA_MISMATCH")
        need(frame.source_row.tolist() == list(range(1, len(frame) + 1)), "SOURCE_ROW_ORDER_MISMATCH")
        need(frame.symbol.str.fullmatch(r"\d{6}\.(SH|SZ|BJ)").all(), "CODE_FORMAT_MISMATCH")
        need(frame.symbol.eq(frame.vendor_symbol.str.strip().str.upper()).all(), "CODE_MAPPING_MISMATCH")
        need(not frame.duplicated(["symbol", "timestamp_local"]).any(), "DUPLICATE_PRIMARY_KEY")
        need(frame.timestamp_local.dt.strftime("%Y-%m-%d").eq(day).all() and frame.trade_date.astype(str).eq(day).all(), "DATE_TIMESTAMP_MISMATCH")
        time = frame.timestamp_local.dt.strftime("%H:%M:%S")
        labels.update(time.tolist())
        late = frame.loc[time.eq("15:30:00")].copy()
        missing = frame.adjustment_factor_vendor.isna()
        nv, na = frame.volume_vendor.lt(0), frame.amount_vendor.lt(0)
        bad_price = frame[["open", "high", "low", "close"]].le(0).any(axis=1)
        bad_ohlc = frame.high.lt(frame[["open", "low", "close"]].max(axis=1)) | frame.low.gt(frame[["open", "high", "close"]].min(axis=1))
        values = frame[NUMERIC].to_numpy()
        counts = {"null_cells": int(frame[COLUMNS[:12]].isna().sum().sum()),
                  "nonfinite_numeric_cells": int((~np.isfinite(values)).sum()),
                  "negative_volume_rows": int(nv.sum()), "negative_amount_rows": int(na.sum()),
                  "nonpositive_price_rows": int(bad_price.sum()), "ohlc_logic_rows": int(bad_ohlc.sum()),
                  "zero_volume_rows": int(frame.volume_vendor.eq(0).sum()), "zero_amount_rows": int(frame.amount_vendor.eq(0).sum())}
        for key, value in counts.items():
            need(value == int(audit_day[key]), "DAILY_PRIOR_AUDIT_CHECK_MISMATCH: " + key)
        counters.update(counts)
        need(int(frame.symbol.nunique()) == int(audit_day["securities"]), "DAILY_SYMBOL_COUNT_MISMATCH")
        need(frame.loc[missing, "quality_flags"].map(lambda v: bool(v & 1 and v & 2)).all(), "MISSING_FACTOR_FLAGS_MISMATCH")
        need(frame.loc[nv, "quality_flags"].map(lambda v: bool(v & 16)).all(), "NEGATIVE_VOLUME_FLAG_MISMATCH")
        need(frame.loc[na, "quality_flags"].map(lambda v: bool(v & 32)).all(), "NEGATIVE_AMOUNT_FLAG_MISMATCH")
        errors = frame.loc[missing | nv | na]
        for row in errors.to_dict("records"):
            raw_requests[day].add(int(row["source_row"]))
            affected.append(row)
        if len(late):
            need((late.quality_flags.astype(int) & 192).eq(192).all(), "LATE_FLAGS_MISMATCH")
            late_year.update({day[:4]: len(late)})
            late_exchange.update(late.symbol.str.rsplit(".", n=1).str[-1].tolist())
            late_source.update({audit_day["container"]: len(late)})
            late_symbols.update(late.symbol)
            late_days.add(day)
            all_late.append(late)
            symbols = set(late.symbol)
            group = frame[frame.symbol.isin(symbols)].sort_values("timestamp_local")
            agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume_vendor": "sum", "amount_vendor": "sum"}
            including = group.groupby("symbol").agg(agg)
            excluding = group.loc[~group.timestamp_local.dt.strftime("%H:%M:%S").eq("15:30:00")].groupby("symbol").agg(agg)
            need(including.index.equals(excluding.index), "LATE_ONLY_SECURITY_DAY")
            impact = {"date": day, "late_rows": len(late), "late_symbols": len(symbols)}
            for field in agg:
                changed = ~np.isclose(including[field], excluding[field], rtol=0, atol=0, equal_nan=True)
                impact[field + "_changed_security_days"] = int(changed.sum())
                totals[field + "_changed_security_days"] += int(changed.sum())
            impact["late_zero_volume"] = int(late.volume_vendor.eq(0).sum())
            impact["late_positive_volume"] = int(late.volume_vendor.gt(0).sum())
            totals["late_zero_volume"] += impact["late_zero_volume"]
            totals["late_positive_volume"] += impact["late_positive_volume"]
            totals["affected_security_days"] += len(symbols)
            daily_late.append(impact)
        all_dates.append(day)
    for part in catalog["partitions"]:
        receipt_path, parquet_path = dataset / part["receipt"], dataset / part["parquet"]
        need(sha(receipt_path) == part["receipt_sha256"] and sha(parquet_path) == part["parquet_sha256"], "ARTIFACT_SHA_MISMATCH")
        receipt, pf = load(receipt_path), pq.ParquetFile(parquet_path)
        need(pf.schema_arrow.names == COLUMNS and pf.metadata.num_rows == part["rows"] == receipt["rows"], "PARTITION_SCHEMA_OR_ROWS_MISMATCH")
        need(json.loads(pf.schema_arrow.metadata[b"factorlab_contract"]) == contract == receipt["contract"], "PARQUET_CONTRACT_MISMATCH")
        need(receipt["implementation_sha256"] == producer.hexdigest() and receipt["audit_evidence"] == selection["audit_evidence"], "RECEIPT_PRODUCER_OR_AUDIT_MISMATCH")
        need([d["date"] for d in receipt["days"]] == part["dates"], "RECEIPT_DATE_MISMATCH")
        by_day = {d["date"]: d for d in receipt["days"]}
        fingerprint, frames, current_day, rows = hashlib.sha256(), [], None, 0
        for batch in pf.iter_batches(batch_size=32768):
            frame = batch.to_pandas()
            fingerprint.update(pd.util.hash_pandas_object(frame, index=False, categorize=False).to_numpy(dtype="<u8").tobytes())
            rows += len(frame)
            for day, segment in frame.groupby(frame.trade_date.astype(str), sort=False):
                if current_day is not None and day != current_day:
                    inspect_day(pd.concat(frames, ignore_index=True), by_day[current_day])
                    frames = []
                current_day = day
                frames.append(segment)
        if frames:
            inspect_day(pd.concat(frames, ignore_index=True), by_day[current_day])
        need(fingerprint.hexdigest() == receipt["logical_sha256"] and rows == part["rows"], "OUTPUT_LOGICAL_FINGERPRINT_MISMATCH")
        partition_results.append({"month": part["month"], "rows": rows, "sha256": part["parquet_sha256"], "status": "PASS"})
        print(json.dumps({"event": "independent_partition", "month": part["month"], "rows": rows}), flush=True)
    need(all_dates == selection["dates"] == sorted(day_audit), "FULL_DATE_COVERAGE_MISMATCH")
    need(sum(p["rows"] for p in partition_results) == catalog["rows"] == 64115523, "FULL_ROW_RECONCILIATION_MISMATCH")
    late = pd.concat(all_late, ignore_index=True)
    need(len(late) == 189364 and counters["negative_volume_rows"] == 13 and counters["negative_amount_rows"] == 12 and counters["null_cells"] == 126, "EXPECTED_EXCEPTION_COUNTS_CHANGED")
    # Stratify original-byte spot checks across years, suffixes and date boundaries.
    for _, group in late.groupby([late.trade_date.astype(str).str[:4], late.symbol.str.rsplit(".", n=1).str[-1]]):
        for row in pd.concat([group.head(2), group.tail(2)]).to_dict("records"):
            raw_requests[str(row["trade_date"])].add(int(row["source_row"]))
    actual_rows = {str(d): g.set_index("source_row") for d, g in pd.concat([late, pd.DataFrame(affected)], ignore_index=True).drop_duplicates(["source_id", "source_row"]).groupby("trade_date")}
    checked, checked_dates = 0, []
    for day, offsets in sorted(raw_requests.items()):
        item = day_audit[day]
        if item["container"] == "loose_2026_csv":
            content = (source / item["locator"]).read_bytes()
        else:
            with zipfile.ZipFile(source / item["container"]) as archive:
                content = archive.read(item["locator"])
        need(hashlib.sha256(content).hexdigest() == item["content_sha256"], "SPOT_SOURCE_SHA_MISMATCH")
        found = set()
        for offset, raw in enumerate(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))), 1):
            if offset not in offsets:
                continue
            row = actual_rows[day].loc[offset]
            need(raw[RAW[0]] == row.vendor_symbol and raw[RAW[0]].strip().upper() == row.symbol and
                 raw[RAW[1]] == row.security_name and datetime.strptime(raw[RAW[2]], "%Y%m%d").date() == row.trade_date and
                 datetime.strptime(raw[RAW[3]], "%Y-%m-%d %H:%M:%S") == row.timestamp_local, "ORIGINAL_IDENTITY_FIELDS_MISMATCH")
            for original_name, column in zip(RAW[4:], NUMERIC):
                value = float(raw[original_name]) if raw[original_name] else float("nan")
                need((pd.isna(value) and pd.isna(row[column])) or value == row[column], "ORIGINAL_NUMERIC_FIELD_MISMATCH")
            found.add(offset)
            checked += 1
        need(found == offsets, "ORIGINAL_SOURCE_ROW_MISSING")
        checked_dates.append(day)
    late.to_csv(output / "late-records.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(affected).to_csv(output / "exception-records.csv", index=False, encoding="utf-8-sig")
    output_csv(output / "daily-late-impact.csv", daily_late)
    exception_table = pd.DataFrame(affected)
    exception_groups = []
    for (day, symbol), g in exception_table.groupby([exception_table.trade_date.astype(str), "symbol"]):
        exception_groups.append({"date": day, "symbol": symbol, "rows": len(g),
                                 "missing_factor": int(g.adjustment_factor_vendor.isna().sum()),
                                 "negative_volume": int(g.volume_vendor.lt(0).sum()), "negative_amount": int(g.amount_vendor.lt(0).sum())})
    output_csv(output / "exception-groups.csv", exception_groups)
    result = {"schema": "YHH_INDEPENDENT_DATA_ACCEPTANCE_V1", "reviewed_at_utc": datetime.now(timezone.utc).isoformat(),
              "parquet_rows_read": catalog["rows"], "partitions": len(partition_results), "days": len(all_dates),
              "source_containers_sha_checked": len(containers), "producer_commit": producer_commit,
              "producer_implementation_sha256": producer.hexdigest(), "dataset_sha256": sha(dataset / "dataset.json"),
              "checks": dict(counters), "late_rows": len(late), "late_distinct_symbols": len(late_symbols),
              "late_dates": len(late_days), "late_first_date": min(late_days), "late_last_date": max(late_days),
              "late_year_counts": dict(late_year), "late_supplied_exchange_suffix": dict(late_exchange),
              "late_source_containers": dict(late_source), "late_aggregation_sensitivity": dict(totals),
              "exception_unique_rows": len(exception_table), "exception_groups": exception_groups,
              "original_raw_rows_independently_checked": checked, "original_raw_dates_checked": checked_dates,
              "partition_results": partition_results, "formal_admission": False,
              "timestamp_semantics": "UNVERIFIED_VENDOR_LABELS;SENSITIVITY_IS_NOT_AN_APPROVED_AGGREGATION",
              "security_type": "SUPPLIED_SUFFIX_ONLY_HISTORY_NOT_CERTIFIED", "status": "PASS_ENGINEERING_REPRODUCTION"}
    (output / "data-acceptance.json").write_bytes(encoded(result))
    print(json.dumps({k: v for k, v in result.items() if k not in {"partition_results", "exception_groups"}}, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "dataset", "source", "audit", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--producer-commit", default="129dbbd201fd640d58b0d787bd0089883c9dd402")
    args = parser.parse_args()
    review(args.repo, args.dataset, args.source, args.audit, args.output, args.producer_commit)


if __name__ == "__main__":
    main()
