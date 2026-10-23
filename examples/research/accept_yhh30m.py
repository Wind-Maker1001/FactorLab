"""Independent small-sample checks and a compact full conversion inventory.

Pilot mode reads only the selected day/month again for acceptance. Summary mode
uses the newly generated, hash-checked monthly receipts without rescanning CSVs.
"""

import argparse
from collections import Counter
import csv
from datetime import datetime
import io
import json
import math
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from factorlab.engineering.yhh30m import SCHEMA, IntradayParquetProvider
from factorlab.engineering.yhh30m.contract import RAW_COLUMNS, NUMERIC_COLUMNS
from factorlab.engineering.yhh30m.normalize import DayValidator
from factorlab.engineering.yhh30m.source import file_sha, load_inventory, open_source
from factorlab.engineering.yhh30m.storage import now, write_json


def accept(dataset, output, *, source=None, audit=None):
    provider = IntradayParquetProvider(dataset, purpose="engineering")
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("NEW_ACCEPTANCE_DIRECTORY_REQUIRED")
    output.mkdir(parents=True, exist_ok=True)
    receipts, inventory = [], []
    checks, flags, missing, labels = Counter(), Counter(), Counter(), Counter()
    total_days, samples = 0, 0
    for part in provider.catalog["partitions"]:
        path = provider._path(part["receipt"])
        parquet = provider._path(part["parquet"])
        assert file_sha(path) == part["receipt_sha256"]
        assert file_sha(parquet) == part["parquet_sha256"]
        receipt = json.loads(path.read_text(encoding="utf-8"))
        assert pq.ParquetFile(parquet).metadata.num_rows == part["rows"]
        assert receipt["output_roundtrip_verified"]
        receipts.append(receipt)
        inventory.append({"month": part["month"], "rows": part["rows"], "days": len(part["dates"]),
                          "parquet_bytes": part["parquet_bytes"], "parquet": part["parquet"],
                          "parquet_sha256": part["parquet_sha256"], "receipt": part["receipt"],
                          "receipt_sha256": part["receipt_sha256"]})
        for day in receipt["days"]:
            total_days += 1
            checks.update(day["checks"])
            flags.update(day["flags"])
            missing.update(day["missing"])
            labels.update(day["time_labels"])
            samples += len(day["samples"])
    independent = {"status": "NOT_RUN_FULL_DATASET_USES_PER_PARTITION_CONVERSION_ROUNDTRIP"}
    if source is not None:
        assert audit is not None
        assert provider.catalog["rows"] <= 2_000_000, "Pilot must be a day or month, not a second full audit"
        batch_tables = list(provider.iter_batches())
        actual = pa.Table.from_batches(batch_tables, schema=SCHEMA).combine_chunks()
        frame = actual.to_pandas()
        assert not frame.duplicated(["symbol", "timestamp_local"]).any()
        all_items, _, _ = load_inventory(source, audit)
        chosen = [d for d in all_items if d["date"] in provider.catalog["selection"]["dates"]]
        assert sum(int(d["rows"]) for d in chosen) == len(frame)
        exact_days, raw_samples = 0, 0
        for item in chosen:
            # One-shot parsing of each complete daily input vs the streaming
            # conversion. It exercises real ZIP members and loose daily CSVs.
            with open_source(source, item) as handle:
                content = handle.read()
            types = {c: pa.string() if i < 4 else pa.float64() for i, c in enumerate(RAW_COLUMNS)}
            parsed = pacsv.read_csv(io.BytesIO(content), convert_options=pacsv.ConvertOptions(
                column_types=types, strings_can_be_null=True)).combine_chunks()
            one_shot = DayValidator(item).convert(parsed.to_batches(max_chunksize=len(parsed))[0])
            selected = actual.filter(pc.equal(actual["source_id"], item["content_sha256"]))
            assert one_shot.equals(selected, check_metadata=True), item["date"]
            exact_days += 1
            day_receipt = next(d for r in receipts for d in r["days"] if d["date"] == item["date"])
            wanted = {s["source_row"] for s in day_receipt["samples"]}
            # A separate stdlib parser and hand-written mapping, independent of
            # DayValidator, check every original field of seeded random rows.
            selected_frame = selected.to_pandas().set_index("source_row")
            for row_index, row in enumerate(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))), 1):
                if row_index not in wanted:
                    continue
                result = selected_frame.loc[row_index]
                assert result.symbol == row["证券代码"].strip().upper()
                assert result.vendor_symbol == row["证券代码"]
                assert result.security_name == row["证券名称"]
                assert result.trade_date == datetime.strptime(row["交易日期"], "%Y%m%d").date()
                assert result.timestamp_local == datetime.strptime(row["分钟时间"], "%Y-%m-%d %H:%M:%S")
                for raw_name, normalized in zip(RAW_COLUMNS[4:], NUMERIC_COLUMNS):
                    value = row[raw_name]
                    if value in {"", "NaN", "nan"}:
                        assert pd.isna(result[normalized])
                    else:
                        assert result[normalized] == float(value)
                raw_samples += 1
        independent = {"status": "PASS", "whole_daily_input_one_shot_vs_streaming_equal": exact_days,
                       "stdlib_csv_samples_all_11_fields_equal": raw_samples,
                       "primary_keys_unique": True, "selected_input_output_rows_equal": True}
    result = {"schema": "FACTORLAB_YHH_ACCEPTANCE_V1", "verified_at_utc": now(),
              "dataset": str(Path(dataset).absolute()), "dataset_sha256": file_sha(Path(dataset) / "dataset.json"),
              "implementation_sha256": provider.catalog["selection"]["implementation_sha256"],
              "audit_evidence": provider.catalog["selection"]["audit_evidence"],
              "rows": provider.catalog["rows"], "days": total_days, "partitions": len(inventory),
              "first_date": min(provider.catalog["selection"]["dates"]),
              "last_date": max(provider.catalog["selection"]["dates"]),
              "parquet_bytes": sum(p["parquet_bytes"] for p in inventory),
              "checks": dict(checks), "missing": dict(missing), "flags": dict(flags),
              "time_labels": dict(sorted(labels.items())), "source_roundtrip_samples": samples,
              "primary_key_unique_all_days": all(d["primary_key_unique"] for r in receipts for d in r["days"]),
              "all_partitions_output_roundtrip_verified": True,
              "source_days_complete_against_audit_selection": total_days == len(provider.catalog["selection"]["dates"]),
              "max_process_peak_rss_bytes": max(r["process_peak_rss_bytes"] for r in receipts),
              "max_csv_batch_rows": max(d["max_batch_rows"] for r in receipts for d in r["days"]),
              "independent_pilot": independent, "formal_admission": False, "no_live_money": True}
    write_json(output / "acceptance.json", result)
    with (output / "inventory.csv").open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(inventory[0]))
        writer.writeheader()
        writer.writerows(inventory)
    report = ("# 余哈哈分钟数据转换验收\n\n"
              "NO LIVE MONEY。Parquet工程转换不构成金融数据认证。\n\n"
              f"观察区间：{result['first_date']}—{result['last_date']}；{total_days}个日期；"
              f"{result['rows']:,}行；{len(inventory)}个月分区。\n\n"
              "每月文件已进行Schema、记录数、逐行逻辑指纹和原始随机样本回读；"
              "源内容SHA及逐日审计计数匹配，异常值未修复或删除。\n\n"
              "完整统计与资格限制见acceptance.json；逐文件路径、大小与SHA见inventory.csv。\n\n"
              f"独立小样本复读：{independent['status']}。分钟区间、时区语义、单位、历史身份、"
              "复权及公司行为、PIT、状态和换手率尚未获得正式资格；日线聚合禁用。\n")
    (output / "report.md").write_text(report, encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--audit", type=Path)
    args = parser.parse_args()
    print(json.dumps(accept(args.dataset, args.output, source=args.source, audit=args.audit), ensure_ascii=False))


if __name__ == "__main__":
    main()
