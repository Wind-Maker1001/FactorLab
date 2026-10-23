"""Deterministic Dataset -> four models -> statistics -> both backtests acceptance."""

import json

import numpy as np
import pandas as pd
import pytest

from factorlab.diagnostics.model_readiness.checks import assert_folds, statistical_probes
from factorlab.diagnostics.model_readiness.profiling import Recorder
from factorlab.diagnostics.model_readiness.runner import run_acceptance
from factorlab.diagnostics.model_readiness.synthetic import (
    MODELS, TARGET, build_dataset, generate_inputs,
)
from factorlab.metrics.robustness import RobustnessOptions, evaluate_model_robustness


@pytest.fixture(scope="module")
def acceptance(tmp_path_factory):
    output = tmp_path_factory.mktemp("readiness") / "run"
    return output, run_acceptance(output)


def test_complete_small_software_acceptance_and_data_separation(acceptance):
    output, report = acceptance
    failures = [row for row in report["matrix"] if row["status"] == "FAIL"]
    assert failures == [], report.get("failure")
    assert set(report["models"]) == set(MODELS)
    for row in report["matrix"]:
        if row["check"] in ("Formal", "Forward"):
            assert row["status"] == "BLOCKED"
        elif row["check"] in ("scale_run", "formal_parameter_stress", "comparison_counterexamples"):
            assert row["status"] == "NOT_RUN"
        else:
            assert row["status"] == "PASS", row
    assert report["financial_data_qualified"] is False
    assert report["schema_version"] == 2
    assert report["source_verification"]["status"] == "PASS"
    start = json.loads((output / "source-start.json").read_text(encoding="utf-8"))
    end = json.loads((output / "source-end.json").read_text(encoding="utf-8"))
    assert start == end
    assert report["environment"]["source_hash_protocol"] == start["protocol"]
    assert report["environment"]["source_sha256"] == start["canonical_sha256"] == start["git_canonical_sha256"]
    assert (output / "readiness.md").exists()
    assert json.loads((output / "readiness.json").read_text(encoding="utf-8")) == report


def test_native_resources_cover_every_stage(acceptance):
    output, _ = acceptance
    rows = pd.read_csv(output / "resources.csv")
    assert {"dataset_build", "train", "predict", "prediction_metrics", "statistical_evaluation",
            "fast_backtest", "execution_backtest"}.issubset(rows.stage)
    assert rows.seconds.ge(0).all()
    assert rows.sampled_peak_rss_bytes.gt(0).all()
    assert rows.process_high_watermark_bytes.ge(rows.sampled_peak_rss_bytes).all()
    assert rows.status.eq("PASS").all()


def test_label_windows_remove_unmatured_and_boundary_rows_even_without_gaps():
    inputs = generate_inputs()
    frame = build_dataset(inputs)
    frame.loc[frame.index[0], TARGET] = np.nan
    folds = assert_folds(frame, inputs.research_calendar, purge_days=0, embargo_days=0)
    assert any(fold.audit.train_overlap_rows_removed > 0 for fold in folds)
    assert any(fold.audit.validation_overlap_rows_removed > 0 for fold in folds)
    # Deliberately extending an old row's label horizon must remove it from Train.
    row = int(folds[0].train_indices[10])
    frame.loc[row, "exit_date"] = folds[0].test_dates[0]
    changed = assert_folds(frame, inputs.research_calendar, purge_days=0, embargo_days=0)
    assert row not in changed[0].train_indices
    assert frame.loc[frame.date.isin(inputs.calendar[-6:]), TARGET].isna().all()


def test_statistics_reuse_and_serial_dependence_counterexample():
    evidence = statistical_probes()
    assert evidence["hac_to_iid_se_ratio"] > 2
    assert evidence["extreme_inputs_rejected"] == 2


def test_joint_missing_rankic_does_not_become_difference_of_unpaired_means():
    days = pd.bdate_range("2020-01-02", periods=120)
    frames = []
    for offset, model in enumerate(MODELS):
        values = np.sin(np.arange(120) / 7 + offset) / 4
        frames.append(pd.DataFrame({"date": days, "model_name": model, "fold": 0,
                                    "stage": "HISTORICAL_TEST", "prediction_rank_ic": values,
                                    "prediction_ic": values, "top_bottom_spread": values / 100}))
    daily = pd.concat(frames, ignore_index=True)
    daily.loc[daily.model_name.eq("ridge") & daily.date.isin(days[::5]), "prediction_rank_ic"] = np.nan
    result = evaluate_model_robustness(daily, days, RobustnessOptions(bootstrap_repetitions=100))
    paired = result.summary.loc[result.summary.primary_hypothesis & result.summary.model_name.eq("ridge")].iloc[0]
    wide = daily.pivot(index="date", columns="model_name", values="prediction_rank_ic")
    assert paired.valid_dates == 96
    assert paired["mean"] == pytest.approx((wide.ridge - wide.factor_score).mean())
    assert paired["mean"] != pytest.approx(wide.ridge.mean() - wide.factor_score.mean())
    assert result.protocol["primary_family_size"] == 3


def test_failure_receipt_keeps_exact_stage_and_exception(tmp_path):
    recorder = Recorder(tmp_path)
    try:
        with pytest.raises(MemoryError, match="synthetic allocation failure"):
            with recorder.span("train", "ridge", fold=2):
                raise MemoryError("synthetic allocation failure")
    finally:
        recorder.close()
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in rows] == ["START", "END"]
    assert rows[-1]["status"] == "FAIL"
    assert rows[-1]["stage"] == "train" and rows[-1]["fold"] == 2
    assert rows[-1]["exception"] == "MemoryError"


def test_reused_output_refused_to_prevent_stale_acceptance(acceptance):
    with pytest.raises(ValueError, match="new or empty"):
        run_acceptance(acceptance[0])
