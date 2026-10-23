"""The public Phase-3 example must be offline, small, and complete."""

import json
import hashlib

import pandas as pd

from examples.research.phase3_synthetic_demo import (
    MODEL_NAMES,
    WARNING,
    run_demo,
)


def test_phase3_synthetic_demo_runs_complete_offline_path(tmp_path):
    destination = tmp_path / "synthetic"
    manifest = run_demo(destination, seed=42)

    stored = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["synthetic"] is True
    assert stored["network_used"] is False
    assert stored["warning"] == WARNING
    assert set(stored["models"]) == set(MODEL_NAMES)
    assert stored["stage_diagnostic_scope"]["train_folds_pooled"] is False
    assert stored["stage_diagnostic_scope"]["real_forward_available"] is False
    assert stored["configuration"]["runner"]["random_seed"] == 42
    assert "pandas" in stored["package_versions"]
    assert set(stored["synthetic_input_sha256"]) == {"dataset", "stock_bars", "benchmark"}
    assert set(stored["market_regime_counts"]) == {"BULL", "BEAR", "SIDEWAYS"}
    assert all(value > 0 for value in stored["market_regime_counts"].values())

    for model_name in MODEL_NAMES:
        model_dir = destination / model_name
        predictions = pd.read_parquet(model_dir / "predictions.parquet")
        fast_trades = pd.read_csv(
            model_dir / "trades.csv",
            parse_dates=["signal_date", "execution_date"],
        )
        execution_trades = pd.read_csv(
            model_dir / "execution_trades.csv",
            parse_dates=["signal_date", "execution_date"],
        )
        prediction_summary = pd.read_csv(model_dir / "prediction_summary.csv")

        assert not predictions.empty
        assert set(predictions["MARKET_REGIME"]) == {"BULL", "BEAR", "SIDEWAYS"}
        assert not fast_trades.empty
        assert not execution_trades.empty
        assert (fast_trades["execution_date"] > fast_trades["signal_date"]).all()
        assert (
            execution_trades["execution_date"] > execution_trades["signal_date"]
        ).all()
        assert (execution_trades["executed_shares"] % 100 == 0).all()
        assert "REGIME" in set(prediction_summary["scope"])
        assert (model_dir / "execution_rejections.parquet").is_file()
        assert WARNING in (model_dir / "summary.md").read_text(encoding="utf-8")
        for name in ("stage_predictions", "stage_prediction_daily", "stage_prediction_summary", "stage_prediction_audit"):
            assert (model_dir / (name + ".csv")).is_file()
            assert not pd.read_parquet(model_dir / (name + ".parquet")).empty
        stage_audit = pd.read_parquet(model_dir / "stage_prediction_audit.parquet")
        assert set(stage_audit["stage"]) == {"TRAIN_IN_SAMPLE", "VALIDATION_SELECTION", "HISTORICAL_TEST"}
        assert stored["models"][model_name]["stage_audit_rows"] == len(stage_audit)
        inventory = stored["models"][model_name]["artifact_sha256"]
        for filename, digest in inventory.items():
            assert hashlib.sha256((model_dir / filename).read_bytes()).hexdigest() == digest
        assert (model_dir / "stage_ic_comparison.png").is_file()

    assert (destination / "ridge" / "ridge_coefficients.parquet").is_file()
    for model_name in ("lightgbm", "xgboost"):
        assert (destination / model_name / "feature_importance.parquet").is_file()

    comparison = pd.read_csv(destination / "comparison" / "model_comparison.csv")
    assert set(comparison["model_name"]) == set(MODEL_NAMES)
    assert (destination / "comparison" / "execution_nav.parquet").is_file()
    stage_summary = pd.read_parquet(destination / "comparison" / "stage_prediction_summary.parquet")
    assert set(stage_summary["model_name"]) == set(MODEL_NAMES)
    assert set(stage_summary["stage"]) == {"TRAIN_IN_SAMPLE", "VALIDATION_SELECTION", "HISTORICAL_TEST"}
    assert not (destination / "comparison" / "stage_predictions.parquet").exists()
    inference = pd.read_parquet(destination / "comparison" / "model_inference.parquet")
    primary = inference.loc[inference.primary_hypothesis]
    assert len(primary) == 3
    assert primary.status.eq("INSUFFICIENT_DATES").all()
    assert primary.holm_adjusted_p_value.isna().all()
    assert primary.holm_reject.isna().all()
    assert stored["configuration"]["inference"]["automatic_model_selection"] is False
    assert stored["configuration"]["inference"]["options"]["min_valid_days"] == 60
    assert (destination / "comparison" / "model_inference.png").is_file()
    assert set(stored["inference_evidence"]) == {"daily_sha256", "summary_sha256", "audit_sha256", "protocol_sha256"}
    assert comparison["stage_diagnostics_available"].all()
    for filename, digest in stored["comparison_artifact_sha256"].items():
        assert hashlib.sha256((destination / "comparison" / filename).read_bytes()).hexdigest() == digest
    assert WARNING in (destination / "comparison" / "summary.md").read_text(
        encoding="utf-8"
    )
    assert WARNING in (destination / "README.md").read_text(encoding="utf-8")
