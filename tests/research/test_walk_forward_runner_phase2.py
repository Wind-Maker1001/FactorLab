"""Train/validation/test integrity tests for the Phase 2 runner."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import factorlab.validation.walk_forward as walk_forward_module
from factorlab.models.ridge import RidgeResearchModel
from factorlab.validation.splitter import ExpandingWalkForwardSplitter
from factorlab.validation.walk_forward import (
    WalkForwardRunError,
    WalkForwardRunner,
)


def _dataset(start="2020-01-02", end="2021-06-30", symbols=6):
    dates = pd.bdate_range(start, end)
    rows = []
    for date_number, date in enumerate(dates):
        market_wave = np.sin(date_number / 11.0)
        entry_date = (
            dates[date_number + 1] if date_number + 1 < len(dates) else pd.NaT
        )
        exit_date = (
            dates[date_number + 6] if date_number + 6 < len(dates) else pd.NaT
        )
        for symbol_number in range(symbols):
            factor_1 = (symbol_number - 2.5) / 2.5 + market_wave * 0.05
            factor_2 = np.cos(date_number / 17.0 + symbol_number)
            target = 0.025 * factor_1 - 0.01 * factor_2
            rows.append(
                {
                    "date": date,
                    "symbol": "%06d.SZ" % (symbol_number + 1),
                    "REV1": factor_1,
                    "MOM20": factor_2,
                    "EXCESS_RET5": target,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "MARKET_REGIME": "BULL" if market_wave >= 0 else "BEAR",
                }
            )
    return dates, pd.DataFrame(rows)


def _splitter(**overrides):
    options = {
        "initial_train_months": 4,
        "validation_months": 2,
        "test_months": 2,
        "step_months": 2,
        "purge_days": 5,
        "embargo_days": 5,
    }
    options.update(overrides)
    return ExpandingWalkForwardSplitter(**options)


def test_ridge_candidates_fit_train_only_and_selected_model_is_not_refit(monkeypatch):
    dates, frame = _dataset()

    class SpyRidge(RidgeResearchModel):
        fit_calls = 0
        predict_calls = 0

        def fit(self, X, y=None):
            type(self).fit_calls += 1
            return super().fit(X, y)

        def predict(self, X):
            type(self).predict_calls += 1
            return super().predict(X)

    monkeypatch.setattr(walk_forward_module, "RidgeResearchModel", SpyRidge)
    runner = WalkForwardRunner(
        _splitter(),
        model_name="ridge",
        feature_names=["REV1", "MOM20"],
        ridge_alphas=[0.01, 1.0, 100.0],
        top_k=2,
        minimum_cross_section=4,
    )

    result = runner.run(frame, calendar=dates)
    folds = len(result.fold_summary)

    assert SpyRidge.fit_calls == folds * 3
    assert SpyRidge.predict_calls == folds * 5  # 3 validation + 1 test + 1 Train diagnostic
    assert (result.fold_summary["fit_scope"] == "train_only").all()
    assert (result.fold_summary["purged_date_count"] == 5).all()
    assert (result.fold_summary["embargoed_date_count"] == 5).all()
    assert result.fold_summary["purged_dates"].str.len().gt(0).all()
    assert result.fold_summary["embargoed_dates"].str.len().gt(0).all()
    assert (
        result.fold_summary["train_max_exit_date"]
        < result.fold_summary["audit_validation_first_date"]
    ).all()
    assert (
        result.fold_summary["validation_max_exit_date"]
        < result.fold_summary["audit_test_first_date"]
    ).all()
    assert {
        "train_overlap_rows_removed",
        "validation_overlap_rows_removed",
        "incomplete_label_rows_removed",
        "train_window_end_exclusive",
        "validation_window_end_exclusive",
        "test_window_end_exclusive",
    }.issubset(result.fold_summary.columns)
    assert (result.validation_metrics["fit_scope"] == "train_only").all()
    assert len(result.validation_metrics) == folds * 3
    assert len(result.coefficients) == folds * 2
    assert not result.predictions.duplicated(["date", "symbol"]).any()
    assert result.predictions.groupby("date")["rank"].apply(list).map(
        lambda values: values == list(range(1, 7))
    ).all()


def test_ridge_alpha_and_test_scores_do_not_depend_on_test_labels():
    dates, frame = _dataset()
    runner = WalkForwardRunner(
        _splitter(),
        model_name="ridge",
        feature_names=["REV1", "MOM20"],
        ridge_alphas=[0.01, 1.0, 100.0],
        top_k=2,
        minimum_cross_section=4,
    )
    baseline = runner.run(frame, calendar=dates)
    mutated = frame.copy()
    final_fold = int(baseline.predictions["fold"].max())
    test_keys = baseline.predictions.loc[
        baseline.predictions["fold"] == final_fold, ["date", "symbol"]
    ]
    test_index = mutated.set_index(["date", "symbol"]).index.isin(
        pd.MultiIndex.from_frame(test_keys)
    )
    mutated.loc[test_index, "EXCESS_RET5"] = (
        np.arange(test_index.sum(), dtype=float) * 1000.0
    )

    rerun = runner.run(mutated, calendar=dates)

    assert baseline.fold_summary.loc[
        baseline.fold_summary["fold"] == final_fold, "selected_alpha"
    ].tolist() == rerun.fold_summary.loc[
        rerun.fold_summary["fold"] == final_fold, "selected_alpha"
    ].tolist()
    pd.testing.assert_series_equal(
        baseline.predictions.loc[
            baseline.predictions["fold"] == final_fold, "score"
        ].reset_index(drop=True),
        rerun.predictions.loc[
            rerun.predictions["fold"] == final_fold, "score"
        ].reset_index(drop=True),
    )


def test_factor_score_runner_outputs_only_test_predictions_and_no_coefficients():
    dates, frame = _dataset(end="2020-12-31")
    runner = WalkForwardRunner(
        _splitter(step_months=8),
        model_name="factor_score",
        feature_names=["REV1", "MOM20"],
        factor_weights={"REV1": 1.0, "MOM20": -0.25},
        top_k=2,
        minimum_cross_section=4,
    )

    result = runner.run(frame, calendar=dates)

    assert set(result.predictions["model_name"]) == {"factor_score"}
    assert result.predictions["date"].min() == result.fold_summary.loc[0, "test_start"]
    assert result.predictions["date"].max() == result.fold_summary.loc[0, "test_end"]
    assert result.coefficients.empty
    assert result.fold_summary.loc[0, "selected_alpha"] is None


def test_models_can_be_persisted_per_fold_with_training_only_state(tmp_path):
    dates, frame = _dataset(end="2020-12-31")
    output = tmp_path / "models"
    runner = WalkForwardRunner(
        _splitter(step_months=8),
        model_name="ridge",
        feature_names=["REV1", "MOM20"],
        ridge_alphas=[1.0],
        top_k=2,
        minimum_cross_section=4,
        model_directory=output,
    )

    result = runner.run(frame, calendar=dates)

    assert result.model_paths == (output / "fold_000_ridge.pkl",)
    assert all(Path(path).is_file() for path in result.model_paths)
    restored = RidgeResearchModel.load(result.model_paths[0])
    assert restored.feature_names == ("REV1", "MOM20")
    assert result.fold_summary.loc[0, "model_path"] == str(result.model_paths[0])


def test_missing_labels_are_excluded_from_fit_but_preserved_in_test_output():
    dates, frame = _dataset(end="2020-12-31")
    frame.loc[0, "EXCESS_RET5"] = np.nan
    test_day = pd.Timestamp("2020-07-02")
    frame.loc[
        (frame["date"] == test_day) & (frame["symbol"] == "000001.SZ"),
        "EXCESS_RET5",
    ] = np.nan
    runner = WalkForwardRunner(
        _splitter(step_months=8),
        model_name="ridge",
        feature_names=["REV1", "MOM20"],
        ridge_alphas=[1.0],
        top_k=2,
        minimum_cross_section=4,
    )

    result = runner.run(frame, calendar=dates)

    assert result.fold_summary.loc[0, "train_labeled_rows"] < result.fold_summary.loc[
        0, "train_rows"
    ]
    missing_test = result.predictions.loc[
        (result.predictions["date"] == test_day)
        & (result.predictions["symbol"] == "000001.SZ")
    ]
    assert len(missing_test) == 1
    assert missing_test["EXCESS_RET5"].isna().all()
    assert missing_test["score"].notna().all()


def test_overlapping_test_windows_and_duplicate_source_rows_fail_fast():
    dates, frame = _dataset()
    overlapping = WalkForwardRunner(
        _splitter(test_months=3, step_months=1),
        model_name="ridge",
        feature_names=["REV1", "MOM20"],
        ridge_alphas=[1.0],
    )
    with pytest.raises(WalkForwardRunError, match="test windows overlap"):
        overlapping.run(frame, calendar=dates)

    duplicate = pd.concat([frame.iloc[[0]], frame], ignore_index=True).sort_values(
        ["date", "symbol"], kind="mergesort"
    )
    with pytest.raises(WalkForwardRunError, match=r"duplicate \(date, symbol\)"):
        overlapping.run(duplicate, calendar=dates)


def test_runner_validates_columns_weights_and_finite_values():
    dates, frame = _dataset(end="2020-12-31")
    with pytest.raises(WalkForwardRunError, match="exactly match"):
        WalkForwardRunner(
            _splitter(),
            model_name="factor_score",
            feature_names=["REV1", "MOM20"],
            factor_weights={"REV1": 1.0},
        )

    runner = WalkForwardRunner(
        _splitter(step_months=8),
        model_name="ridge",
        feature_names=["REV1", "MOM20"],
        ridge_alphas=[1.0],
    )
    with pytest.raises(WalkForwardRunError, match="missing required columns"):
        runner.run(frame.drop(columns="MOM20"), calendar=dates)
    frame.loc[0, "REV1"] = np.inf
    with pytest.raises(WalkForwardRunError, match="infinity"):
        runner.run(frame, calendar=dates)

    with pytest.raises(WalkForwardRunError, match="positive"):
        WalkForwardRunner(
            _splitter(),
            model_name="ridge",
            feature_names=["REV1", "MOM20"],
            ridge_alphas=[0.0],
        )

    frame.loc[0, "REV1"] = 0.0
    with pytest.raises(WalkForwardRunError, match="label-window audit columns"):
        runner.run(
            frame.drop(columns=["entry_date", "exit_date"]),
            calendar=dates,
        )


def test_runner_rejects_target_or_audit_columns_as_features():
    with pytest.raises(WalkForwardRunError, match="target or reserved"):
        WalkForwardRunner(
            _splitter(),
            model_name="ridge",
            feature_names=["EXCESS_RET5"],
            ridge_alphas=[1.0],
        )

    dates, frame = _dataset(end="2020-12-31")
    runner = WalkForwardRunner(
        _splitter(step_months=8),
        model_name="ridge",
        feature_names=["REV1"],
        ridge_alphas=[1.0],
    )
    with pytest.raises(WalkForwardRunError, match="target or reserved"):
        runner.run(
            frame,
            calendar=dates,
            entry_date_col="REV1",
            exit_date_col="exit_date",
        )


def test_custom_label_audit_columns_are_preserved_under_canonical_output_names():
    dates, frame = _dataset(end="2020-12-31")
    frame = frame.rename(
        columns={"entry_date": "label_entry", "exit_date": "label_exit"}
    )
    runner = WalkForwardRunner(
        _splitter(step_months=8),
        model_name="ridge",
        feature_names=["REV1", "MOM20"],
        ridge_alphas=[1.0],
        top_k=2,
        minimum_cross_section=4,
    )

    result = runner.run(
        frame,
        calendar=dates,
        entry_date_col="label_entry",
        exit_date_col="label_exit",
    )

    assert {"entry_date", "exit_date"}.issubset(result.predictions.columns)
    assert "label_entry" not in result.predictions
    assert "label_exit" not in result.predictions
    assert (result.predictions["date"] < result.predictions["entry_date"]).all()
    assert (result.predictions["entry_date"] < result.predictions["exit_date"]).all()
