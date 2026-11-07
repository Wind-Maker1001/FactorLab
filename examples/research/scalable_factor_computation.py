"""Reproducible P2-09 capacity workload, externally supervised RSS/disk caps."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import tracemalloc

import numpy as np
import pandas as pd

from examples.research.dataset_registry_v2_demo import synthetic_inputs
from factorlab.dataset import DatasetBuilder, DatasetBuildOptions
from factorlab.dataset.identity import file_hash, frame_hash
from factorlab.dataset.partitioned import ChunkedConfig, directory_bytes, _require_psutil
from factorlab.exploratory.factor_discovery.contracts import Node
from factorlab.exploratory.factor_discovery.engine import compile_expression
from factorlab.factors.registry_v2 import FactorDefinition, FactorRegistryV2, FactorDataContext
from factorlab.validation import ExpandingWalkForwardSplitter, WalkForwardRunner


LEVELS = {"L0": (50, 200, 14), "L1": (500, 1200, 14), "L2": (500, 1200, 100),
          "L3": (500, 2500, 100), "L4": (500, 2500, 300)}


def registry(count):
    result = FactorRegistryV2.with_builtins()
    refs = [d.ref for d in result.freeze().definitions]
    # Fixed, label-independent economic templates. No duplicated columns or
    # constant rescalings used to reach the target count. Distinct definitions
    # remain candidates, not claims of independent economic information.
    definitions = []
    for field in ("adj_close", "volume", "amount", "turnover", "RET1"):
        leaf = Node("Field", value=field)
        for window in (5, 10, 20, 60):
            for lag in (1, 5, 10, 20, 60):
                for op in ("RollingMean", "RollingStd", "Delta"):
                    numerator = Node(op, (leaf,), param=window)
                    denominator = Node("Lag", (Node("RollingMean", (leaf,), param=window),), param=lag)
                    node = Node("SafeDiv", (numerator, denominator))
                    definition = FactorDefinition.from_expression(
                        f"SCALE_{len(definitions):03d}", "1", compile_expression(node))
                    definitions.append(definition)
    definitions = definitions[:max(0, count - len(refs))]
    result.register_many(definitions)
    refs.extend(d.ref for d in definitions)
    assert len(refs) == count and len({r.definition_hash for r in refs}) == count
    return result.freeze(), tuple(refs)


def configured(symbols, count):
    snapshot, refs = registry(count)
    return DatasetBuilder(
        DatasetBuildOptions(expected_member_count=symbols, minimum_cross_section=min(20, symbols)),
        snapshot=snapshot, factor_refs=refs, data_context=FactorDataContext("P209_SYNTHETIC", "UNVERIFIED"),
        data_version="seed-20261014-fixed-v1", calendar_id="SYNTHETIC_BDAY_NOT_EXCHANGE-v1")


def model_proof(dataset, builder, dates):
    models = {}
    names = builder.factor_names
    for model in ("factor_score", "ridge", "lightgbm", "xgboost"):
        options = {"factor_score": {"factor_weights": {n: 1 / len(names) for n in names}},
                   "ridge": {"ridge_alphas": (0.1, 1.0)},
                   "lightgbm": {"lightgbm_candidates": [{"n_estimators": 5, "num_leaves": 7, "min_child_samples": 10}]},
                   "xgboost": {"xgboost_candidates": [{"n_estimators": 5, "max_depth": 2}]}}[model]
        runner = WalkForwardRunner(
            ExpandingWalkForwardSplitter(initial_train_months=1, validation_months=1,
                                         test_months=1, step_months=1, purge_days=5, embargo_days=5),
            model_name=model, feature_names=names, top_k=5, minimum_cross_section=20, **options)
        result = runner.run(dataset, calendar=dates)
        assert len(result.predictions) and len(result.fold_summary)
        assert (result.fold_summary.train_max_exit_date < result.fold_summary.audit_validation_first_date).all()
        assert (result.fold_summary.validation_max_exit_date < result.fold_summary.audit_test_first_date).all()
        models[model] = {"folds": len(result.fold_summary), "prediction_rows": len(result.predictions),
                         "prediction_hash": frame_hash(result.predictions), "purge_maturity_checked": True}
    return models


def worker(args):
    psutil = _require_psutil()
    symbols, sessions, count = LEVELS[args.level]
    root = Path(args.output)
    preflight = {"available_memory_bytes": psutil.virtual_memory().available,
                 "free_disk_bytes": shutil.disk_usage(root).free,
                 "output_directory": str(root.resolve()),
                 "dataset_destination_existed": (root / "dataset").exists()}
    generated = time.perf_counter()
    dates, *frames = synthetic_inputs(sessions=sessions, symbols=symbols)
    generation_seconds = time.perf_counter() - generated
    builder = configured(symbols, count)
    cfg = ChunkedConfig(workers=args.workers, memory_limit_bytes=int(args.memory_gib * 1024**3),
                        disk_limit_bytes=int(args.disk_gib * 1024**3), timeout_seconds=args.timeout)
    tracemalloc.start()
    cpu, started = time.process_time(), time.perf_counter()
    io_before = psutil.Process().io_counters()
    dataset = builder.build_chunked(*frames, dates, output_dir=root / "dataset", config=cfg)
    # Includes a verified read through the public builder input path.
    loaded = builder.read_partitioned(root / "dataset", calendar=dates,
                                      expected_identity_hash=dataset.identity_hash)
    loaded.verify()
    seconds, cpu_seconds = time.perf_counter() - started, time.process_time() - cpu
    _, python_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    io_after = psutil.Process().io_counters()
    report = {"level": args.level, "status": "PASS", "synthetic": True,
              "warning": "NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED",
              "sls_status": "SLS_PERFORMANCE_NOT_VERIFIED", "python": platform.python_version(),
              "preflight": preflight,
              "input_generation_seconds": generation_seconds, "input_stock_days": len(frames[0]),
              "dense_calendar_stock_days": symbols * sessions,
              "end_to_end_seconds": seconds, "cpu_seconds": cpu_seconds,
              "python_tracemalloc_peak_bytes": python_peak,
              "read_bytes": io_after.read_bytes - io_before.read_bytes,
              "write_bytes": io_after.write_bytes - io_before.write_bytes,
              "config": asdict(cfg), "dataset_identity_hash": dataset.identity_hash,
              "manifest_sha256": file_hash(root / "dataset" / "manifest.json"),
              "partition_count": len(dataset.manifest["partitions"]),
              "counts": dataset.manifest["counts"], "dataset_rows": dataset.manifest["rows"],
              "valid_factor_values_per_second": dataset.manifest["counts"]["valid_factor_values"] / seconds,
              "failed_batches": 0, "retries": 0}
    report["native_thread_environment"] = {n: os.environ.get(n) for n in
        ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")}
    if args.level == "L0":
        old = builder.build(*frames, dates)
        new = loaded.to_frame()
        pd.testing.assert_frame_equal(old, new, check_exact=True)
        assert old.attrs == new.attrs, "Dense and partitioned V2 content identities differ"
        report["dense_equivalence"] = "EXACT"
        report["models"] = model_proof(new, builder, new.date.drop_duplicates())
    (root / "metrics.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(root / "dataset" / "manifest.json", root / "partition-manifest.json")


def supervise(args):
    psutil = _require_psutil()
    root = Path(args.output).resolve()
    if root.exists():
        raise FileExistsError(root)
    root.mkdir(parents=True)
    command = [sys.executable, "-m", "examples.research.scalable_factor_computation",
               "--worker", "--level", args.level, "--output", str(root), "--workers", str(args.workers),
               "--memory-gib", str(args.memory_gib), "--disk-gib", str(args.disk_gib), "--timeout", str(args.timeout)]
    started = time.perf_counter()
    peak_rss = peak_disk = peak_private = 0
    stop_reason = None
    with (root / "worker.log").open("w", encoding="utf-8") as log:
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        process = psutil.Process(child.pid)
        while child.poll() is None:
            try:
                infos = [p.memory_info() for p in [process] + process.children(recursive=True)]
                peak_rss = max(peak_rss, sum(m.rss for m in infos))
                peak_private = max(peak_private, sum(getattr(m, "private", m.vms) for m in infos))
                peak_disk = max(peak_disk, directory_bytes(root))
                reason = ("MEMORY_LIMIT" if peak_rss > args.memory_gib * 1024**3 else
                          "DISK_LIMIT" if peak_disk > args.disk_gib * 1024**3 else
                          "TIMEOUT" if time.perf_counter() - started > args.timeout + 120 else None)
                if reason:
                    stop_reason = reason
                    for p in process.children(recursive=True):
                        p.kill()
                    process.kill()
                    break
            except psutil.NoSuchProcess:
                pass
            time.sleep(0.1)
        code = child.wait()
    monitor = {"exit_code": code, "stop_reason": stop_reason, "peak_process_tree_rss_bytes": peak_rss,
               "peak_private_commit_bytes": peak_private, "peak_disk_bytes": peak_disk,
               "supervisor_seconds": time.perf_counter() - started,
               "sample_interval_seconds": 0.1, "command": command,
               "machine": {"platform": platform.platform(), "cpu": platform.processor(),
                           "logical_cpus": psutil.cpu_count(), "physical_cpus": psutil.cpu_count(logical=False),
                           "total_memory_bytes": psutil.virtual_memory().total},
               "memory_note": "External process-tree RSS samples; short transients may be missed. Private commit also recorded."}
    (root / "monitor.json").write_text(json.dumps(monitor, indent=2) + "\n", encoding="utf-8")
    if stop_reason:
        # Scoped to only this supervisor's newly created output directory.
        for path in root.glob("dataset.pending-*"):
            if path.resolve().parent == root:
                shutil.rmtree(path)
    if code:
        raise SystemExit(code)
    print(json.dumps({**monitor, "metrics": json.loads((root / "metrics.json").read_text())}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--level", choices=LEVELS, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--memory-gib", type=float, default=5)
    parser.add_argument("--disk-gib", type=float, default=8)
    parser.add_argument("--timeout", type=float, default=14400)
    parser.add_argument("--worker", action="store_true")
    options = parser.parse_args()
    (worker if options.worker else supervise)(options)
