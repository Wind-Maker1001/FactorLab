"""The public synthetic example must remain offline and unambiguous."""

import json
from pathlib import Path

import pandas as pd

from examples.research.phase2_synthetic_demo import WARNING, run_demo


def test_synthetic_demo_runs_both_models_and_marks_everything_synthetic(tmp_path):
    destination = tmp_path / "synthetic"
    manifest = run_demo(destination, seed=42)

    stored = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["synthetic"] is True
    assert stored["network_used"] is False
    assert stored["warning"] == WARNING
    assert set(stored["models"]) == {"factor_score", "ridge"}
    for model_name in stored["models"]:
        predictions = pd.read_parquet(destination / model_name / "predictions.parquet")
        trades = pd.read_csv(
            destination / model_name / "trades.csv",
            parse_dates=["signal_date", "execution_date"],
        )
        assert not predictions.empty
        assert not trades.empty
        assert (trades["execution_date"] > trades["signal_date"]).all()
        assert WARNING in (destination / model_name / "summary.md").read_text(
            encoding="utf-8"
        )
    assert (destination / "comparison" / "model_comparison.csv").is_file()
    assert WARNING in (destination / "comparison" / "summary.md").read_text(
        encoding="utf-8"
    )
    assert WARNING in (destination / "README.md").read_text(encoding="utf-8")
