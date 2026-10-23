"""Paired offline benchmarks against the frozen factor Golden reference.

SYNTHETIC SOFTWARE DIAGNOSTIC ONLY. No Provider, PIT publication, model fit,
Forward activation or live order is involved. Every worker has its own process
and exclusive artifacts. Profile runs belong to scale_factor_pipeline instead.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import statistics
import subprocess
import sys
from time import perf_counter

import numpy as np
import pandas as pd

from examples.research.factor_performance import PerformanceRecorder, environment_details
from factorlab.config import load_config
from factorlab.metrics import factor as optimized
from factorlab.reports.generator import write_factor_report
from factorlab.reproducibility import RunManifest
from tests.research.frozen_reference import reference
from tests.research.reference.compare import TABLES, ATOL, RTOL, assert_evaluation_equal


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sources():
    from factorlab.metrics import _factor_arrays
    return {"reference": sha256_file(reference.__file__), "optimized": sha256_file(optimized.__file__),
            "array_kernels": sha256_file(_factor_arrays.__file__), "benchmark": sha256_file(__file__),
            "measurement": sha256_file(Path(__file__).with_name("factor_performance.py"))}


def check_reference():
    meta = json.loads(Path(reference.__file__).with_suffix(".json").read_text(encoding="utf-8"))
    if sources()["reference"] != meta["source_sha256"]:
        raise ValueError("Frozen Golden reference checksum mismatch")


def input_bundle(manifest_path):
    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("status") != "success" or manifest.get("synthetic_only") is not True:
        raise ValueError("Only successful explicitly synthetic source runs may be replayed")
    artifact = manifest["output_artifacts"]["synthetic-dataset.parquet"]
    content = Path(artifact["path"]).read_bytes()
    if hashlib.sha256(content).hexdigest() != artifact["sha256"]:
        raise ValueError("Synthetic dataset content hash mismatch")
    data = pd.read_parquet(io.BytesIO(content))
    dates = pd.DatetimeIndex(data["date"].drop_duplicates().sort_values())
    return manifest, data, dates, artifact["sha256"]


def worker(args):
    output = Path(args.worker_output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    payload = {"status": "running", "synthetic_only": True, "research_conclusions_permitted": False,
               "no_live_money": True, "implementation": args.implementation, "scope": args.scope,
               "started_at": datetime.now(timezone.utc).isoformat(), "sources_sha256": sources()}
    started = perf_counter()
    try:
        check_reference()
        with PerformanceRecorder(measure_memory=True) as recorder:
            with recorder.phase("read_and_verify_input"):
                manifest, data, dates, digest = input_bundle(args.source_manifest)
            factors = manifest["factor_list"]
            config = manifest["config"]
            target = "EXCESS_RET%d" % config["label"]["holding_days"]
            if args.input_case == "sparse":
                rng = np.random.default_rng(42)
                keep = rng.random(len(data)) >= 0.02
                data = data.loc[keep & ~data.date.eq(dates[len(dates) // 2])].copy()
                values = data[factors].to_numpy(copy=True)
                values[rng.random(values.shape) < 0.05] = np.nan
                data.loc[:, factors] = values
                data.loc[rng.random(len(data)) < 0.03, target] = np.nan
            options = dict(target=target, quantiles=config["evaluation"]["quantiles"],
                           minimum_cross_section=config["evaluation"]["min_cross_section"],
                           group_column="MARKET_REGIME" if args.scope == "regimes" else None, calendar=dates)
            module = reference if args.implementation == "reference" else optimized
            warmup = 0.0
            if args.worker_warmup_days:
                warm_dates = dates[:args.worker_warmup_days]
                warm = data.loc[data.date.isin(warm_dates)]
                warm_options = dict(options, calendar=warm_dates)
                stage = perf_counter()
                with recorder.phase("explicit_kernel_warmup_excluded"):
                    module.evaluate_factors(warm, factors, **warm_options)
                warmup = perf_counter() - stage
                del warm
            with recorder.phase("factor_evaluation"), recorder.instrument_evaluator(module):
                stage = perf_counter()
                evaluation = module.evaluate_factors(data, factors, **options)
                wall = perf_counter() - stage
            # Snapshot BEFORE report and output writes; lifetime peak also
            # includes imports, loading and the explicitly documented warmup.
            evaluation_memory = recorder.payload()["process_memory"]
            with recorder.phase("report_generation"), recorder.instrument_report():
                write_factor_report(evaluation, output / "factor-report")
            with recorder.phase("write_evaluation_tables"):
                for name in TABLES:
                    getattr(evaluation, name).to_parquet(output / (name + ".parquet"), index=name == "correlation")
            with recorder.phase("output_hashing"):
                artifacts = {str(p.relative_to(output)): sha256_file(p) for p in sorted(output.rglob("*")) if p.is_file()}
            payload.update({"status": "success", "source_run_id": manifest["run_id"],
                            "source_manifest_sha256": sha256_file(args.source_manifest), "source_dataset_sha256": digest,
                            "input_case": args.input_case, "rows": len(data), "calendar_dates": len(dates),
                            "warmup_days": args.worker_warmup_days, "warmup_seconds_excluded": warmup,
                            "factor_evaluation_seconds": wall, "evaluation_memory": evaluation_memory,
                            "performance_details": recorder.payload(), "environment_details": environment_details(),
                            "artifacts_sha256": artifacts, "sources_sha256_at_end": sources(),
                            "elapsed_before_worker_receipt_seconds": perf_counter() - started})
        if payload["sources_sha256_at_end"] != payload["sources_sha256"]:
            raise ValueError("Evaluator source changed during measurement")
    except Exception as exc:
        payload.update(status="failed", error="%s: %s" % (type(exc).__name__, exc))
    (output / "worker.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0 if payload["status"] == "success" else 1


def load_result(directory):
    directory = Path(directory)
    payload = json.loads((directory / "worker.json").read_text(encoding="utf-8"))
    if payload["status"] != "success":
        raise ValueError("Worker failed: %s" % payload.get("error"))
    for relative, digest in payload["artifacts_sha256"].items():
        if sha256_file(directory / relative) != digest:
            raise ValueError("Worker artifact hash mismatch: %s" % relative)
    tables = optimized.FactorEvaluation(**{name: pd.read_parquet(directory / (name + ".parquet")) for name in TABLES})
    return payload, tables


def run_benchmark(args):
    config = load_config(args.config)
    run = RunManifest(config, "factor-performance-benchmark", vars(args))
    output = config.paths.results_root / "diagnostics" / "factor-performance-benchmark" / run.run_id
    run.payload.update(synthetic_only=True, diagnostic_only=True, research_conclusions_permitted=False,
                       no_live_money=True, network_requests=0, point_in_time_universe_verified=False,
                       forward_evidence=False)
    started = perf_counter()
    try:
        if not 1 <= args.repeats <= 20 or not 1 <= args.warmup_days <= 100:
            raise ValueError("repeats must be 1..20 and warmup-days 1..100")
        check_reference()
        output.mkdir(parents=True, exist_ok=False)
        original_sources = sources()
        records = []
        summaries = {}
        for scope in args.scopes:
            for kind, count in (("first", 1), ("repeat", args.repeats)):
                for iteration in range(count):
                    pair = {}
                    order = ("reference", "optimized") if (iteration + (kind == "repeat")) % 2 == 0 else ("optimized", "reference")
                    for implementation in order:
                        if sources() != original_sources:
                            raise ValueError("Evaluator sources changed during paired benchmark")
                        directory = output / ("%s-%s-%02d-%s" % (scope, kind, iteration, implementation))
                        command = [sys.executable, "-B", "-X", "utf8", "-m", "examples.research.benchmark_factor_evaluation",
                                   "--worker-output", str(directory), "--source-manifest", str(Path(args.source_manifest).resolve()),
                                   "--implementation", implementation, "--scope", scope, "--input-case", args.input_case,
                                   "--worker-warmup-days", str(0 if kind == "first" else args.warmup_days)]
                        result = subprocess.run(command, cwd=str(config.repo_root), capture_output=True, text=True, encoding="utf-8")
                        if result.returncode:
                            raise RuntimeError("%s %s worker failed: %s" % (scope, implementation, result.stderr[-2000:]))
                        payload, tables = load_result(directory)
                        if payload["sources_sha256"] != original_sources:
                            raise ValueError("Worker source identity differs from the paired benchmark")
                        payload.update(run_kind=kind, iteration=iteration, worker_directory=str(directory))
                        records.append(payload)
                        pair[implementation] = tables
                        print(json.dumps({"scope": scope, "kind": kind, "iteration": iteration,
                                          "implementation": implementation, "seconds": payload["factor_evaluation_seconds"]}), flush=True)
                    assert_evaluation_equal(pair["optimized"], pair["reference"])
                    del pair
            selected = [r for r in records if r["scope"] == scope and r["run_kind"] == "repeat"]
            summary = {}
            for implementation in ("reference", "optimized"):
                values = [r["factor_evaluation_seconds"] for r in selected if r["implementation"] == implementation]
                peaks = [r["evaluation_memory"].get("at_recording_end", {}).get("process_lifetime_peak_resident_bytes")
                         for r in selected if r["implementation"] == implementation]
                summary[implementation] = {"median_seconds": statistics.median(values), "min_seconds": min(values),
                                           "max_seconds": max(values), "repeats": len(values),
                                           "median_process_lifetime_peak_resident_bytes": statistics.median(peaks) if all(p is not None for p in peaks) else None}
            summary["reduction_fraction"] = 1 - summary["optimized"]["median_seconds"] / summary["reference"]["median_seconds"]
            summary["golden_all_pairs_passed"] = True
            summaries[scope] = summary
        summary_path = output / "comparison.json"
        summary_path.write_text(json.dumps({"synthetic_only": True, "research_conclusions_permitted": False,
            "no_live_money": True,
            "atol": ATOL, "rtol": RTOL, "first_runs_excluded_from_repeat_medians": True,
            "warmup_protocol": "fresh process for every sample; explicit leading-date kernel warmup before repeats; all per-call caches rebuilt; OS file cache uncontrolled",
            "summaries": summaries, "workers": records,
            "elapsed_before_parent_receipt_seconds": perf_counter() - started}, indent=2) + "\n", encoding="utf-8")
        run.add_output("comparison", summary_path)
        run.payload.update(comparison_sha256=sha256_file(summary_path), sources_sha256=original_sources,
                           summaries=summaries, elapsed_before_receipt_seconds=perf_counter() - started)
        run.finish("success")
        print("Comparison: %s" % summary_path, flush=True)
        return 0
    except Exception as exc:
        run.finish("failed", "%s: %s" % (type(exc).__name__, exc))
        print("Benchmark failed: %s: %s; receipt: %s" % (type(exc).__name__, exc, run.path), flush=True)
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/research/csi500_5y.yaml")
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmup-days", type=int, default=20)
    parser.add_argument("--scopes", nargs="+", choices=("all", "regimes"), default=["all", "regimes"])
    parser.add_argument("--input-case", choices=("dense", "sparse"), default="dense")
    parser.add_argument("--worker-output", help=argparse.SUPPRESS)
    parser.add_argument("--implementation", choices=("reference", "optimized"), help=argparse.SUPPRESS)
    parser.add_argument("--scope", choices=("all", "regimes"), help=argparse.SUPPRESS)
    parser.add_argument("--worker-warmup-days", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    return worker(args) if args.worker_output else run_benchmark(args)


if __name__ == "__main__":
    raise SystemExit(main())
