"""Synthetic stage-report contracts; not real-data research performance."""

from dataclasses import replace

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest

from qbot.research.backtest.fast import FastBacktestResult
from qbot.research.metrics.prediction import PredictionEvaluation, evaluate_predictions
from qbot.research.reports import model as report
from qbot.research.validation.splitter import ExpandingWalkForwardSplitter
from qbot.research.validation.walk_forward import WalkForwardRunner, diagnostic_frame_sha256


_FIELDS = ("diagnostic_predictions", "diagnostic_daily", "diagnostic_summary", "diagnostic_audit")
_TABLES = ("stage_predictions", "stage_prediction_daily", "stage_prediction_summary", "stage_prediction_audit")


@pytest.fixture(scope="module")
def synthetic_result():
    dates = pd.bdate_range("2020-01-01", "2021-05-31")
    symbols = ["000001.SZ", "000002.SZ", "600000.SH", "600001.SH"]
    frame = pd.DataFrame({"date": np.repeat(dates.to_numpy(), 4), "symbol": symbols * len(dates)})
    frame["REV1"] = np.tile([0.3, 0.1, -0.1, -0.3], len(dates))
    frame["MOM20"] = np.repeat(np.sin(np.arange(len(dates)) / 30), 4) + np.tile([0.2, -0.1, 0.1, -0.2], len(dates))
    frame["EXCESS_RET5"] = 0.03 * frame["REV1"] + 0.004 * frame["MOM20"]
    entries = pd.Series(list(dates[1:]) + [pd.NaT], index=dates)
    exits = pd.Series(list(dates[6:]) + [pd.NaT] * 6, index=dates)
    frame["entry_date"] = frame["date"].map(entries)
    frame["exit_date"] = frame["date"].map(exits)
    runner = WalkForwardRunner(
        ExpandingWalkForwardSplitter(initial_train_months=6, validation_months=2, test_months=2, step_months=2, purge_days=5, embargo_days=5),
        model_name="factor_score", feature_names=["REV1", "MOM20"],
        factor_weights={"REV1": 1.0, "MOM20": 0.1}, top_k=1, minimum_cross_section=4,
    )
    return runner.run(frame, calendar=dates)


@pytest.fixture
def result(synthetic_result):
    return replace(synthetic_result, **{
        name: getattr(synthetic_result, name).copy(deep=True)
        for name in ("predictions", "fold_summary", "validation_metrics", "coefficients", "feature_importance") + _FIELDS
    })


def _prediction(result):
    return evaluate_predictions(result.predictions, top_k=1, minimum_cross_section=4)


def _backtest():
    return FastBacktestResult(nav=pd.DataFrame(), trades=pd.DataFrame(), positions=pd.DataFrame(), signals=pd.DataFrame())


def _write(result, path, prediction=None):
    return report.write_model_report(
        "factor_score", result, _prediction(result) if prediction is None else prediction,
        _backtest(), {}, path, disclaimer="SYNTHETIC SOFTWARE TEST ONLY - NOT A RESEARCH RESULT",
    )


def test_complete_stage_artifacts_preserve_selected_scores_and_test_only_outputs(result, tmp_path):
    before = result.predictions.copy(deep=True)
    summary_path = _write(result, tmp_path)
    for name, field in zip(_TABLES, _FIELDS):
        assert (tmp_path / (name + ".csv")).is_file()
        stored = pd.read_parquet(tmp_path / (name + ".parquet"))
        original = getattr(result, field)
        assert list(stored.dtypes.astype(str)) == list(original.dtypes.astype(str))
        pd.testing.assert_frame_equal(stored.astype(object).where(stored.notna(), None), original.astype(object).where(original.notna(), None), check_exact=True)
        assert diagnostic_frame_sha256(stored) == diagnostic_frame_sha256(getattr(result, field))
    stored_test = pd.read_parquet(tmp_path / "predictions.parquet")
    pd.testing.assert_frame_equal(stored_test, before)
    pd.testing.assert_frame_equal(result.predictions, before)
    assert "stage" not in stored_test
    assert set(pd.read_parquet(tmp_path / "stage_predictions.parquet")["stage"]) == {"TRAIN_IN_SAMPLE", "VALIDATION_SELECTION"}
    assert (tmp_path / "stage_ic_comparison.png").read_bytes().startswith(b"\x89PNG")
    text = summary_path.read_text(encoding="utf-8")
    assert "in-sample diagnostic only" in text and "selection optimism" in text
    assert "not an independent final holdout" in text
    assert "not pooled" in text and "not a train-plus-validation refit" in text
    assert "before transaction costs" in text and "not realised portfolio returns" in text
    assert "SYNTHETIC SOFTWARE TEST ONLY" in text
    assert not list(tmp_path.glob(".*.tmp*"))


def test_public_validator_accepts_foldwise_overlap_without_pooled_training(result):
    tables = report.validate_stage_diagnostics(result)
    assert set(tables) == set(_TABLES)
    train = tables["stage_predictions"].loc[lambda frame: frame["stage"].eq("TRAIN_IN_SAMPLE")]
    assert train.duplicated(["date", "symbol"]).any()
    assert not train.duplicated(["fold", "stage", "date", "symbol"]).any()
    assert tables["stage_prediction_audit"].groupby("fold").size().eq(3).all()
    tables["stage_predictions"].loc[0, "score"] = 123
    assert result.diagnostic_predictions.loc[0, "score"] != 123


@pytest.mark.parametrize("field", _FIELDS)
def test_partial_diagnostics_fail_before_publishing(result, tmp_path, field):
    altered = replace(result, **{field: pd.DataFrame()})
    with pytest.raises(report.ModelReportError, match="incomplete"):
        _write(altered, tmp_path / "unpublished")
    assert not (tmp_path / "unpublished").exists()


@pytest.mark.parametrize("field,column,bad", [
    ("diagnostic_predictions", "stage", "HISTORICAL_TEST"),
    ("diagnostic_daily", "fit_scope", "train_plus_validation"),
    ("diagnostic_summary", "selection_usage", "selection_and_diagnostic"),
    ("diagnostic_audit", "selection_usage", "evaluation_only"),
    ("diagnostic_audit", "model_name", "ridge"),
    ("diagnostic_audit", "source_keys_sha256", "f" * 64),
    ("diagnostic_audit", "fit_source_keys_sha256", "f" * 64),
    ("diagnostic_audit", "prediction_sha256", "f" * 64),
    ("diagnostic_audit", "daily_sha256", "f" * 64),
    ("diagnostic_audit", "summary_sha256", "f" * 64),
    ("diagnostic_audit", "source_sha256", "unknown"),
    ("diagnostic_audit", "rows", 1),
    ("diagnostic_audit", "model_parameters", "{bad-json}"),
])
def test_roles_identity_counts_and_hashes_cannot_be_misreported(result, field, column, bad):
    frame = getattr(result, field)
    index = frame.index[frame["stage"].eq("TRAIN_IN_SAMPLE")][0]
    frame.loc[index, column] = bad
    with pytest.raises(report.ModelReportError):
        report.validate_stage_diagnostics(result)


@pytest.mark.parametrize("field", _FIELDS)
def test_missing_columns_or_groups_do_not_become_complete_diagnostics(result, field):
    frame = getattr(result, field)
    altered = replace(result, **{field: frame.drop(columns="selection_usage")})
    with pytest.raises(report.ModelReportError, match="missing columns"):
        report.validate_stage_diagnostics(altered)
    altered = replace(result, **{field: frame.loc[~(frame["fold"].eq(0) & frame["stage"].eq("TRAIN_IN_SAMPLE"))].copy()})
    with pytest.raises(report.ModelReportError, match="fold/stage"):
        report.validate_stage_diagnostics(altered)


def test_independent_test_scores_cannot_be_replaced_by_training_scores(result):
    result.predictions.loc[result.predictions.index[0], "score"] += 2
    with pytest.raises(report.ModelReportError, match="SHA-256"):
        report.validate_stage_diagnostics(result)


@pytest.mark.parametrize("column,bad", [
    ("model_parameters", '{"different_model":true}'),
    ("feature_names", '["MOM20"]'), ("target_column", "OTHER_TARGET"),
    ("selection_metric", "prediction_ic_mean"), ("top_k", 2),
    ("minimum_cross_section", 3), ("random_seed", 43),
    ("saved_model_sha256", "a" * 64),
])
def test_one_stage_cannot_declare_another_valid_model_or_evaluation_protocol(result, column, bad):
    index = result.diagnostic_audit.index[result.diagnostic_audit["stage"].eq("VALIDATION_SELECTION")][0]
    result.diagnostic_audit.loc[index, column] = bad
    with pytest.raises(report.ModelReportError, match="protocol"):
        report.validate_stage_diagnostics(result)


def test_test_daily_must_match_independent_test_evaluation_before_writes(result, tmp_path):
    prediction = _prediction(result)
    daily = prediction.daily.copy(deep=True)
    daily.loc[0, "prediction_ic"] = -0.9
    with pytest.raises(report.ModelReportError, match="independent Test evaluation"):
        _write(result, tmp_path / "unpublished", PredictionEvaluation(daily=daily, summary=prediction.summary))
    assert not (tmp_path / "unpublished").exists()


def test_test_summary_must_match_independent_per_fold_evaluation(result, tmp_path):
    prediction = _prediction(result)
    summary = prediction.summary.copy(deep=True)
    summary.loc[summary["scope"].eq("FOLD"), "prediction_rank_ic_mean"] = -0.5
    with pytest.raises(report.ModelReportError, match="independent Test evaluation"):
        _write(result, tmp_path / "unpublished", PredictionEvaluation(daily=prediction.daily, summary=summary))
    assert not (tmp_path / "unpublished").exists()


def test_train_validation_maturity_cannot_be_faked_even_with_rehashed_predictions(result):
    frame = result.diagnostic_predictions
    chosen = frame.index[frame["fold"].eq(0) & frame["stage"].eq("TRAIN_IN_SAMPLE")][-1]
    boundary = result.fold_summary.loc[result.fold_summary["fold"].eq(0), "audit_validation_first_date"].iloc[0]
    frame.loc[chosen, "exit_date"] = boundary
    stage = frame.loc[frame["fold"].eq(0) & frame["stage"].eq("TRAIN_IN_SAMPLE")]
    index = result.diagnostic_audit.index[result.diagnostic_audit["fold"].eq(0) & result.diagnostic_audit["stage"].eq("TRAIN_IN_SAMPLE")][0]
    result.diagnostic_audit.loc[index, "label_max_exit"] = boundary
    result.diagnostic_audit.loc[index, "labeled_max_exit"] = boundary
    result.diagnostic_audit.loc[index, "prediction_sha256"] = diagnostic_frame_sha256(stage)
    with pytest.raises(report.ModelReportError, match="mature fold boundaries"):
        report.validate_stage_diagnostics(result)


def test_all_default_empty_is_explicit_legacy_not_fabricated_diagnostics(result, tmp_path):
    legacy = replace(result, **{field: pd.DataFrame() for field in _FIELDS})
    assert report.validate_stage_diagnostics(legacy) == {}
    summary = _write(legacy, tmp_path)
    assert "Stage diagnostics unavailable" in summary.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("stage_*"))


def test_stage_plot_keeps_nan_gaps_and_separate_fold_lines(result, tmp_path, monkeypatch):
    summary = result.diagnostic_summary.copy(deep=True)
    summary.loc[summary["fold"].eq(1), ["prediction_ic_mean", "prediction_rank_ic_mean"]] = np.nan
    captured = []
    real_save = report._save_figure
    def remember(figure, destination):
        for axis in figure.axes:
            captured.extend((line.get_label(), np.asarray(line.get_xdata()), np.asarray(line.get_ydata())) for line in axis.lines)
        real_save(figure, destination)
    monkeypatch.setattr(report, "_save_figure", remember)
    report._plot_stage_ic_comparison(summary, tmp_path / "stages.png")
    assert len(captured) == 6
    for _, x, y in captured:
        assert list(x) == sorted(result.fold_summary["fold"])
        assert np.isnan(y[np.flatnonzero(x == 1)[0]])
    assert not plt.get_fignums()


def test_stage_plot_with_no_valid_correlations_does_not_invent_zero(result, tmp_path, monkeypatch):
    summary = result.diagnostic_summary.copy(deep=True)
    summary[["prediction_ic_mean", "prediction_rank_ic_mean"]] = np.nan
    captured = []
    real_save = report._save_figure
    def remember(figure, destination):
        captured.extend(figure.axes)
        real_save(figure, destination)
    monkeypatch.setattr(report, "_save_figure", remember)
    report._plot_stage_ic_comparison(summary, tmp_path / "stages.png")
    assert all(not axis.lines for axis in captured)
    assert all(any("NaN" in text.get_text() for text in axis.texts) for axis in captured)
