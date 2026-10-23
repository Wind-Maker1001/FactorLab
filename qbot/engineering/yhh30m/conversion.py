"""Audited vendor CSV/ZIP to partitioned engineering-only Parquet."""

from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import platform
import time
import uuid

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from qbot.research.forward.integrity import assert_no_link_components

from .contract import CONTRACT, SCHEMA, IntradayDataError, json_bytes
from .normalize import convert_source, logical_update
from .source import file_sha, load_inventory, signature, verify_containers
from .storage import (implementation_sha, now, peak_rss_bytes, prepare_root,
                      verify_partition, write_json, writer_lock)


def convert(source, audit, output, start=None, end=None, block_size=1024 * 1024, progress=None):
    source, audit = Path(source).absolute(), Path(audit).absolute()
    if block_size < 1024:
        raise IntradayDataError("CSV_BLOCK_SIZE_MUST_BE_AT_LEAST_1024_BYTES")
    for bound in (start, end):
        if bound is not None and pd.Timestamp(bound).strftime("%Y-%m-%d") != bound:
            raise IntradayDataError("DATE_BOUND_MUST_BE_ISO_DATE")
    inventory, containers, evidence = load_inventory(source, audit)
    chosen = [d for d in inventory if (start is None or d["date"] >= start) and (end is None or d["date"] <= end)]
    if not chosen:
        raise IntradayDataError("EMPTY_DATE_SELECTION")
    root = prepare_root(Path(output), source, audit)
    started, tick = now(), time.perf_counter()
    implementation = implementation_sha()
    selection = {"implementation_sha256": implementation, "audit_evidence": evidence,
                 "dates": [d["date"] for d in chosen], "contract": CONTRACT}
    selection_id = hashlib.sha256(json_bytes(selection)).hexdigest()
    grouped = defaultdict(list)
    for item in chosen:
        grouped[item["date"][:7]].append(item)
    with writer_lock(root):
        request_file = assert_no_link_components(root / "selection.json")
        if request_file.exists():
            if request_file.read_bytes() != json_bytes(selection):
                raise IntradayDataError("OUTPUT_SELECTION_OR_IMPLEMENTATION_CHANGED_USE_NEW_ROOT")
        else:
            write_json(request_file, selection)
        # A byte hash is not a second financial audit. Each source member is hashed
        # again in the necessary conversion read, against the reused prior audit.
        snapshots = verify_containers(source, containers)
        if progress:
            progress({"event": "preflight_verified", "containers": len(snapshots), "days": len(chosen)})
        published = []
        run_id = uuid.uuid4().hex
        try:
            for month, items in sorted(grouped.items()):
                identity = hashlib.sha256(json_bytes({"selection_id": selection_id, "month": month})).hexdigest()
                parent = assert_no_link_components(root / "partitions" / ("year=" + month[:4]) / ("month=" + month[5:]))
                parent.mkdir(parents=True, exist_ok=True)
                folder = assert_no_link_components(parent / identity)
                status = "REUSED_VERIFIED"
                if folder.exists():
                    receipt = verify_partition(folder)
                    if (receipt["identity"] != identity or receipt["implementation_sha256"] != implementation or
                            receipt["audit_evidence"] != evidence or receipt["month"] != month or
                            [(d["date"], d["source_id"], d["rows"], d["container"], d["locator"]) for d in receipt["days"]] !=
                            [(d["date"], d["content_sha256"], int(d["rows"]), d["container"], d["locator"]) for d in items]):
                        raise IntradayDataError("EXISTING_PARTITION_IDENTITY_MISMATCH")
                else:
                    temporary = parent / (".incomplete-" + uuid.uuid4().hex)
                    temporary.mkdir()
                    path = temporary / "bars.parquet"
                    days, digest = [], hashlib.sha256()
                    with path.open("xb") as handle:
                        with pq.ParquetWriter(handle, SCHEMA, compression="zstd", use_dictionary=True,
                                              write_statistics=True) as writer:
                            def consume(table):
                                logical_update(digest, table)
                                writer.write_table(table, row_group_size=65536)
                            for item in items:
                                days.append(convert_source(source, item, block_size, consume))
                        handle.flush()
                        os.fsync(handle.fileno())
                    receipt = {"schema": CONTRACT["schema"], "identity": identity, "contract": CONTRACT,
                               "implementation_sha256": implementation, "audit_evidence": evidence,
                               "month": month, "days": days, "rows": sum(d["rows"] for d in days),
                               "parquet_sha256": file_sha(path), "parquet_bytes": path.stat().st_size,
                               "logical_sha256": digest.hexdigest(),
                               "logical_hash_method": "PANDAS_ROW_HASH64_THEN_SHA256_INDEX_FALSE_CATEGORIZE_FALSE",
                               "csv_block_bytes": block_size, "created_at_utc": now(),
                               "versions": {"python": platform.python_version(), "pyarrow": pa.__version__,
                                            "pandas": pd.__version__, "numpy": np.__version__},
                               "process_peak_rss_bytes": peak_rss_bytes(),
                               "arrow_peak_bytes": pa.default_memory_pool().max_memory(),
                               "output_roundtrip_verified": True}
                    verify_partition(temporary, receipt)
                    write_json(temporary / "receipt.json", receipt)
                    os.rename(str(temporary), str(folder))
                    status = "CONVERTED"
                published.append({"month": month, "identity": identity,
                                  "receipt": (folder / "receipt.json").relative_to(root).as_posix(),
                                  "receipt_sha256": file_sha(folder / "receipt.json"),
                                  "parquet": (folder / "bars.parquet").relative_to(root).as_posix(),
                                  "parquet_sha256": receipt["parquet_sha256"],
                                  "parquet_bytes": receipt["parquet_bytes"], "rows": receipt["rows"],
                                  "dates": [d["date"] for d in receipt["days"]]})
                write_json(root / "dataset.json", {"schema": CONTRACT["schema"], "contract": CONTRACT,
                           "selection_id": selection_id, "selection": selection,
                           "complete": False, "partitions": published,
                           "rows": sum(p["rows"] for p in published)}, replace=True)
                if progress:
                    progress({"event": "partition", "month": month, "status": status, "rows": receipt["rows"]})
            for name, snapshot in snapshots.items():
                if signature(source / name) != snapshot["stat"]:
                    raise IntradayDataError("SOURCE_CHANGED_DURING_RUN: " + name)
            write_json(root / "dataset.json", {"schema": CONTRACT["schema"], "contract": CONTRACT,
                       "selection_id": selection_id, "selection": selection, "complete": True,
                       "partitions": published, "rows": sum(p["rows"] for p in published)}, replace=True)
            result = {"schema": "FACTORLAB_YHH_CONVERSION_RUN_V1", "status": "COMPLETE",
                      "no_live_money": True, "formal_admission": False,
                      "run_id": run_id, "started_at_utc": started, "finished_at_utc": now(),
                      "source": str(source), "output": str(root), "selection_id": selection_id,
                      "implementation_sha256": implementation, "audit_evidence": evidence,
                      "source_containers": snapshots, "partitions": len(published), "days": len(chosen),
                      "rows": sum(p["rows"] for p in published), "expected_rows": sum(int(d["rows"]) for d in chosen),
                      "parquet_bytes": sum(p["parquet_bytes"] for p in published),
                      "dataset_sha256": file_sha(root / "dataset.json"),
                      "process_peak_rss_bytes": peak_rss_bytes(), "elapsed_seconds": time.perf_counter() - tick}
            runs = root / "runs"
            runs.mkdir(exist_ok=True)
            write_json(runs / (run_id + ".json"), result)
            return result
        except Exception as exc:
            failures = root / "failures"
            failures.mkdir(exist_ok=True)
            write_json(failures / (run_id + ".json"), {"status": "FAILED", "error": str(exc),
                       "selection_id": selection_id, "completed_months": [p["month"] for p in published],
                       "failed_at_utc": now(), "formal_admission": False})
            raise
