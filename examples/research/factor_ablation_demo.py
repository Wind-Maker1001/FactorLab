"""P2-08: reuse the P2-07 32-factor synthetic pool and existing four models.

PYTHONPATH=src python -B -m examples.research.factor_ablation_demo --output ...
"""

import argparse
import json
from pathlib import Path
import platform
import time

import pandas as pd

from examples.research.walk_forward_factor_selection_demo import experiment
from examples.research.dataset_registry_v2_demo import synthetic_inputs, WARNING
from examples.research.factor_registry_v2_demo import process_peak_rss_bytes
from factorlab.dataset.identity import file_hash
from factorlab.validation import AblationConfig, ExpandingWalkForwardSplitter, FactorAblationRunner
from factorlab.validation.ablation_statistics import summarize_ablation, selection_stability


def run_demo(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    builder, original, selector = experiment()
    # This rule is declared before synthetic labels exist, with no screening.
    fixed = tuple(sorted(original)[:selector.config.k])
    config = AblationConfig(fixed, "P208-v1: lexicographically first K builtin IDs; declared before synthetic_inputs", original)
    dates, bars, benchmark, membership, status, listings = synthetic_inputs(sessions=300, symbols=24)
    dataset = builder.build(bars, benchmark, membership, status, listings, dates)
    destination, features = output / "dataset.parquet", output / "features"
    sidecar = builder.write_artifacts(dataset, destination, features)
    identity = json.loads(sidecar.read_text(encoding="utf-8"))
    dataset = builder.read_artifacts(destination, features, calendar=dates,
        expected_identity_hash=identity["identity_hash"])
    splitter = ExpandingWalkForwardSplitter(initial_train_months=3, validation_months=1,
        test_months=2, step_months=2, purge_days=5, embargo_days=5)
    options = {"factor_score": {}, "ridge": {"ridge_alphas": (0.1, 1.0)},
        "lightgbm": {"lightgbm_candidates": [
            {"n_estimators": 12, "num_leaves": 7, "min_child_samples": 10},
            {"n_estimators": 20, "num_leaves": 7, "min_child_samples": 10}]},
        "xgboost": {"xgboost_candidates": [
            {"n_estimators": 12, "max_depth": 2}, {"n_estimators": 20, "max_depth": 2}]}}
    runner = FactorAblationRunner(splitter, selector, config, model_options=options,
                                 minimum_cross_section=12, top_k=5)
    result = runner.run(dataset, calendar=membership.date.drop_duplicates(), model_directory=output / "models")
    assert result.manifest["fold_count"] == 3
    result.manifest.update({"engineering_status": "P208_ENGINEERING_ACCEPTED", "warning": WARNING,
        "python": platform.python_version(), "synthetic": True, "rows": len(dataset),
        "dataset_memory_bytes": int(dataset.memory_usage(deep=True).sum()),
        "dataset_sidecar_verified": True, "candidate_count": len(selector.factor_refs),
        "total_seconds": time.perf_counter() - started,
        "process_lifetime_peak_rss_bytes": process_peak_rss_bytes()})
    result.write(output)
    # Recompute from the deliverable, without model objects or predictions.
    restored = pd.read_csv(output / "daily.csv")
    for actual, expected in zip(summarize_ablation(restored), (result.metrics, result.contrasts, result.volatility)):
        pd.testing.assert_frame_equal(actual, expected, check_exact=False, atol=1e-12)
    plans = pd.DataFrame(json.loads((output / "plans.json").read_text(encoding="utf-8")))
    for actual, expected in zip(selection_stability(plans, selector.feature_names), (result.frequency, result.jaccard)):
        pd.testing.assert_frame_equal(actual, expected)
    receipt = {"summary_recomputed": True, "model_reload_exact_count": int(
        result.audit.loc[result.audit.stage.eq("HISTORICAL_TEST"), "restored_test_predictions_exact"].sum()),
        "contrast_status_counts": result.contrasts.rank_ic_status.value_counts().to_dict(),
        "artifact_sha256": {p.name: file_hash(p) for p in output.iterdir() if p.is_file() and p.suffix in (".json", ".csv")}}
    (output / "recomputation.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return result.manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=WARNING)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_demo(args.output), indent=2))
