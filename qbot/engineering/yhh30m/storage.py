"""Immutable monthly generations, atomic catalog and recoverable OS locks."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

import pyarrow as pa
import pyarrow.parquet as pq

from qbot.research.forward.integrity import assert_no_link_components
from .contract import CONTRACT, SCHEMA, IntradayDataError, json_bytes
from .normalize import logical_update, portable
from .source import file_sha


def implementation_sha():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value, replace=False):
    path = assert_no_link_components(path)
    temp = path.with_name(".tmp-" + path.name + "-" + uuid.uuid4().hex)
    with temp.open("xb") as handle:
        handle.write(json_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())
    if not replace and path.exists():
        raise IntradayDataError("REFUSE_OVERWRITE: " + str(path))
    os.replace(str(temp), str(path))


@contextmanager
def writer_lock(root):
    """OS releases this advisory lock after normal exit or a killed process."""
    lock = assert_no_link_components(root / ".writer.lock")
    with lock.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise IntradayDataError("CONCURRENT_WRITER") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def prepare_root(root, source, audit):
    root = assert_no_link_components(root)
    for other in (source, audit):
        other = assert_no_link_components(other)
        if root == other or root in other.parents or other in root.parents:
            raise IntradayDataError("SOURCE_AUDIT_OUTPUT_OVERLAP")
    if root.exists() and not (root / "contract.json").exists() and any(root.iterdir()):
        raise IntradayDataError("REFUSE_NONEMPTY_UNMARKED_OUTPUT")
    root.mkdir(parents=True, exist_ok=True)
    with writer_lock(root):
        marker = assert_no_link_components(root / "contract.json")
        if marker.exists():
            if marker.read_bytes() != json_bytes(CONTRACT):
                raise IntradayDataError("OUTPUT_CONTRACT_MISMATCH")
        else:
            write_json(marker, CONTRACT)
    return root


def verify_partition(folder, receipt=None):
    """Read output once: schema, row count, logical digest and source samples."""
    folder = assert_no_link_components(folder)
    if receipt is None:
        receipt = json.loads(assert_no_link_components(folder / "receipt.json").read_text(encoding="utf-8"))
    if receipt["contract"] != json.loads(json_bytes(CONTRACT)):
        raise IntradayDataError("PARTITION_CONTRACT_MISMATCH")
    path = folder / "bars.parquet"
    if file_sha(path) != receipt["parquet_sha256"]:
        raise IntradayDataError("PARQUET_HASH_MISMATCH")
    parquet = pq.ParquetFile(path)
    if not parquet.schema_arrow.equals(SCHEMA, check_metadata=True):
        raise IntradayDataError("PARQUET_SCHEMA_MISMATCH")
    digest, rows = hashlib.sha256(), 0
    samples = {(day["source_id"], s["source_row"]): s["normalized"]
               for day in receipt["days"] for s in day["samples"]}
    found = set()
    counts = {}
    for batch in parquet.iter_batches(batch_size=65536):
        table = pa.Table.from_batches([batch], schema=SCHEMA)
        logical_update(digest, table)
        ids, offsets = batch.column("source_id").to_pylist(), batch.column("source_row").to_pylist()
        for index, key in enumerate(zip(ids, offsets)):
            counts[key[0]] = counts.get(key[0], 0) + 1
            if key in samples:
                if portable(table.slice(index, 1).to_pylist()[0]) != samples[key]:
                    raise IntradayDataError("SOURCE_SAMPLE_ROUNDTRIP_MISMATCH")
                found.add(key)
        rows += len(batch)
    if (rows != receipt["rows"] or digest.hexdigest() != receipt["logical_sha256"] or
            found != set(samples) or counts != {d["source_id"]: d["rows"] for d in receipt["days"]}):
        raise IntradayDataError("PARQUET_CONTENT_RECONCILIATION_FAILED")
    return receipt


def peak_rss_bytes():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                              "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                              "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        ctypes.windll.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if ctypes.windll.psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.PeakWorkingSetSize
        return None
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
