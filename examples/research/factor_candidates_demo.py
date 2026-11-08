"""Run bounded P3-11 synthetic research: python -m examples.research.factor_candidates_demo --output PATH."""

import argparse
from dataclasses import asdict
import json
from importlib.metadata import version
from pathlib import Path
import platform
import time
import tracemalloc

import pandas as pd

from examples.research.dataset_registry_v2_demo import synthetic_inputs, WARNING
from factorlab.dataset import DatasetBuildOptions, DatasetBuilder
from factorlab.dataset.identity import frame_hash, json_hash, file_hash
from factorlab.dataset.partitioned import ChunkedConfig
from factorlab.factors.candidates import candidate_catalog, catalog_records, register_candidates
from factorlab.factors.registry_v2 import FactorDataContext, FactorRegistryV2
from factorlab.universe.filters import UniverseFilterConfig
from factorlab.validation import ExpandingWalkForwardSplitter, FactorSelectionConfig, TrainFactorSelector, WalkForwardRunner
from factorlab.validation.ablation import _OneFold
from factorlab.validation.ablation_resources import ResidentMemorySample
from factorlab.validation.ablation_statistics import daily_receipts
from factorlab.validation.candidate_research import variation_diagnostics, summarize_candidates, candidate_contrasts, recompute_output

MODELS = {"factor_score": {}, "ridge": {"ridge_alphas": (1.0,)},
          "lightgbm": {"lightgbm_candidates": [{"n_estimators": 12, "num_leaves": 7, "min_child_samples": 10}]},
          "xgboost": {"xgboost_candidates": [{"n_estimators": 12, "max_depth": 2}]}}


def experiment():
    registry = FactorRegistryV2.with_builtins()
    baseline = tuple(d.factor_id for d in registry.freeze().definitions)
    register_candidates(registry)
    snapshot = registry.freeze()
    refs = tuple(row["definition"].ref for row in candidate_catalog())
    builder = DatasetBuilder(DatasetBuildOptions(holding_days=5, expected_member_count=24,
        minimum_cross_section=12, filter_config=UniverseFilterConfig(min_listed_days=1,
        zero_volume_window=1, quality_window=5, min_quality_ratio=0.8)), snapshot=snapshot,
        factor_refs=tuple(d.ref for d in snapshot.definitions),
        data_context=FactorDataContext("p311-synthetic-seed-20261014", "SYNTHETIC_CLOSE_ONLY"),
        data_version="p311-synthetic-v1", calendar_id="SYNTHETIC_BDAY_NOT_EXCHANGE-v1")
    selector = TrainFactorSelector(snapshot, refs, FactorSelectionConfig(k=10,
        minimum_cross_section=12, minimum_dates=20))
    return builder, baseline, selector


def splitter():
    return ExpandingWalkForwardSplitter(initial_train_months=3, validation_months=1,
        test_months=2, step_months=2, purge_days=5, embargo_days=5)


def prepare(data, calendar, selector):
    """Freeze every fold's Train-only choice before any Test evaluation."""
    prepared = WalkForwardRunner(splitter(), model_name="ridge", feature_names=selector.feature_names)._validate_data(data)
    folds = list(splitter().split(prepared, calendar=calendar))
    selections, plans, quality, redundancy, variation = {}, [], [], [], []
    for fold in folds:
        train = prepared.iloc[fold.train_indices]
        if train.exit_date.max() >= fold.audit.validation_first_date:
            raise ValueError("Unmatured Train label")
        selected = selector.select(train)
        selections[fold.fold] = selected
        plans.append({"fold": fold.fold, **selected.receipt,
                      "train_window": [d.isoformat() for d in fold.train_window],
                      "validation_window": [d.isoformat() for d in fold.validation_window],
                      "test_window": [d.isoformat() for d in fold.test_window],
                      "train_max_exit": train.exit_date.max().isoformat()})
        quality.append(selected.quality.assign(fold=fold.fold))
        redundancy.append(selected.redundancy.assign(fold=fold.fold))
        variation.append(variation_diagnostics(train, selector.feature_names).assign(fold=fold.fold))
    return prepared, folds, selections, plans, pd.concat(quality), pd.concat(redundancy), pd.concat(variation)


def run_demo(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    builder, baseline, selector = experiment()
    dates, bars, benchmark, membership, status, listings = synthetic_inputs(sessions=300, symbols=24)
    # Includes 65 pre-membership sessions; the longest prior-session requirement is 60.
    partitioned = builder.build_chunked(bars, benchmark, membership, status, listings, dates,
        output_dir=output / "dataset", config=ChunkedConfig())
    data = partitioned.to_frame()
    calendar = membership.date.drop_duplicates()
    prepared, folds, selections, plans, quality, redundancy, variation = prepare(data, calendar, selector)
    for name, frame in (("quality", quality), ("redundancy", redundancy), ("variation", variation)):
        frame.to_csv(output / (name + ".csv"), index=False)
    (output / "plans.json").write_text(json.dumps(plans, indent=2) + "\n", encoding="utf-8")
    (output / "catalog.json").write_text(json.dumps(catalog_records(bars.columns), indent=2) + "\n", encoding="utf-8")
    categories = sorted({row["category"] for row in candidate_catalog()})
    arms = {"baseline14": baseline, "fixed64": selector.feature_names, "selected": None}
    arms.update({"without_" + category: tuple(row["definition"].factor_id for row in candidate_catalog()
                 if row["category"] != category) for category in categories})
    daily, resources, audits = [], [], []
    for group, fixed in arms.items():
        for model, options in MODELS.items():
            if group.startswith("without_") and model != "factor_score":
                continue
            for fold in folds:
                selected = selections[fold.fold]
                names = selected.feature_names if fixed is None else fixed
                local = selected.transform(prepared) if fixed is None else prepared.copy()
                local.attrs.clear()
                weights = {"factor_weights": dict.fromkeys(names, 1 / len(names)),
                           "normalize_by_absolute_weight": False} if model == "factor_score" else {}
                runner = WalkForwardRunner(_OneFold(splitter(), fold), model_name=model,
                    feature_names=names, minimum_cross_section=12, top_k=5, random_seed=42,
                    model_directory=output / "models" / group / model, **weights, **options)
                tracemalloc.start()
                tick = time.perf_counter()
                try:
                    with ResidentMemorySample() as memory:
                        result = runner.run(local, calendar=calendar)
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                if not all(json.loads(value) == list(names) for value in result.diagnostic_audit.feature_names):
                    raise AssertionError("Model audit feature order differs from frozen plan")
                resources.append({"group": group, "model": model, "fold": fold.fold,
                    "features": len(names), "seconds": time.perf_counter() - tick,
                    "python_peak_bytes": peak, "sampled_peak_rss_bytes": memory.peak})
                daily.append(daily_receipts(result.predictions, group=group, model=model, minimum_cross_section=12))
                audits.append(result.diagnostic_audit.assign(group=group))
    daily = pd.concat(daily, ignore_index=True)
    summary = summarize_candidates(daily)
    daily.to_csv(output / "daily.csv", index=False)
    summary.to_csv(output / "summary.csv", index=False)
    candidate_contrasts(summary).to_csv(output / "contrasts.csv", index=False)
    pd.DataFrame([{"factor_id": name, "selected_folds": sum(name in p["selected_features"] for p in plans),
                   "total_folds": len(plans)} for name in selector.feature_names]).to_csv(output / "frequency.csv", index=False)
    pd.DataFrame(resources).to_csv(output / "resources.csv", index=False)
    pd.concat(audits).to_csv(output / "fit-audit.csv", index=False)
    stability = []
    for i, left in enumerate(plans):
        for right in plans[i + 1:]:
            a, b = set(left["selected_features"]), set(right["selected_features"])
            stability.append({"left": left["fold"], "right": right["fold"], "jaccard": len(a & b) / len(a | b)})
    manifest = {"schema": "P311_V1", "warning": WARNING, "synthetic": True,
        "python": platform.python_version(),
        "dependencies": {name: version(name) for name in ("numpy", "pandas", "scikit-learn", "lightgbm", "xgboost")},
        "real_market_predictive_ability": "NOT_VERIFIED", "candidate_count": 64,
        "executable_count": len(selector.factor_refs), "unique_definition_count": len({r.definition_hash for r in selector.factor_refs}),
        "baseline_count": len(baseline), "folds": len(folds), "rows": len(data),
        "selection_config": asdict(selector.config), "models": MODELS,
        "chunked_config": asdict(ChunkedConfig()),
        "selection_jaccard": stability, "snapshot_hash": selector.snapshot.snapshot_hash,
        "dataset_hash": frame_hash(data), "dataset_identity_hash": partitioned.identity_hash,
        "catalog_hash": json_hash(catalog_records()), "common_sample_identity_verified": True,
        "counts_by_fold": [{"fold": p["fold"], "eligible_after_redundancy": p["eligible_count"],
                            "selected": p["selected_count"]} for p in plans],
        "statistics": "daily equal-weight RankIC; ICIR mean/sample std, unannualized; descriptive synthetic only",
        "memory": "per-run tracemalloc peak and sampled process RSS; native RSS includes shared state",
        "ablation": "predeclared leave-one-category-out fixed candidates; FactorScore only; no causal efficacy claim"}
    receipt = recompute_output(output)
    (output / "recomputation.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    manifest["artifact_sha256"] = {p.name: file_hash(p) for p in output.iterdir()
                                   if p.suffix in (".csv", ".json") and p.name != "manifest.json"}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=WARNING)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recompute-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(recompute_output(args.output) if args.recompute_only else run_demo(args.output), indent=2))
