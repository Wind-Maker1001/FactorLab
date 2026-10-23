"""Inference tests use artificial daily metrics, not empirical CSI500 results."""

from dataclasses import replace
import math

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from factorlab.metrics.robustness import (
    RobustnessError, RobustnessOptions, evaluate_model_robustness,
    holm_adjust, mean_uncertainty,
    validate_robustness_evaluation,
)


def _options(**changes):
    return replace(RobustnessOptions(), hac_lags=4, bootstrap_block_days=5,
                   bootstrap_repetitions=100, min_valid_days=10, **changes)


def _inputs():
    dates = pd.bdate_range("2022-01-03", periods=90)
    rng = np.random.default_rng(142)
    base = rng.normal(0.02, 0.1, len(dates))
    frames = []
    for offset, model in enumerate(("factor_score", "ridge", "lightgbm", "xgboost")):
        values = base + rng.normal(0.01 * offset, 0.02, len(dates))
        frames.append(pd.DataFrame({
            "date": dates, "fold": np.repeat([0, 1, 2], 30),
            "stage": "HISTORICAL_TEST", "model_name": model,
            "regime": np.where(np.arange(len(dates)) % 2 == 0, "BULL", "BEAR"),
            "prediction_rank_ic": values, "prediction_ic": values / 2,
            "top_bottom_spread": values / 100,
        }))
    return pd.concat(frames, ignore_index=True), dates


def test_hac_equals_independent_bartlett_matrix_calculation():
    values = np.array([2, -1, 3, 1, -2, 4, 1, 3, -1, 2, 0, 3], dtype=float)
    options = _options()
    result = mean_uncertainty(values, signal_dates=len(values), options=options)
    residual = values - values.mean()
    distance = np.abs(np.arange(len(values))[:, None] - np.arange(len(values)))
    kernel = np.maximum(0.0, 1.0 - distance / 5.0)
    expected = math.sqrt(residual @ kernel @ residual) / len(values)
    assert result["hac_standard_error"] == pytest.approx(expected)
    assert result["hac_p_value"] == pytest.approx(2 * norm.sf(abs(values.mean() / expected)))
    assert result["hac_ci_lower"] == pytest.approx(values.mean() - norm.ppf(.975) * expected)
    assert result["status"] == result["bootstrap_status"] == "OK"


def test_missing_calendar_slots_are_not_compacted_or_imputed():
    values = np.linspace(-1, 1, 30)
    values[[3, 5, 12, 18, 26]] = np.nan
    options = _options()
    result = mean_uncertainty(values, signal_dates=30, options=options)
    finite = np.isfinite(values)
    residual = np.where(finite, values - np.nanmean(values), 0)
    distance = np.abs(np.arange(30)[:, None] - np.arange(30))
    expected = math.sqrt(residual @ np.maximum(0., 1. - distance / 5.) @ residual) / finite.sum()
    assert result["mean"] == pytest.approx(np.nanmean(values))
    assert result["hac_standard_error"] == pytest.approx(expected)
    compact = mean_uncertainty(values[finite], signal_dates=int(finite.sum()), options=options)
    assert result["hac_standard_error"] != pytest.approx(compact["hac_standard_error"])
    assert result["coverage"] == 25 / 30
    assert result["missing_metric_dates"] == 5


@pytest.mark.parametrize("value", [0., .1, -2.])
def test_constant_sequence_has_no_invented_significance(value):
    result = mean_uncertainty(np.full(30, value), signal_dates=30, options=_options())
    assert result["status"] == "DEGENERATE_VARIANCE"
    assert math.isnan(result["hac_p_value"])
    assert math.isnan(result["hac_ci_lower"])
    assert result["bootstrap_status"] == "NOT_RUN"


def test_low_coverage_and_insufficient_dates_are_explicit():
    options = _options()
    assert mean_uncertainty([np.nan] * 30, signal_dates=30, options=options)["status"] == "INSUFFICIENT_DATES"
    values = np.arange(30, dtype=float)
    values[:10] = np.nan
    result = mean_uncertainty(values, signal_dates=30, options=options)
    assert result["status"] == "INSUFFICIENT_COVERAGE"
    assert result["mean"] == 19.5
    assert math.isnan(result["hac_p_value"])


def test_holm_matches_hand_values_and_keeps_unavailable_family_member():
    np.testing.assert_allclose(holm_adjust([.01, .04, .02]), [.03, .04, .04])
    np.testing.assert_allclose(holm_adjust([.01, np.nan, .04]), [.03, np.nan, .08], equal_nan=True)
    np.testing.assert_allclose(holm_adjust([.6, .5, .4]), [1., 1., 1.])


@pytest.mark.parametrize("changes", [
    {"hac_lags": 3}, {"hac_lags": True}, {"bootstrap_block_days": 4},
    {"bootstrap_repetitions": 99}, {"min_valid_days": 5}, {"min_coverage": 0},
    {"confidence_level": 1.}, {"confidence_level": np.inf},
    {"random_seed": -1}, {"random_seed": 2 ** 32}, {"holding_days": 0},
])
def test_invalid_protocol_rejected(changes):
    with pytest.raises(RobustnessError):
        RobustnessOptions(**changes)


def test_evaluation_is_paired_and_deterministic_without_changing_inputs_or_rng():
    frame, dates = _inputs()
    original = frame.copy(deep=True)
    np.random.seed(775)
    state = np.random.get_state()
    evaluation = evaluate_model_robustness(frame, dates, _options())
    shuffled = evaluate_model_robustness(frame.sample(frac=1, random_state=91), dates, _options())
    pd.testing.assert_frame_equal(evaluation.daily, shuffled.daily)
    pd.testing.assert_frame_equal(evaluation.summary, shuffled.summary)
    pd.testing.assert_frame_equal(evaluation.audit, shuffled.audit)
    pd.testing.assert_frame_equal(frame, original)
    after = np.random.get_state()
    assert state[0] == after[0] and np.array_equal(state[1], after[1]) and state[2:] == after[2:]
    primary = evaluation.summary.loc[evaluation.summary.primary_hypothesis]
    assert len(primary) == 3
    assert set(primary.model_name) == {"ridge", "lightgbm", "xgboost"}
    assert primary.holm_adjusted_p_value.notna().all()
    assert evaluation.summary.loc[~evaluation.summary.primary_hypothesis, "holm_adjusted_p_value"].isna().all()
    expected = frame.pivot(index="date", columns="model_name", values="prediction_rank_ic")
    row = primary.loc[primary.model_name.eq("ridge")].iloc[0]
    assert row["mean"] == pytest.approx((expected.ridge - expected.factor_score).mean())
    assert evaluation.protocol["automatic_model_selection"] is False
    assert len(evaluation.audit) == len(evaluation.summary)


def test_paired_missingness_is_joint_not_separate_means():
    frame, dates = _inputs()
    frame.loc[frame.model_name.eq("ridge") & frame.date.le(dates[4]), "prediction_rank_ic"] = np.nan
    evaluation = evaluate_model_robustness(frame, dates, _options())
    primary = evaluation.summary.loc[evaluation.summary.primary_hypothesis & evaluation.summary.model_name.eq("ridge")].iloc[0]
    assert primary.valid_dates == 85
    assert primary.missing_metric_dates == 5
    wide = frame.pivot(index="date", columns="model_name", values="prediction_rank_ic")
    assert primary["mean"] == pytest.approx((wide.ridge - wide.factor_score).mean())


def test_calendar_gaps_and_regime_exclusions_preserve_actual_trading_lags():
    frame, dates = _inputs()
    expanded = dates.union(pd.DatetimeIndex([pd.Timestamp("2022-01-08")]))
    result = evaluate_model_robustness(frame, expanded, _options())
    overall = result.summary.loc[(result.summary.scope == "OVERALL")]
    assert overall.calendar_slots.eq(91).all()
    assert overall.signal_dates.eq(90).all()
    assert overall.coverage.eq(1.).all()
    bull = result.summary.loc[(result.summary.scope == "REGIME") & result.summary.period.eq("BULL")]
    assert bull.signal_dates.eq(45).all()
    assert bull.calendar_slots.gt(45).all()


def test_optional_regime_absent_is_not_inferred():
    frame, dates = _inputs()
    result = evaluate_model_robustness(frame.drop(columns="regime"), dates, _options())
    assert "REGIME" not in set(result.summary.scope)


@pytest.mark.parametrize("mutation,match", [
    (lambda f: f.assign(stage="TRAIN_IN_SAMPLE"), "HISTORICAL_TEST"),
    (lambda f: f.assign(stage="VALIDATION_SELECTION"), "HISTORICAL_TEST"),
    (lambda f: pd.concat([f, f.iloc[:1]], ignore_index=True), "duplicate"),
    (lambda f: f.loc[~(f.model_name.eq("ridge") & f.date.eq(f.date.min()))], "identical"),
    (lambda f: f.assign(prediction_ic=True), "numeric"),
    (lambda f: f.assign(prediction_ic=np.inf), "infinity"),
    (lambda f: f.assign(prediction_ic=1.5), "between"),
    (lambda f: f.assign(prediction_ic=1j), "numeric"),
    (lambda f: f.assign(fold=True), "integers"),
    (lambda f: f.assign(regime=None), "regime"),
    (lambda f: f.assign(date=f.date + pd.Timedelta(hours=1)), "trading dates"),
    (lambda f: f.drop(columns="stage"), "missing columns"),
])
def test_unsafe_daily_inputs_rejected(mutation, match):
    frame, dates = _inputs()
    with pytest.raises(RobustnessError, match=match):
        evaluate_model_robustness(mutation(frame), dates, _options())


@pytest.mark.parametrize("mutation,match", [
    (lambda d: d[::-1], "increasing"),
    (lambda d: d.append(d[:1]), "unique"),
    (lambda d: d[:-1], "absent"),
    (lambda d: d.tz_localize("UTC"), "tz-naive"),
])
def test_unsafe_calendar_rejected(mutation, match):
    frame, dates = _inputs()
    with pytest.raises(RobustnessError, match=match):
        evaluate_model_robustness(frame, mutation(dates), _options())


def test_block_sampling_matches_independent_seeded_reference():
    values = np.sin(np.arange(30) / 2)
    values[[4, 18]] = np.nan
    options = _options()
    result = mean_uncertainty(values, signal_dates=30, options=options, identity="reference")
    rng = np.random.default_rng(result["bootstrap_seed"])
    starts = rng.integers(0, 26, size=(100, 6))
    indices = (starts[:, :, None] + np.arange(5)).reshape(100, 30)
    means = np.nanmean(values[indices], axis=1)
    lower, upper = np.quantile(means, [.025, .975], method="linear")
    assert result["bootstrap_ci_lower"] == pytest.approx(lower)
    assert result["bootstrap_ci_upper"] == pytest.approx(upper)
    assert indices.min() >= 0 and indices.max() < len(values)


def test_same_models_have_unavailable_paired_significance_not_infinity():
    frame, dates = _inputs()
    for metric in ("prediction_ic", "prediction_rank_ic", "top_bottom_spread"):
        values = frame.loc[frame.model_name.eq("factor_score"), metric].to_numpy()
        for model in ("ridge", "lightgbm", "xgboost"):
            frame.loc[frame.model_name.eq(model), metric] = values
    result = evaluate_model_robustness(frame, dates, _options())
    primary = result.summary.loc[result.summary.primary_hypothesis]
    assert primary.status.eq("DEGENERATE_VARIANCE").all()
    assert primary.holm_adjusted_p_value.isna().all()
    assert primary.holm_reject.isna().all()


def test_nullable_missing_stage_is_not_silently_accepted():
    frame, dates = _inputs()
    frame["stage"] = frame.stage.astype("string")
    frame.loc[0, "stage"] = pd.NA
    with pytest.raises(RobustnessError, match="HISTORICAL_TEST"):
        evaluate_model_robustness(frame, dates, _options())


@pytest.mark.parametrize("fold", [np.inf, np.nan, 1j, -1, 1e40, 1.5, float(2 ** 63)])
def test_bad_fold_type_range_and_finite_value_rejected(fold):
    frame, dates = _inputs()
    frame["fold"] = fold
    with pytest.raises(RobustnessError, match="integers"):
        evaluate_model_robustness(frame, dates, _options())


@pytest.mark.parametrize("p,size", [([.1], True), ([.1, .2], 1), ([1.1], 3), ([-.1], 3), ([np.inf], 3), ([True], 3)])
def test_invalid_holm_inputs_rejected(p, size):
    with pytest.raises(RobustnessError):
        holm_adjust(p, size)


def test_numpy_protocol_scalars_are_normalized_for_json_and_determinism():
    options = replace(RobustnessOptions(), random_seed=np.int64(42), min_coverage=np.float64(.8))
    frame, dates = _inputs()
    result = evaluate_model_robustness(frame, dates, options)
    validate_robustness_evaluation(result)
    assert type(result.protocol["options"]["random_seed"]) is int
    assert type(result.protocol["options"]["min_coverage"]) is float


def test_positive_serial_dependence_changes_hac_standard_error():
    values = np.sin(np.arange(120) / 12.)
    options = _options()
    result = mean_uncertainty(values, signal_dates=120, options=options)
    iid_se = np.std(values, ddof=0) / np.sqrt(120)
    assert result["hac_standard_error"] > iid_se


def test_sparse_calendar_can_have_too_few_valid_bootstrap_replicates():
    values = np.full(2000, np.nan)
    values[:5] = np.arange(5)
    values[-5:] = np.arange(5)
    result = mean_uncertainty(values, signal_dates=10, options=_options(), identity="sparse-ends")
    assert result["status"] == "OK"
    assert result["bootstrap_status"] == "INSUFFICIENT_VALID_REPLICATES"
    assert math.isnan(result["bootstrap_ci_lower"])


def test_vectorized_bootstrap_batches_are_seed_reproducible():
    values = np.sin(np.arange(70))
    options = replace(_options(), bootstrap_repetitions=259)
    result = mean_uncertainty(values, signal_dates=70, options=options)
    again = mean_uncertainty(values, signal_dates=70, options=options)
    assert result == again


def test_near_one_confidence_does_not_generate_infinite_interval():
    options = replace(_options(), confidence_level=np.nextafter(1., 0.))
    result = mean_uncertainty(np.sin(np.arange(30)), signal_dates=30, options=options)
    assert math.isfinite(result["hac_ci_lower"]) and math.isfinite(result["hac_ci_upper"])


