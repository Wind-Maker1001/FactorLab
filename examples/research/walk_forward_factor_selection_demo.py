"""Synthetic A/B plumbing demonstration; no claim of market superiority.

PYTHONPATH=src python -B -m examples.research.walk_forward_factor_selection_demo --output ...
"""

import argparse
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import platform
import sys
import time
import tracemalloc

import numpy as np
import pandas as pd

from factorlab.dataset import DatasetBuildOptions, DatasetBuilder
from factorlab.dataset.identity import IDENTITY_ATTR, file_hash, frame_hash
from factorlab.factors.registry_v2 import FactorDataContext, FactorDefinition, FactorRegistryV2
from factorlab.metrics.prediction import evaluate_predictions
from factorlab.models.base import ResearchModel
from factorlab.universe.filters import UniverseFilterConfig
from factorlab.validation import ExpandingWalkForwardSplitter, FactorSelectionConfig, TrainFactorSelector, WalkForwardRunner
from examples.research.dataset_registry_v2_demo import synthetic_inputs, WARNING
from examples.research.factor_registry_v2_demo import approved_definitions, process_peak_rss_bytes


def experiment():
    registry = FactorRegistryV2.with_builtins()
    original = tuple(d.factor_id for d in registry.freeze().definitions)
    # Fixed before generating labels: 16 lag ratios plus 14 original factors,
    # a same-definition ID and a dependency alias. No full-history screening.
    expressions = [d for d in approved_definitions()
                   if d.input_fields in (("turnover",), ("volume",))][:16]
    # Include explicit constant ratios too: the selector must reject them.
    for definition in expressions:
        registry.register(definition)
    registry.register(FactorDefinition.builtin("REV1", factor_id="REV1_COPY"))
    registry.register(FactorDefinition.alias("REV1_ALIAS", "1", registry.get("REV1", "1")))
    snapshot = registry.freeze()
    refs = tuple(d.ref for d in snapshot.definitions)
    builder = DatasetBuilder(DatasetBuildOptions(
        holding_days=5, expected_member_count=24, minimum_cross_section=12,
        filter_config=UniverseFilterConfig(min_listed_days=1, zero_volume_window=1,
                                            quality_window=5, min_quality_ratio=0.8)),
        snapshot=snapshot, factor_refs=refs,
        data_context=FactorDataContext("p207-synthetic-seed-20261014", "SYNTHETIC_CLOSE_ONLY"),
        data_version="p207-synthetic-v1", calendar_id="SYNTHETIC_BDAY_NOT_EXCHANGE-v1")
    config = FactorSelectionConfig(k=10, minimum_selected=1, shortfall_policy="use_available",
        minimum_coverage=0.8, maximum_missing_ratio=0.2, maximum_nonfinite_ratio=0,
        minimum_observations=100, minimum_cross_section=12, minimum_dates=20,
        minimum_abs_rank_ic=0.01, minimum_direction_consistency=0.5,
        minimum_pair_coverage=0.8, redundancy_threshold=0.95, orient_by_train_ic=True)
    return builder, original, TrainFactorSelector(snapshot, refs, config)


def run_demo(output, reference_runner=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    builder, original, selector = experiment()
    dates, bars, benchmark, membership, status, listings = synthetic_inputs(sessions=300, symbols=24)
    dataset = builder.build(bars, benchmark, membership, status, listings, dates)
    destination, features = output / "dataset.parquet", output / "features"
    sidecar = builder.write_artifacts(dataset, destination, features)
    identity = json.loads(sidecar.read_text(encoding="utf-8"))
    dataset = builder.read_artifacts(destination, features, calendar=dates,
                                     expected_identity_hash=identity["identity_hash"])
    calendar = membership.date.drop_duplicates()
    split = ExpandingWalkForwardSplitter(initial_train_months=3, validation_months=1,
        test_months=2, step_months=2, purge_days=5, embargo_days=5)
    reference = None
    if reference_runner:
        spec = importlib.util.spec_from_file_location("p207_main_reference", reference_runner)
        reference = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = reference
        spec.loader.exec_module(reference)
    records, receipts, frequencies, samples, proofs = [], [], [], [], []
    quality = redundancy = folds = None
    for mode in ("A_fixed14", "B_dynamic"):
        for model in ("factor_score", "ridge", "lightgbm", "xgboost"):
            names = original if mode == "A_fixed14" else selector.feature_names
            options = {"ridge_alphas": (0.1, 1.0)}
            if model == "factor_score":
                options.update({"factor_weights": dict.fromkeys(names, 1 / len(names))}
                    if mode == "A_fixed14" else {"dynamic_factor_score_weighting": "equal"})
            elif model == "lightgbm":
                options["lightgbm_candidates"] = [{"n_estimators": 12, "num_leaves": 7, "min_child_samples": 10}]
            elif model == "xgboost":
                options["xgboost_candidates"] = [{"n_estimators": 12, "max_depth": 2}]
            run_path = output / mode / model
            base_options = dict(model_name=model, feature_names=names, top_k=5,
                                minimum_cross_section=12, **options)
            configured = WalkForwardRunner(split, model_directory=run_path / "models",
                factor_selector=selector if mode == "B_dynamic" else None, **base_options)
            tracemalloc.start()
            tick = time.perf_counter()
            result = configured.run(dataset, calendar=calendar)
            seconds = time.perf_counter() - tick
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert len(result.fold_summary) >= 2
            evaluation = evaluate_predictions(result.predictions, top_k=5, minimum_cross_section=12)
            record = {"mode": mode, "model": model, "folds": len(result.fold_summary),
                "oos_rank_ic": float(evaluation.overall.iloc[0].prediction_rank_ic_mean),
                "fold_rank_ic_std": float(result.fold_summary.test_prediction_rank_ic.std(ddof=1)),
                "fold_rank_ic": result.fold_summary.test_prediction_rank_ic.tolist(),
                "seconds": seconds, "python_peak_bytes": peak,
                "process_lifetime_peak_rss_bytes": process_peak_rss_bytes(),
                "prediction_rows": len(result.predictions), "predictions_sha256": frame_hash(result.predictions)}
            if mode == "A_fixed14" and reference is not None:
                control = reference.WalkForwardRunner(split, **base_options).run(dataset, calendar=calendar)
                for field in ("predictions", "validation_metrics", "coefficients", "feature_importance",
                              "diagnostic_predictions", "diagnostic_daily", "diagnostic_summary"):
                    pd.testing.assert_frame_equal(getattr(result, field), getattr(control, field), check_exact=True)
                pd.testing.assert_frame_equal(result.fold_summary.drop(columns="model_path"),
                                               control.fold_summary.drop(columns="model_path"), check_exact=True)
                record["main_fixed_exact_parity"] = True
            for fold, path in zip(list(split.split(dataset, calendar=calendar)), result.model_paths):
                chosen = list(names) if mode == "A_fixed14" else result.factor_selection.iloc[fold.fold].selected_features
                direction = dict.fromkeys(chosen, 1) if mode == "A_fixed14" else result.factor_selection.iloc[fold.fold].directions
                fitted = ResearchModel.load(path)
                assert list(fitted.feature_names) == chosen
                test = dataset.iloc[fold.test_indices].copy()
                for name, sign in direction.items():
                    test[name] *= sign
                expected = pd.Series(fitted.predict(test[chosen]).to_numpy(),
                    index=pd.MultiIndex.from_frame(test[["date", "symbol"]])).sort_index()
                actual = result.predictions[result.predictions.fold.eq(fold.fold)].set_index(["date", "symbol"]).score.sort_index()
                np.testing.assert_array_equal(expected, actual)
                audit = result.diagnostic_audit.query("fold == @fold.fold and stage == 'HISTORICAL_TEST'").iloc[0]
                assert json.loads(audit.feature_names) == chosen
                proofs.append({"mode": mode, "model": model, "fold": fold.fold,
                    "features": chosen, "directions": direction, "restored_predictions_exact": True,
                    "saved_model_sha256": file_hash(path), "fit_source_sha256": audit.fit_source_sha256,
                    "stage_prediction_sha256": audit.prediction_sha256})
            result.predictions.to_csv(run_path / "predictions.csv", index=False)
            result.fold_summary.to_csv(run_path / "folds.csv", index=False)
            result.diagnostic_audit.to_csv(run_path / "fit-audit.csv", index=False)
            samples.append(result.predictions.head(4).assign(mode=mode))
            if mode == "B_dynamic":
                receipts.extend(result.factor_selection.to_dict("records"))
                if quality is None:
                    quality, redundancy, folds = result.factor_quality, result.factor_redundancy, result.fold_summary
                    quality.to_csv(output / "factor-quality.csv", index=False)
                    redundancy.to_csv(output / "factor-redundancy.csv", index=False)
                    folds.to_csv(output / "dynamic-folds.csv", index=False)
                    for name in selector.feature_names:
                        count = sum(name in ns for ns in result.factor_selection.selected_features)
                        frequencies.append({"mode": mode, "factor": name, "fold_count": count,
                                            "frequency": count / len(result.fold_summary)})
            records.append(record)
    for name in original:
        frequencies.append({"mode": "A_fixed14", "factor": name,
                            "fold_count": len(folds), "frequency": 1.0})
    manifest = {"engineering_status": "P207_ENGINEERING_ACCEPTED", "warning": WARNING,
        "synthetic": True, "python": platform.python_version(), "rows": len(dataset),
        "candidate_count": len(selector.factor_refs), "fixed_factor_names": list(original),
        "selection_config": asdict(selector.config), "dataset_identity_hash": identity["identity_hash"],
        "dataset_sidecar_verified": True, "dataset_memory_bytes": int(dataset.memory_usage(deep=True).sum()),
        "reference_runner_sha256": file_hash(reference_runner) if reference_runner else None,
        "total_seconds": time.perf_counter() - started, "models": records}
    for name, payload in (("manifest.json", manifest), ("selections.json", receipts), ("model-proof.json", proofs)):
        (output / name).write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    pd.DataFrame(frequencies).to_csv(output / "selection-frequency.csv", index=False)
    pd.concat(samples).to_csv(output / "predictions.sample.csv", index=False)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=WARNING)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-runner", type=Path)
    args = parser.parse_args()
    print(json.dumps(run_demo(args.output, args.reference_runner), indent=2))
