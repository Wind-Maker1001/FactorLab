"""Synthetic-only diagnostics must not change the walk-forward experiment."""

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from factorlab.metrics.prediction import evaluate_predictions
from factorlab.models.factor_score import FactorScoreModel
from factorlab.models.lightgbm import LightGBMResearchModel
from factorlab.models.ridge import RidgeResearchModel
from factorlab.models.xgboost import XGBoostResearchModel
from factorlab.validation import ExpandingWalkForwardSplitter
from factorlab.validation import walk_forward as wf


def _dataset():
    dates = pd.bdate_range("2020-01-02", "2020-06-30")
    rows = []
    for offset, day in enumerate(dates):
        for number in range(6):
            rev = (number - 2.5) / 2.5 + np.sin(offset / 8.0) * 0.05
            momentum = np.cos(offset / 9.0 + number)
            rows.append({
                "date": day, "symbol": "%06d.SZ" % (number + 1),
                "REV1": rev, "MOM20": momentum,
                "EXCESS_RET5": 0.02 * rev - 0.01 * momentum,
                "entry_date": dates[offset + 1] if offset + 1 < len(dates) else pd.NaT,
                "exit_date": dates[offset + 6] if offset + 6 < len(dates) else pd.NaT,
                "MARKET_REGIME": "BULL" if offset % 20 < 10 else "BEAR",
            })
    frame = pd.DataFrame(rows)
    frame.loc[0, "EXCESS_RET5"] = np.nan
    return dates, frame


def _splitter():
    return ExpandingWalkForwardSplitter(
        initial_train_months=1, validation_months=1, test_months=1,
        step_months=2, purge_days=5, embargo_days=5,
    )


def _options(model_name):
    if model_name == "factor_score":
        return {"factor_weights": {"REV1": 1.0, "MOM20": -0.3}}
    if model_name == "ridge":
        return {"ridge_alphas": (0.1, 1.0, 10.0)}
    if model_name == "lightgbm":
        return {"lightgbm_candidates": (
            {"n_estimators": 4, "num_leaves": 3, "max_depth": 2, "min_child_samples": 2},
            {"n_estimators": 6, "num_leaves": 3, "max_depth": 2, "min_child_samples": 2},
        )}
    return {"xgboost_candidates": (
        {"n_estimators": 4, "max_depth": 2},
        {"n_estimators": 6, "max_depth": 2},
    )}


def _runner(model_name="ridge", **kwargs):
    return wf.WalkForwardRunner(
        _splitter(), model_name=model_name, feature_names=("REV1", "MOM20"),
        top_k=2, minimum_cross_section=4, regime_col="MARKET_REGIME",
        **_options(model_name), **kwargs,
    )


def _stage_predictions(result, fold, stage):
    if stage == "HISTORICAL_TEST":
        return result.predictions.loc[result.predictions["fold"].eq(fold)].assign(
            stage=stage, fit_scope="train_only",
            selection_usage=wf.DIAGNOSTIC_SELECTION_USAGE[stage],
        )
    return result.diagnostic_predictions.loc[
        result.diagnostic_predictions["fold"].eq(fold)
        & result.diagnostic_predictions["stage"].eq(stage)
    ]


def _metadata(frame, fold, model_name, stage):
    return frame.assign(fold=fold, model_name=model_name, stage=stage,
                        fit_scope="train_only", selection_usage=wf.DIAGNOSTIC_SELECTION_USAGE[stage])


@pytest.mark.parametrize("model_name", ["factor_score", "ridge", "lightgbm", "xgboost"])
def test_selected_models_predict_validation_once_test_once_and_full_train_once(monkeypatch, model_name):
    dates, frame = _dataset()
    base, attribute = {
        "factor_score": (FactorScoreModel, "FactorScoreModel"),
        "ridge": (RidgeResearchModel, "RidgeResearchModel"),
        "lightgbm": (LightGBMResearchModel, "LightGBMResearchModel"),
        "xgboost": (XGBoostResearchModel, "XGBoostResearchModel"),
    }[model_name]
    events = []

    class SpyModel(base):
        def fit(self, X, y=None):
            events.append((self, "fit", tuple(X.index), None))
            return super().fit(X, y)

        def predict(self, X):
            scores = super().predict(X)
            events.append((self, "predict", tuple(X.index), scores.copy(deep=True)))
            return scores

    monkeypatch.setattr(wf, attribute, SpyModel)
    result = _runner(model_name).run(frame, calendar=dates)
    folds = list(_splitter().split(frame, calendar=dates))
    candidate_count = {"factor_score": 1, "ridge": 3, "lightgbm": 2, "xgboost": 2}[model_name]
    assert sum(event[1] == "fit" for event in events) == len(folds) * candidate_count
    assert sum(event[1] == "predict" for event in events) == len(folds) * (candidate_count + 2)
    for fold in folds:
        train = frame.iloc[fold.train_indices]
        train_keys = tuple(train.index)
        fit_keys = tuple(train.index[train["EXCESS_RET5"].notna()])
        val_keys = tuple(frame.iloc[fold.validation_indices].index)
        test_keys = tuple(frame.iloc[fold.test_indices].index)
        fitted = [event[0] for event in events if event[1] == "fit" and event[2] == fit_keys]
        assert len(fitted) == candidate_count
        calls = [event for event in events if event[0] in fitted and event[1] == "predict"]
        assert sum(event[2] == val_keys for event in calls) == candidate_count
        assert sum(event[2] == test_keys for event in calls) == 1
        assert sum(event[2] == train_keys for event in calls) == 1
        chosen = next(event[0] for event in calls if event[2] == test_keys)
        assert [event[2] for event in calls if event[0] == chosen] == [val_keys, test_keys, train_keys]
        # Validation diagnostics must be those exact already-computed scores.
        cached_scores = next(event[3] for event in calls if event[0] == chosen and event[2] == val_keys)
        expected = pd.DataFrame({"symbol": frame.loc[cached_scores.index, "symbol"],
                                 "date": frame.loc[cached_scores.index, "date"], "score": cached_scores})
        observed = _stage_predictions(result, fold.fold, "VALIDATION_SELECTION")[["date", "symbol", "score"]]
        pd.testing.assert_frame_equal(
            observed.sort_values(["date", "symbol"]).reset_index(drop=True),
            expected[["date", "symbol", "score"]].sort_values(["date", "symbol"]).reset_index(drop=True),
        )
        assert train_keys != fit_keys  # The unlabeled row remains a scored diagnostic observation.
    assert "stage" not in result.predictions
    assert set(result.diagnostic_predictions["stage"]) == set(wf.DIAGNOSTIC_STAGES[:2])


@pytest.mark.parametrize("model_name", ["factor_score", "ridge", "lightgbm", "xgboost"])
def test_each_stage_is_audited_and_metrics_are_computed_independently_per_fold(tmp_path, model_name):
    dates, frame = _dataset()
    before = frame.copy(deep=True)
    result = _runner(model_name, model_directory=tmp_path / model_name).run(frame, calendar=dates)
    folds = list(_splitter().split(frame, calendar=dates))
    assert list(result.diagnostic_audit) == list(wf.DIAGNOSTIC_AUDIT_COLUMNS)
    assert len(result.diagnostic_audit) == len(folds) * 3
    assert not result.diagnostic_audit.duplicated(["fold", "stage"]).any()
    assert result.diagnostic_predictions.duplicated(["date", "symbol", "stage"]).any()
    assert not result.diagnostic_predictions.duplicated(["fold", "date", "symbol", "stage"]).any()
    for fold in folds:
        for stage, indices, boundary in (
            ("TRAIN_IN_SAMPLE", fold.train_indices, fold.audit.validation_first_date),
            ("VALIDATION_SELECTION", fold.validation_indices, fold.audit.test_first_date),
            ("HISTORICAL_TEST", fold.test_indices, None),
        ):
            audit = result.diagnostic_audit.loc[
                result.diagnostic_audit["fold"].eq(fold.fold) & result.diagnostic_audit["stage"].eq(stage)
            ].iloc[0]
            predictions = _stage_predictions(result, fold.fold, stage)
            source = frame.iloc[indices][[
                "date", "symbol", "REV1", "MOM20", "EXCESS_RET5", "entry_date", "exit_date", "MARKET_REGIME",
            ]]
            fit_source = frame.iloc[fold.train_indices]
            fit_source = fit_source.loc[fit_source["EXCESS_RET5"].notna(), source.columns]
            assert audit["source_keys_sha256"] == wf.diagnostic_frame_sha256(source[["date", "symbol"]])
            assert audit["source_sha256"] == wf.diagnostic_frame_sha256(source)
            assert audit["fit_source_keys_sha256"] == wf.diagnostic_frame_sha256(fit_source[["date", "symbol"]])
            assert audit["fit_source_sha256"] == wf.diagnostic_frame_sha256(fit_source)
            assert audit["prediction_sha256"] == wf.diagnostic_frame_sha256(predictions)
            assert audit["rows"] == len(source)
            assert audit["labeled_rows"] == int(source["EXCESS_RET5"].notna().sum())
            assert audit["scored_rows"] == len(source)
            assert audit["signal_start"] == source["date"].min()
            assert audit["signal_end"] == source["date"].max()
            assert audit["label_min_entry"] == source["entry_date"].min()
            assert audit["label_max_entry"] == source["entry_date"].max()
            assert audit["label_min_exit"] == source["exit_date"].min()
            assert audit["label_max_exit"] == source["exit_date"].max()
            assert audit["labeled_max_exit"] == source.loc[source["EXCESS_RET5"].notna(), "exit_date"].max()
            if boundary is None:
                assert pd.isna(audit["maturity_boundary"])
            else:
                assert audit["maturity_boundary"] == boundary
                assert audit["label_max_exit"] < boundary
            assert audit["fit_scope"] == "train_only"
            assert audit["selection_usage"] == wf.DIAGNOSTIC_SELECTION_USAGE[stage]
            assert audit["saved_model_sha256"] == hashlib.sha256(result.model_paths[fold.fold].read_bytes()).hexdigest()
            assert json.loads(audit["feature_names"]) == ["REV1", "MOM20"]
            parameters = json.loads(audit["model_parameters"])
            if model_name == "factor_score":
                assert parameters["weights"] == {"REV1": 1.0, "MOM20": -0.3}
            else:
                assert parameters["random_seed"] == 42
            independent = evaluate_predictions(predictions, top_k=2, minimum_cross_section=4,
                                               regime_col="MARKET_REGIME")
            daily = result.diagnostic_daily.loc[
                result.diagnostic_daily["fold"].eq(fold.fold) & result.diagnostic_daily["stage"].eq(stage)
            ].reset_index(drop=True)
            summary = result.diagnostic_summary.loc[
                result.diagnostic_summary["fold"].eq(fold.fold) & result.diagnostic_summary["stage"].eq(stage)
            ].reset_index(drop=True)
            pd.testing.assert_frame_equal(daily, _metadata(independent.daily, fold.fold, model_name, stage))
            pd.testing.assert_frame_equal(summary, _metadata(independent.summary, fold.fold, model_name, stage))
            assert audit["daily_sha256"] == wf.diagnostic_frame_sha256(daily)
            assert audit["summary_sha256"] == wf.diagnostic_frame_sha256(summary)
            assert len(summary.loc[summary["scope"].eq("OVERALL")]) == 1
            assert not summary["period"].eq("POOLED").any()
    pd.testing.assert_frame_equal(frame, before)


@pytest.mark.parametrize("model_name", ["factor_score", "ridge", "lightgbm", "xgboost"])
def test_last_test_label_mutation_cannot_change_train_validation_or_chosen_parameters(model_name):
    dates, frame = _dataset()
    runner = _runner(model_name)
    baseline = runner.run(frame, calendar=dates)
    last_fold = baseline.predictions["fold"].max()
    test_keys = pd.MultiIndex.from_frame(baseline.predictions.loc[
        baseline.predictions["fold"].eq(last_fold), ["date", "symbol"]])
    changed = frame.copy(deep=True)
    mask = pd.MultiIndex.from_frame(changed[["date", "symbol"]]).isin(test_keys)
    changed.loc[mask, "EXCESS_RET5"] = np.arange(mask.sum(), dtype=float)[::-1] * 1000.0
    rerun = runner.run(changed, calendar=dates)
    assert baseline.fold_summary["selected_parameters"].tolist() == rerun.fold_summary["selected_parameters"].tolist()
    pd.testing.assert_frame_equal(baseline.validation_metrics, rerun.validation_metrics)
    pd.testing.assert_frame_equal(baseline.diagnostic_predictions, rerun.diagnostic_predictions)
    for field in ("diagnostic_daily", "diagnostic_summary", "diagnostic_audit"):
        left, right = getattr(baseline, field), getattr(rerun, field)
        pd.testing.assert_frame_equal(left.loc[left["stage"].ne("HISTORICAL_TEST")].reset_index(drop=True),
                                      right.loc[right["stage"].ne("HISTORICAL_TEST")].reset_index(drop=True))
    pd.testing.assert_frame_equal(baseline.predictions[["date", "symbol", "fold", "score", "rank"]],
                                  rerun.predictions[["date", "symbol", "fold", "score", "rank"]])
    assert baseline.diagnostic_audit["saved_model_sha256"].isna().all()
    assert baseline.diagnostic_audit["model_path"].isna().all()


def test_unlabeled_train_validation_rows_are_not_removed_from_coverage():
    dates, frame = _dataset()
    fold = next(_splitter().split(frame, calendar=dates))
    missing_validation = int(fold.validation_indices[0])
    frame.loc[missing_validation, "EXCESS_RET5"] = np.nan
    result = _runner("factor_score").run(frame, calendar=dates)
    for stage, position in (("TRAIN_IN_SAMPLE", 0), ("VALIDATION_SELECTION", missing_validation)):
        original = frame.iloc[position]
        predictions = _stage_predictions(result, 0, stage)
        row = predictions.loc[predictions["date"].eq(original["date"]) & predictions["symbol"].eq(original["symbol"])].iloc[0]
        assert pd.isna(row["EXCESS_RET5"]) and pd.notna(row["score"])
        daily = result.diagnostic_daily.loc[
            result.diagnostic_daily["fold"].eq(0) & result.diagnostic_daily["stage"].eq(stage)
            & result.diagnostic_daily["date"].eq(original["date"])
        ].iloc[0]
        assert daily["universe_size"] == 6 and daily["observations"] == 5
        assert daily["coverage"] == pytest.approx(5 / 6)


def test_legacy_result_constructor_keeps_empty_diagnostics_and_test_only_contract():
    result = wf.WalkForwardResult(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), ())
    for name in ("diagnostic_predictions", "diagnostic_daily", "diagnostic_summary", "diagnostic_audit"):
        assert getattr(result, name).empty
    other = wf.WalkForwardResult(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), ())
    result.diagnostic_audit["unrelated"] = [1]
    assert other.diagnostic_audit.empty


def test_custom_source_and_label_names_are_bound_under_canonical_audit_keys():
    dates, source = _dataset()
    renamed = source.rename(columns={"date": "trade_day", "symbol": "ticker",
                                     "entry_date": "label_entry", "exit_date": "label_exit"})
    result = _runner("ridge", date_col="trade_day", symbol_col="ticker").run(
        renamed, calendar=dates, entry_date_col="label_entry", exit_date_col="label_exit",
    )
    assert {"date", "symbol", "entry_date", "exit_date"}.issubset(result.diagnostic_predictions)
    assert not {"trade_day", "ticker", "label_entry", "label_exit"}.intersection(result.diagnostic_predictions)
    audit = result.diagnostic_audit.iloc[0]
    stage = _stage_predictions(result, 0, "TRAIN_IN_SAMPLE")
    assert audit["source_keys_sha256"] == wf.diagnostic_frame_sha256(stage[["date", "symbol"]])


def test_hashes_bind_values_but_ignore_input_row_order():
    frame = pd.DataFrame({"date": pd.to_datetime(["2020-01-02", "2020-01-03"]),
                          "symbol": ["000001.SZ", "000002.SZ"], "score": [0.1, 0.2]})
    digest = wf.diagnostic_frame_sha256(frame)
    assert digest == wf.diagnostic_frame_sha256(frame.iloc[::-1])
    assert digest != wf.diagnostic_frame_sha256(frame.assign(score=[0.1, 0.3]))


@pytest.mark.parametrize("name", ["stage", "fit_scope", "selection_usage"])
@pytest.mark.parametrize("role", ["target_col", "regime_col"])
def test_new_stage_metadata_cannot_silently_replace_target_or_regime(name, role):
    options = {role: name}
    with pytest.raises(wf.WalkForwardRunError, match="reserved stage"):
        wf.WalkForwardRunner(_splitter(), model_name="ridge", feature_names=("REV1", "MOM20"), **options)
