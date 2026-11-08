"""One-pass P3-03 A/B measurement; invoke each mode in a fresh process."""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import platform
import time

import pandas as pd

from examples.research.dataset_registry_v2_demo import synthetic_inputs
from examples.research.scalable_factor_computation import configured
from factorlab.dataset.identity import file_hash, frame_hash
from factorlab.dataset.partitioned import ChunkedConfig, PartitionedDataset, _require_psutil
from factorlab.validation import ExpandingWalkForwardSplitter, WalkForwardRunner
from factorlab.validation.partitioned_walk_forward import measure_rss


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "dense", "window"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    args.report.mkdir(parents=True, exist_ok=True)
    if args.mode == "prepare":
        dates, *inputs = synthetic_inputs(400, 80)
        builder = configured(80, 40)
        builder.options = replace(builder.options,
            filter_config=replace(builder.options.filter_config, min_listed_days=1))
        dataset = builder.build_chunked(*inputs, dates, output_dir=args.data,
            config=ChunkedConfig(dates_per_batch=20))
        (args.report / "data-receipt.json").write_text(json.dumps({
            "identity_hash": dataset.identity_hash, "rows": dataset.manifest["rows"],
            "partitions": len(dataset.manifest["partitions"]),
            "manifest_sha256": file_hash(args.data / "manifest.json"),
            "factor_count": 40, "symbols": 80, "sessions": 400,
            "warning": "NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED",
        }, indent=2) + "\n", encoding="utf-8")
        return

    receipt = json.loads((args.report / "data-receipt.json").read_text(encoding="utf-8"))
    process = _require_psutil().Process()
    io_before = process.io_counters()
    started = time.perf_counter()
    with measure_rss() as total:
        dataset = PartitionedDataset(args.data, expected_identity_hash=receipt["identity_hash"])
        names = tuple(r["factor_id"] for r in dataset.manifest["binding"]["factors"])
        # The benchmark calendar is explicitly the generated research calendar;
        # it is not inferred from model labels or from future returns.
        all_dates = pd.bdate_range("2023-01-02", periods=400)
        interval = dataset.manifest["research_interval"]
        dates = all_dates[(all_dates >= interval["start"]) & (all_dates <= interval["end"])]
        runner = WalkForwardRunner(
            ExpandingWalkForwardSplitter(initial_train_months=1, validation_months=1,
                test_months=1, step_months=3, purge_days=5, embargo_days=5),
            model_name="ridge", feature_names=names, ridge_alphas=(0.1, 1.0),
            top_k=5, minimum_cross_section=20)
        measurements = []
        if args.mode == "dense":
            original_frames = runner._fold_frames

            def frames(prepared, folds):
                for fold in folds:
                    with measure_rss() as measurement:
                        yield from original_frames(prepared, [fold])
                    measurements.append(dict(phase="fold", fold=fold.fold,
                        train_rows=len(fold.train_indices), validation_rows=len(fold.validation_indices),
                        test_rows=len(fold.test_indices), **measurement))

            runner._fold_frames = frames
            frame = dataset.to_frame()
            result = runner.run(frame, calendar=dates)
        else:
            result = runner.run_partitioned(dataset, calendar=dates)
            usage = result.resource_usage.astype(object)
            measurements = usage.where(pd.notna(usage), None).to_dict(orient="records")
    io_after = process.io_counters()
    report = dict(mode=args.mode, python=platform.python_version(),
        end_to_end_seconds=time.perf_counter() - started, **total,
        source_identity_hash=dataset.identity_hash, read_stats=dataset.read_stats,
        process_read_bytes=io_after.read_bytes - io_before.read_bytes,
        process_write_bytes=io_after.write_bytes - io_before.write_bytes,
        rows=dataset.manifest["rows"], folds=len(result.fold_summary),
        fold_rows=result.fold_summary[["fold", "train_rows", "train_labeled_rows",
                                       "validation_rows", "test_rows"]].to_dict(orient="records"),
        prediction_rows=len(result.predictions), prediction_sha256=frame_hash(result.predictions),
        fold_summary_sha256=frame_hash(result.fold_summary), measurements=measurements,
        source_hashes={name: file_hash(Path("src/factorlab") / name) for name in (
            "dataset/partitioned.py", "validation/walk_forward.py", "validation/partitioned_walk_forward.py")})
    (args.report / (args.mode + "-metrics.json")).write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("mode", "end_to_end_seconds", "peak_rss_bytes",
                                            "prediction_sha256")}, indent=2))


if __name__ == "__main__":
    main()
