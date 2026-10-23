"""Shared reader/CLI binding checks; historical producer hashes remain intact."""

from datetime import date
import hashlib
import json
from pathlib import Path
import re

import pyarrow.parquet as pq

from factorlab.forward.integrity import assert_no_link_components
from .contract import CONTRACT, SCHEMA, IntradayDataError, json_bytes
from .source import file_sha


def require(condition, message):
    if not condition:
        raise IntradayDataError(message)


def positive(value):
    return type(value) is int and value > 0


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def iso_day(value):
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def artifact(root, relative):
    require(isinstance(relative, str), "INVALID_ARTIFACT_PATH")
    path = Path(relative)
    require(not path.is_absolute() and ".." not in path.parts, "UNSAFE_DATASET_PATH")
    return assert_no_link_components(root / path)


def load_catalog(root):
    catalog = json.loads(assert_no_link_components(root / "dataset.json").read_text(encoding="utf-8"))
    require(catalog.get("complete") is True, "INVALID_OR_INCOMPLETE_INTRADAY_DATASET")
    require(catalog.get("schema") == CONTRACT["schema"] and catalog.get("contract") == CONTRACT,
            "INTRADAY_CATALOG_CONTRACT_MISMATCH")
    selection = catalog.get("selection", {})
    require(selection.get("contract") == CONTRACT and digest(selection.get("implementation_sha256")),
            "INTRADAY_SELECTION_CONTRACT_MISMATCH")
    evidence = selection.get("audit_evidence", {})
    require(isinstance(evidence, dict) and evidence and all(digest(v) for v in evidence.values()),
            "INTRADAY_SELECTION_AUDIT_BINDING_MISMATCH")
    dates = selection.get("dates", [])
    require(isinstance(dates, list) and dates and all(iso_day(d) for d in dates), "INVALID_SELECTION_DATES")
    require(dates == sorted(set(dates)), "INVALID_SELECTION_DATE_ORDER")
    require(hashlib.sha256(json_bytes(selection)).hexdigest() == catalog.get("selection_id"),
            "INTRADAY_SELECTION_ID_MISMATCH")
    for name, expected in (("contract.json", CONTRACT), ("selection.json", selection)):
        value = json.loads(assert_no_link_components(root / name).read_text(encoding="utf-8"))
        require(value == expected, "INTRADAY_ROOT_BINDING_MISMATCH")
    partitions = catalog.get("partitions", [])
    require(isinstance(partitions, list) and partitions, "EMPTY_INTRADAY_CATALOG")
    observed, months, rows = [], [], 0
    for part in partitions:
        month, days = part.get("month"), part.get("dates", [])
        require(isinstance(month, str) and re.fullmatch(r"\d{4}-\d{2}", month), "INVALID_PARTITION_MONTH")
        require(isinstance(days, list) and days and all(iso_day(d) and d[:7] == month for d in days),
                "INVALID_PARTITION_DATES")
        require(days == sorted(set(days)) and positive(part.get("rows")) and positive(part.get("parquet_bytes")),
                "INVALID_PARTITION_COUNTS_OR_ORDER")
        expected = hashlib.sha256(json_bytes({"selection_id": catalog["selection_id"], "month": month})).hexdigest()
        require(part.get("identity") == expected, "INTRADAY_PARTITION_IDENTITY_MISMATCH")
        parent = "partitions/year=%s/month=%s/%s/" % (month[:4], month[5:], expected)
        require(part.get("parquet") == parent + "bars.parquet" and part.get("receipt") == parent + "receipt.json",
                "INTRADAY_PARTITION_PATH_MISMATCH")
        require(digest(part.get("parquet_sha256")) and digest(part.get("receipt_sha256")),
                "INVALID_PARTITION_DIGEST")
        observed.extend(days)
        months.append(month)
        rows += part["rows"]
    require(months == sorted(set(months)) and observed == dates, "INTRADAY_PARTITION_COVERAGE_MISMATCH")
    require(positive(catalog.get("rows")) and rows == catalog["rows"], "INTRADAY_CATALOG_COUNTS_MISMATCH")
    return catalog


def checked_partition(root, catalog, part):
    receipt_path, path = artifact(root, part["receipt"]), artifact(root, part["parquet"])
    require(file_sha(receipt_path) == part["receipt_sha256"] and file_sha(path) == part["parquet_sha256"],
            "INTRADAY_ARTIFACT_HASH_MISMATCH")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    selection = catalog["selection"]
    require(receipt.get("contract") == CONTRACT and receipt.get("schema") == CONTRACT["schema"] and
            receipt.get("audit_evidence") == selection["audit_evidence"] and
            receipt.get("implementation_sha256") == selection["implementation_sha256"],
            "INTRADAY_RECEIPT_SOURCE_BINDING_MISMATCH")
    require(receipt.get("identity") == part["identity"] and receipt.get("month") == part["month"] and
            receipt.get("rows") == part["rows"] and receipt.get("parquet_sha256") == part["parquet_sha256"] and
            receipt.get("parquet_bytes") == part["parquet_bytes"] == path.stat().st_size and
            receipt.get("output_roundtrip_verified") is True and digest(receipt.get("logical_sha256")),
            "INTRADAY_RECEIPT_MISMATCH")
    days = receipt.get("days", [])
    require(isinstance(days, list) and [d.get("date") for d in days] == part["dates"],
            "INTRADAY_RECEIPT_DATES_MISMATCH")
    ids, rows = set(), 0
    for day in days:
        require(positive(day.get("rows")) and digest(day.get("source_id")) and day["source_id"] not in ids,
                "INTRADAY_RECEIPT_DAILY_COUNTS_MISMATCH")
        ids.add(day["source_id"])
        rows += day["rows"]
        seen = set()
        samples = day.get("samples", [])
        require(isinstance(samples, list) and len(samples) == min(5, day["rows"]), "INTRADAY_SAMPLE_COUNT_MISMATCH")
        for sample in samples:
            row, normalized = sample.get("source_row"), sample.get("normalized", {})
            require(positive(row) and row <= day["rows"] and row not in seen and
                    normalized.get("source_id") == day["source_id"] and normalized.get("source_row") == row and
                    normalized.get("trade_date") == day["date"], "INTRADAY_SAMPLE_BINDING_MISMATCH")
            seen.add(row)
    require(rows == receipt["rows"], "INTRADAY_RECEIPT_DAILY_COUNTS_MISMATCH")
    parquet = pq.ParquetFile(path)
    require(parquet.metadata.num_rows == part["rows"] and parquet.schema_arrow.equals(SCHEMA, check_metadata=True),
            "INTRADAY_SCHEMA_OR_METADATA_COUNTS_MISMATCH")
    return receipt, parquet
