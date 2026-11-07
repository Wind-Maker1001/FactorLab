"""Reproducible 100-definition SOFTWARE-ONLY registration/batch benchmark.

Run from repository root with PYTHONPATH=src (or an editable installation).
Only the small JSON receipt is persisted; input/output frames are rebuildable.
"""

import argparse
import ctypes
from dataclasses import asdict
import hashlib
from itertools import product
import json
from pathlib import Path
import platform
import sys
import time
import tracemalloc

import numpy as np
import pandas as pd

from factorlab.exploratory.factor_discovery.contracts import Node
from factorlab.exploratory.factor_discovery.engine import compile_expression
from factorlab.exploratory.factor_discovery.synthetic import frame_sha256, make_synthetic_data
from factorlab.factors.registry_v2 import FactorDataContext, FactorDefinition, FactorRegistryV2


def approved_definitions():
    """100 distinct AST definitions approved by software validation only.

    The finite grid has deliberate economic redundancy. It is not a claim of
    100 independent signals or permission to use real financial observations.
    """
    definitions = []
    for index, (name, numerator, denominator) in enumerate(product(
            ("turnover", "volume", "adj_close", "raw_close"),
            (1, 5, 10, 20, 60), (1, 5, 10, 20, 60))):
        source = Node("Field", value=name)
        node = Node("SafeDiv", (Node("Lag", (source,), param=numerator),
                                Node("Lag", (source,), param=denominator)))
        definitions.append(FactorDefinition.from_expression(
            "synthetic_%03d" % index, "1", compile_expression(node)))
    assert len({d.definition_hash for d in definitions}) == 100
    return tuple(definitions)


def process_peak_rss_bytes():
    """OS process-lifetime peak resident memory, including imports and input."""
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters),
                                              wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters),
                                          counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(counters.PeakWorkingSetSize)
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def run_benchmark(stocks=20, sessions=160):
    if not 2 <= stocks <= 500 or not 61 <= sessions <= 500 or stocks * sessions > 250000:
        raise ValueError("Bounded synthetic panel with at least 61 sessions required")
    bars, _, calendar = make_synthetic_data(stock_count=stocks, sessions=sessions)
    tracemalloc.start()
    start = time.perf_counter()
    definitions = approved_definitions()
    registry = FactorRegistryV2()
    registry.register_many(definitions)
    snapshot = registry.freeze()
    registration_seconds = time.perf_counter() - start
    refs = tuple(d.ref for d in definitions)
    start = time.perf_counter()
    output = snapshot.compute(bars, refs, calendar=calendar,
                              context=FactorDataContext("SYNTHETIC_ONLY", "UNVERIFIED"))
    compute_seconds = time.perf_counter() - start
    _, allocation_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_rss = process_peak_rss_bytes()
    # Reordering input changes neither output keys nor exact factor values.
    repeated = snapshot.compute(bars.sample(frac=1, random_state=7), refs, calendar=calendar)
    pd.testing.assert_frame_equal(output, repeated, check_exact=True)
    finite_counts = {name: int(np.isfinite(output[name]).sum()) for name in output.columns[2:]}
    assert all(count > 0 for count in finite_counts.values())
    root = Path(__file__).resolve().parents[2]
    paths = sorted((root / "src/factorlab/factors").glob("*.py")) + [
        root / "src/factorlab/exploratory/factor_discovery" / name
        for name in ("contracts.py", "engine.py", "synthetic.py")]
    paths += [Path(__file__).resolve(), root / "tests/research/test_factor_registry_v2.py"]
    source_hashes = {
        path.relative_to(root).as_posix(): hashlib.sha256(
            path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for path in paths
    }
    return {
        "schema": "FACTOR_REGISTRY_V2_SYNTHETIC_BENCHMARK_V1",
        "baseline_commit": "967f9201ce349676b9ad3f2719c162176ffab9fd",
        "python": platform.python_version(), "platform": platform.platform(),
        "pandas": pd.__version__, "numpy": np.__version__,
        "stocks": stocks, "sessions": sessions, "input_rows": len(bars),
        "definitions_registered": len(definitions), "output_rows": len(output),
        "factor_columns": len(refs),
        "unique_definition_hashes": len({d.definition_hash for d in definitions}),
        "snapshot_hash": snapshot.snapshot_hash,
        "registration_and_freeze_seconds": registration_seconds,
        "batch_compute_seconds": compute_seconds,
        "timing_includes_tracemalloc_overhead": True,
        "traced_peak_allocation_bytes": allocation_peak,
        "traced_peak_scope": "definition construction + registration + freeze + compute; excludes input generation",
        "process_peak_rss_bytes": peak_rss,
        "rss_scope": "OS process lifetime through measured batch, including imports and synthetic inputs",
        "deterministic_reordered_input_exact": True,
        "input_hash": frame_sha256(bars), "output_hash": frame_sha256(output),
        "finite_values_per_factor": finite_counts,
        "software_approval": "ALL_100_DEFINITIONS_REVALIDATED_BY_EXISTING_AST_WHITELIST",
        "independent_economic_signals_claimed": False,
        "data_context": asdict(FactorDataContext("SYNTHETIC_ONLY", "UNVERIFIED")),
        "source_hash_policy": "SHA256_UTF8_BYTES_WITH_CRLF_NORMALIZED_TO_LF",
        "source_sha256": source_hashes,
        "reproduce": "$env:PYTHONPATH='src'; & 'D:/Environments/factorlab-py313/Scripts/python.exe' -B -m examples.research.factor_registry_v2_demo --output docs/validation/p2-05/performance.json",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stocks", type=int, default=20)
    parser.add_argument("--sessions", type=int, default=160)
    args = parser.parse_args(argv)
    receipt = run_benchmark(args.stocks, args.sessions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: receipt[key] for key in (
        "definitions_registered", "input_rows", "registration_and_freeze_seconds",
        "batch_compute_seconds", "traced_peak_allocation_bytes", "process_peak_rss_bytes",
        "deterministic_reordered_input_exact")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
