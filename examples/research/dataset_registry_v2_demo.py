"""Small offline factor-to-model integration proof; no financial admission."""

import argparse
import json
from pathlib import Path
import platform
import time
import tracemalloc

import pandas as pd

from factorlab.dataset import DatasetBuildOptions, DatasetBuilder
from factorlab.dataset.identity import IDENTITY_ATTR, file_hash
from factorlab.exploratory.factor_discovery.generator import generate_candidates
from factorlab.exploratory.factor_discovery.synthetic import make_synthetic_data
from factorlab.factors.registry_v2 import FactorDataContext, FactorDefinition, FactorRegistryV2
from factorlab.metrics.prediction import evaluate_predictions
from factorlab.models.base import ResearchModel
from factorlab.universe.filters import UniverseFilterConfig
from factorlab.validation import ExpandingWalkForwardSplitter, WalkForwardRunner
from examples.research.factor_registry_v2_demo import process_peak_rss_bytes


WARNING = "NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED"


def synthetic_inputs(sessions=280, symbols=8):
    bars, benchmark, dates = make_synthetic_data(stock_count=symbols, sessions=sessions)
    benchmark["adj_close"] = benchmark["adj_open"]
    keys = pd.MultiIndex.from_product(
        [dates, sorted(bars.symbol.unique())], names=["date", "symbol"],
    ).to_frame(index=False)
    membership = keys.loc[keys.date.isin(dates[65:])].assign(is_member=True)
    status = keys.assign(trade_status=1, is_st=False)
    listings = pd.DataFrame({"symbol": bars.symbol.unique(), "listing_date": dates[0]})
    return dates, bars, benchmark, membership, status, listings


def configured_builder():
    registry = FactorRegistryV2.with_builtins()
    # Finite generator is independent of labels and the eventual Test interval.
    candidate = next(r for r in generate_candidates().records if r["compiled"] is not None)
    discovered = registry.register_expression(candidate["candidate_id"], "1", candidate["compiled"])
    alias = registry.register(FactorDefinition.alias("pinned_reversal", "1", registry.get("REV1", "1")))
    snapshot = registry.freeze()
    refs = (snapshot.get("MOM20", "1").ref, discovered, alias)
    return DatasetBuilder(
        DatasetBuildOptions(
            holding_days=5, expected_member_count=8, minimum_cross_section=4,
            filter_config=UniverseFilterConfig(min_listed_days=1, zero_volume_window=1,
                                               quality_window=5, min_quality_ratio=0.8),
        ),
        snapshot=snapshot, factor_refs=refs,
        data_context=FactorDataContext("synthetic-discovery-generator", "SYNTHETIC_CLOSE_ONLY"),
        data_version="seed-20261014-v1", calendar_id="SYNTHETIC_BDAY_NOT_EXCHANGE-v1",
    )


def run_demo(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    tracemalloc.start()
    builder = configured_builder()
    dates, bars, benchmark, membership, status, listings = synthetic_inputs()
    dataset = builder.build(bars, benchmark, membership, status, listings, dates)
    build_seconds = time.perf_counter() - start
    destination, features = output / "dataset.parquet", output / "features"
    sidecar = builder.write_artifacts(dataset, destination, features)
    stored_identity = json.loads(sidecar.read_text(encoding="utf-8"))
    dataset = builder.read_artifacts(
        destination, features, calendar=dates,
        expected_identity_hash=stored_identity["identity_hash"],
        expected_input_hashes=dataset.attrs[IDENTITY_ATTR]["input_hashes"],
    )
    names = builder.factor_names
    models = {}
    for model_name in ("factor_score", "ridge", "lightgbm", "xgboost"):
        options = {}
        if model_name == "factor_score":
            options["factor_weights"] = dict(zip(names, (0.3, 0.4, 0.3)))
        elif model_name == "lightgbm":
            options["lightgbm_candidates"] = [{"n_estimators": 12, "num_leaves": 7, "min_child_samples": 10}]
        elif model_name == "xgboost":
            options["xgboost_candidates"] = [{"n_estimators": 12, "max_depth": 2}]
        runner = WalkForwardRunner(
            ExpandingWalkForwardSplitter(initial_train_months=3, validation_months=2,
                                         test_months=2, step_months=2, purge_days=5, embargo_days=5),
            model_name=model_name, feature_names=names, top_k=2,
            minimum_cross_section=4, ridge_alphas=(0.1, 1.0),
            model_directory=output / model_name / "models", **options,
        )
        result = runner.run(dataset, calendar=membership.date.drop_duplicates())
        evaluation = evaluate_predictions(result.predictions, top_k=2, minimum_cross_section=4)
        model_output = output / model_name
        result.predictions.to_csv(model_output / "predictions.csv", index=False)
        result.fold_summary.to_csv(model_output / "folds.csv", index=False)
        evaluation.daily.to_csv(model_output / "metrics-daily.csv", index=False)
        evaluation.summary.to_csv(model_output / "metrics-summary.csv", index=False)
        result.diagnostic_audit.to_csv(model_output / "fit-audit.csv", index=False)
        result.coefficients.to_csv(model_output / "coefficients.csv", index=False)
        result.feature_importance.to_csv(model_output / "feature-importance.csv", index=False)
        overall = evaluation.overall.iloc[0]
        models[model_name] = {
            "feature_names": list(names),
            "prediction_rows": len(result.predictions), "folds": len(result.fold_summary),
            "fit_scope": result.fold_summary.fit_scope.unique().tolist(),
            "prediction_rank_ic_mean": float(overall.prediction_rank_ic_mean),
            "prediction_ic_mean": float(overall.prediction_ic_mean),
            "predictions_sha256": file_hash(model_output / "predictions.csv"),
            "model_sha256": [file_hash(path) for path in result.model_paths],
            "fit_source_sha256": result.diagnostic_audit.fit_source_sha256.unique().tolist(),
        }
        assert all(json.loads(value) == list(names) for value in result.diagnostic_audit.feature_names)
        assert (result.fold_summary.train_max_exit_date < result.fold_summary.audit_validation_first_date).all()
        assert (result.fold_summary.validation_max_exit_date < result.fold_summary.audit_test_first_date).all()
        # Reload this run's own model and prove that the discovered feature
        # affects actual predictions (rather than merely existing in a table).
        fitted = ResearchModel.load(result.model_paths[-1])
        probe = dataset.loc[:, list(names)].tail(64).copy()
        original_scores = fitted.predict(probe)
        probe["FD_001"] += 5.0
        delta = (fitted.predict(probe) - original_scores).abs().max()
        models[model_name]["discovered_feature_score_delta"] = float(delta)
        assert delta > 0, "Discovered feature must reach the fitted model"
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    manifest = {
        "warning": WARNING, "engineering_status": "P206_ENGINEERING_ACCEPTED",
        "financial_status": "FINANCIAL_ADMISSION_BLOCKED", "synthetic": True,
        "python": platform.python_version(), "rows": len(dataset),
        "dataset_identity_hash": stored_identity["identity_hash"],
        "snapshot_hash": builder.snapshot.snapshot_hash, "sidecar_verified": True,
        "models": models, "build_seconds": build_seconds,
        "total_seconds": time.perf_counter() - start, "tracemalloc_peak_bytes": peak,
        "process_peak_rss_bytes": process_peak_rss_bytes(),
        "dataset_memory_bytes": int(dataset.memory_usage(deep=True).sum()),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=WARNING)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run_demo(parser.parse_args().output), indent=2))
