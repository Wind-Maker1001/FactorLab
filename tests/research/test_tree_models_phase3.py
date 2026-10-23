"""Phase-3 tree-model and validation-only selection tests."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qbot.research.models import (
    LightGBMResearchModel,
    XGBoostResearchModel,
)
from qbot.research.validation import (
    ExpandingWalkForwardSplitter,
    WalkForwardRunError,
    WalkForwardRunner,
)


@pytest.mark.parametrize(
    ("model_class", "options"),
    [
        (
            LightGBMResearchModel,
            {
                "n_estimators": 12,
                "num_leaves": 7,
                "max_depth": 3,
                "min_child_samples": 2,
            },
        ),
        (
            XGBoostResearchModel,
            {"n_estimators": 12, "max_depth": 3},
        ),
    ],
)
def test_tree_models_fit_predict_export_and_round_trip(
    tmp_path, model_class, options
):
    index = pd.Index(range(80), name="row")
    X = pd.DataFrame(
        {
            "REV1": np.linspace(-1.0, 1.0, len(index)),
            "MOM20": np.sin(np.arange(len(index)) / 7.0),
        },
        index=index,
    )
    X.loc[3, "MOM20"] = np.nan
    y = pd.Series(
        0.03 * X["REV1"] - 0.01 * X["MOM20"].fillna(0.0),
        index=index,
    )
    model = model_class(
        feature_names=("REV1", "MOM20"),
        random_seed=42,
        n_jobs=1,
        **options
    ).fit(X, y)

    prediction = model.predict(X)
    assert prediction.index.equals(index)
    assert np.isfinite(prediction).all()
    assert model.imputer_.statistics_[1] == pytest.approx(
        X["MOM20"].median()
    )
    assert model.feature_importance.index.tolist() == ["REV1", "MOM20"]
    assert (model.feature_importance >= 0.0).all()

    path = tmp_path / (model_class.__name__ + ".pkl")
    model.save(path)
    restored = model_class.load(path)
    pd.testing.assert_series_equal(restored.predict(X), prediction)
    importance_path = tmp_path / (model_class.__name__ + "_importance.csv")
    restored.export_feature_importance(importance_path)
    assert pd.read_csv(importance_path)["feature"].tolist() == [
        "REV1",
        "MOM20",
    ]


def _walk_forward_data():
    dates = pd.bdate_range("2020-01-02", "2020-12-31")
    rows = []
    for date_number, signal_date in enumerate(dates):
        entry_date = (
            dates[date_number + 1] if date_number + 1 < len(dates) else pd.NaT
        )
        exit_date = (
            dates[date_number + 6] if date_number + 6 < len(dates) else pd.NaT
        )
        for symbol_number in range(8):
            rev = (symbol_number - 3.5) / 3.5
            momentum = np.sin(date_number / 11.0 + symbol_number / 5.0)
            rows.append(
                {
                    "date": signal_date,
                    "symbol": "%06d.SZ" % (symbol_number + 1),
                    "REV1": rev,
                    "MOM20": momentum,
                    "EXCESS_RET5": 0.02 * rev - 0.006 * momentum,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "MARKET_REGIME": (
                        "BULL" if date_number % 40 < 20 else "BEAR"
                    ),
                }
            )
    return dates, pd.DataFrame(rows)


def _splitter():
    return ExpandingWalkForwardSplitter(
        initial_train_months=4,
        validation_months=2,
        test_months=2,
        step_months=8,
        purge_days=5,
        embargo_days=5,
    )


@pytest.mark.parametrize(
    ("model_name", "candidate_argument", "candidates"),
    [
        (
            "lightgbm",
            "lightgbm_candidates",
            (
                {
                    "n_estimators": 8,
                    "num_leaves": 7,
                    "max_depth": 3,
                    "min_child_samples": 2,
                },
                {
                    "n_estimators": 12,
                    "num_leaves": 7,
                    "max_depth": 3,
                    "min_child_samples": 2,
                },
            ),
        ),
        (
            "xgboost",
            "xgboost_candidates",
            (
                {"n_estimators": 8, "max_depth": 2},
                {"n_estimators": 12, "max_depth": 3},
            ),
        ),
    ],
)
def test_tree_walk_forward_uses_validation_only_and_exports_importance(
    model_name, candidate_argument, candidates
):
    dates, frame = _walk_forward_data()
    runner = WalkForwardRunner(
        _splitter(),
        model_name=model_name,
        feature_names=("REV1", "MOM20"),
        regime_col="MARKET_REGIME",
        top_k=2,
        minimum_cross_section=4,
        random_seed=42,
        **{candidate_argument: candidates}
    )
    baseline = runner.run(frame, calendar=dates)
    assert len(baseline.fold_summary) == 1
    assert len(baseline.validation_metrics) == len(candidates)
    assert baseline.fold_summary.loc[0, "fit_scope"] == "train_only"
    assert baseline.fold_summary.loc[0, "selected_parameters"].startswith("{")
    assert len(baseline.feature_importance) == 2
    assert set(baseline.feature_importance["feature"]) == {"REV1", "MOM20"}
    assert baseline.feature_importance["normalized_importance"].sum() == pytest.approx(
        1.0
    )

    mutated = frame.copy()
    test_keys = pd.MultiIndex.from_frame(
        baseline.predictions[["date", "symbol"]]
    )
    changed = mutated.set_index(["date", "symbol"]).index.isin(test_keys)
    mutated.loc[changed, "EXCESS_RET5"] = np.linspace(
        -1000.0, 1000.0, changed.sum()
    )
    rerun = runner.run(mutated, calendar=dates)

    assert baseline.fold_summary["selected_parameters"].tolist() == (
        rerun.fold_summary["selected_parameters"].tolist()
    )
    pd.testing.assert_series_equal(
        baseline.predictions["score"].reset_index(drop=True),
        rerun.predictions["score"].reset_index(drop=True),
    )


def test_tree_runner_rejects_unknown_candidate_parameters():
    with pytest.raises(WalkForwardRunError, match="unknown parameters"):
        WalkForwardRunner(
            _splitter(),
            model_name="lightgbm",
            feature_names=("REV1", "MOM20"),
            lightgbm_candidates=({"future_test_tuning": True},),
        )

