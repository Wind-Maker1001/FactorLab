"""Negative controls preserve observation identity and inference missingness."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal, assert_series_equal

from factorlab.application.alpha_robustness import (
    ShuffledTrainingRunner,
    common_prediction_samples,
    delayed_scores,
    random_scores,
    shuffle_training_labels,
)
from factorlab.metrics.robustness import RobustnessOptions, holm_adjust, mean_uncertainty


@pytest.fixture
def training():
    return pd.DataFrame({
        "date": np.repeat(pd.bdate_range("2024-01-02", periods=3), 8),
        "symbol": list("HGFEDCBA") * 3,
        "EXCESS_RET5": np.tile([1., 2., np.nan, 4., 5., 6., 7., 8.], 3),
        "feature": np.arange(24, dtype=float),
        "receipt": [f"row-{i}" for i in range(24)],
    }, index=np.arange(100, 124))


def test_shuffle_preserves_date_multisets_nan_slots_and_other_columns(training):
    original = training.copy(deep=True)
    result = shuffle_training_labels(training, seed=17)
    assert_frame_equal(result, shuffle_training_labels(training, seed=17))
    assert_frame_equal(training, original)
    assert_frame_equal(result.drop(columns="EXCESS_RET5"), original.drop(columns="EXCESS_RET5"))
    assert_series_equal(result.EXCESS_RET5.isna(), original.EXCESS_RET5.isna())
    for date, group in original.groupby("date"):
        np.testing.assert_array_equal(
            np.sort(result.loc[result.date.eq(date), "EXCESS_RET5"].dropna()),
            np.sort(group.EXCESS_RET5.dropna()),
        )
    assert not result.EXCESS_RET5.equals(original.EXCESS_RET5)
    reordered = shuffle_training_labels(training.iloc[::-1], seed=17).sort_index()
    assert_frame_equal(result.sort_index(), reordered)
    assert not result.EXCESS_RET5.equals(shuffle_training_labels(training, seed=29).EXCESS_RET5)


def test_fold_frames_shuffles_only_training_via_real_partition_hook(training):
    # Only partitioning is exercised; fitting and splitter construction are unrelated.
    runner = object.__new__(ShuffledTrainingRunner)
    runner.shuffle_seed = 17
    runner.target_col = "custom_target"
    prepared = training.rename(columns={"EXCESS_RET5": "custom_target"})
    original = prepared.copy(deep=True)
    folds = [
        SimpleNamespace(train_indices=np.arange(8), validation_indices=np.arange(8, 16),
                        test_indices=np.arange(16, 24)),
        SimpleNamespace(train_indices=np.arange(16), validation_indices=np.arange(16, 20),
                        test_indices=np.arange(20, 24)),
    ]
    partitions = list(runner._fold_frames(prepared, folds))
    assert len(partitions) == 2
    for expected_fold, (fold, train, validation, test) in zip(folds, partitions):
        assert fold is expected_fold
        assert_frame_equal(train, shuffle_training_labels(
            prepared.iloc[fold.train_indices], seed=17, target="custom_target"))
        assert_frame_equal(validation, prepared.iloc[fold.validation_indices])
        assert_frame_equal(test, prepared.iloc[fold.test_indices])
        assert not train.custom_target.equals(prepared.iloc[fold.train_indices].custom_target)
    assert_frame_equal(prepared, original)


def test_test_label_changes_cannot_change_shuffled_training(training):
    runner = object.__new__(ShuffledTrainingRunner)
    runner.shuffle_seed = 17
    runner.target_col = 'EXCESS_RET5'
    fold = SimpleNamespace(train_indices=np.arange(8), validation_indices=np.arange(8, 16),
                           test_indices=np.arange(16, 24))
    modified = training.copy()
    modified.loc[modified.index[16:], 'EXCESS_RET5'] = 1e6
    before = next(runner._fold_frames(training, [fold]))
    after = next(runner._fold_frames(modified, [fold]))
    assert_frame_equal(before[1], after[1])
    assert_frame_equal(before[2], after[2])
    assert_frame_equal(after[3], modified.iloc[16:])


def test_random_scores_preserve_identity_and_nonfinite_slots():
    frame = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-03", "2024-01-02", "2024-01-02", "2024-01-03", "2024-01-04"]),
        "symbol": ["B", "B", "A", "A", "A"],
        "score": [2., np.nan, 1., np.inf, -np.inf],
        "fold": [2, 1, 1, 2, 3],
        "EXCESS_RET5": [.2, .3, .4, .5, .6],
    })
    original = frame.copy(deep=True)
    result = random_scores(frame, seed=31)
    expected = frame.sort_values(["date", "symbol"]).reset_index(drop=True)
    assert_frame_equal(result.drop(columns="score"), expected.drop(columns="score"))
    finite = np.isfinite(expected.score)
    assert_series_equal(result.loc[~finite, "score"], expected.loc[~finite, "score"])
    np.testing.assert_array_equal(result.loc[finite, "score"], np.random.default_rng(31).normal(size=2))
    assert_frame_equal(result, random_scores(frame.iloc[::-1], seed=31))
    assert not result.score.equals(random_scores(frame, seed=32).score)
    assert_frame_equal(frame, original)


def test_delayed_scores_use_full_calendar_across_oos_gap_and_keep_destination_outcomes():
    calendar = pd.bdate_range("2024-01-02", periods=8)
    # Calendar sessions 2 and 3 have no OOS predictions; they still consume delay.
    positions = [0, 1, 4, 5, 6, 7]
    frame = pd.DataFrame([
        {"date": calendar[i], "symbol": symbol, "score": float(10 * i + j),
         "fold": i // 4, "EXCESS_RET5": float(100 + 10 * i + j),
         "receipt": f"destination-{i}-{symbol}"}
        for i in positions for j, symbol in enumerate(["A", "B"])
    ])
    original = frame.copy(deep=True)
    result = delayed_scores(frame, calendar, sessions=4)
    assert set(result.date) == {calendar[4], calendar[5]}
    assert len(result) == 4
    for row in result.itertuples(index=False):
        destination = frame.loc[frame.date.eq(row.date) & frame.symbol.eq(row.symbol)].iloc[0]
        source_date = calendar[calendar.get_loc(row.date) - 4]
        source = frame.loc[frame.date.eq(source_date) & frame.symbol.eq(row.symbol)].iloc[0]
        assert row.source_date == source_date
        assert row.score == source.score
        assert row.source_fold == source.fold
        assert row.fold == destination.fold
        assert row.EXCESS_RET5 == destination.EXCESS_RET5
        assert row.receipt == destination.receipt
    assert_frame_equal(frame, original)


@pytest.mark.parametrize("sessions", [0, -1])
def test_delayed_scores_reject_nonpositive_delay(sessions):
    with pytest.raises(ValueError, match="sessions must be positive"):
        delayed_scores(pd.DataFrame(), pd.bdate_range("2024-01-02", periods=2), sessions=sessions)


def test_common_prediction_samples_intersect_finite_keys_not_row_positions():
    date = pd.Timestamp("2024-01-02")
    first = pd.DataFrame({"date": [date] * 5, "symbol": list("ABCDE"),
                          "score": [1., 2., np.nan, 4., 5.], "fold": [1] * 5,
                          "EXCESS_RET5": [.1, .2, .3, .4, np.nan]})
    second = first.iloc[::-1].copy()
    second["score"] = [50., np.inf, 30., 20., 10.]
    second["fold"] = 2
    originals = {"baseline": first.copy(deep=True), "control": second.copy(deep=True)}
    result = common_prediction_samples({"baseline": first, "control": second})
    for name, source in originals.items():
        expected = source.loc[source.symbol.isin(list("ABE"))].sort_values(["date", "symbol"]).reset_index(drop=True)
        assert_frame_equal(result[name], expected)
    assert_frame_equal(first, originals["baseline"])
    assert_frame_equal(second, originals["control"])
    assert np.isnan(result["baseline"].iloc[-1].EXCESS_RET5)


def test_common_prediction_samples_can_have_no_shared_finite_keys():
    first = pd.DataFrame({"date": pd.to_datetime(["2024-01-02"]), "symbol": ["A"], "score": [1.]})
    second = first.assign(score=np.nan)
    result = common_prediction_samples({"first": first, "second": second})
    assert all(frame.empty for frame in result.values())
    assert all(list(frame.columns) == list(first.columns) for frame in result.values())


def test_cost_accounting_rejects_fee_credited_as_income():
    from factorlab.application.alpha_robustness import cost_accounting
    from factorlab.backtest.fees import FeeSchedule
    dates = pd.bdate_range('2024-01-02', periods=2)
    schedule = FeeSchedule.a_share_default()
    costs = [schedule.calculate(dates[0], 'BUY', 1000.).total,
             schedule.calculate(dates[1], 'SELL', 1100.).total]
    cash = 10000 + np.cumsum(np.array([-1000., 1100.]) - costs)
    trades = pd.DataFrame({'execution_date': dates, 'side': ['BUY', 'SELL'],
        'trade_notional': [1000., 1100.], 'transaction_cost': costs, 'cash_after': cash})
    nav = pd.DataFrame({'date': dates, 'cash': cash, 'transaction_cost': costs})
    assert cost_accounting(nav, trades, initial_cash=10000., schedule=schedule) == 0.
    incorrect = trades.copy()
    incorrect['cash_after'] = 10000 + np.cumsum(np.array([-1000., 1100.]) + costs)
    with pytest.raises(AssertionError):
        cost_accounting(nav, incorrect, initial_cash=10000., schedule=schedule)


def test_prediction_inference_preserves_unscheduled_calendar_slots():
    from factorlab.application.alpha_robustness import calendar_paired_inference
    from factorlab.evaluation.research import EvaluationContext, EvaluationResult
    calendar = pd.bdate_range('2024-01-02', periods=10)
    observed = calendar[[0, 1, 2, 7, 8, 9]]
    context = EvaluationContext('data', 'universe', 'label', 'labels', 'benchmark',
        tuple(observed.strftime('%Y-%m-%d')), 1)
    results = [EvaluationResult(name, 'prediction', context, {},
        {'daily': pd.DataFrame({'date': observed, 'prediction_rank_ic': values})}, {})
        for name, values in [('base', np.zeros(6)), ('control', np.arange(6) / 10)]]
    options = RobustnessOptions(holding_days=1, hac_lags=1, bootstrap_block_days=2,
        bootstrap_repetitions=100, min_valid_days=4, min_coverage=.8)
    row = calendar_paired_inference(results, calendar=calendar, baseline='base',
        metric='prediction_rank_ic', options=options).iloc[0]
    assert row.calendar_slots == 10
    assert row.valid_dates == 6
    assert row.missing_metric_dates == 4
    assert row.status == 'INSUFFICIENT_COVERAGE'
    assert np.isnan(row.holm_p_value)


def test_artificial_strong_daily_contrast_has_finite_uncertainty():
    values = 1. + np.tile([-.1, .1, -.05, .05], 25)
    options = RobustnessOptions(holding_days=5, hac_lags=5, bootstrap_block_days=10,
        bootstrap_repetitions=100, min_valid_days=30, min_coverage=.8)
    result = mean_uncertainty(values, signal_dates=100, options=options)
    assert result['status'] == 'OK'
    assert result['hac_ci_lower'] > 0
    assert result['bootstrap_ci_lower'] > 0
    assert result['hac_p_value'] < .05
    assert result == mean_uncertainty(values, signal_dates=100, options=options)


def test_holm_reference_keeps_declared_family_when_tests_are_missing():
    np.testing.assert_allclose(holm_adjust([.01, np.nan, .04]), [.03, np.nan, .08], equal_nan=True)
    np.testing.assert_allclose(holm_adjust([.04, .01, .03]), [.06, .03, .06])
    np.testing.assert_allclose(holm_adjust([np.nan, np.nan, np.nan]), [np.nan] * 3, equal_nan=True)


@pytest.mark.parametrize("values, expected_status, valid, missing", [
    ([.1, np.nan, .2, np.nan, np.nan, np.nan], "INSUFFICIENT_DATES", 2, 4),
    ([.1, .2, .3, .4, np.nan, np.nan], "INSUFFICIENT_COVERAGE", 4, 2),
    ([np.nan] * 6, "INSUFFICIENT_DATES", 0, 6),
])
def test_mean_uncertainty_retains_missing_counts_without_inventing_significance(
    values, expected_status, valid, missing,
):
    options = RobustnessOptions(holding_days=1, hac_lags=0, bootstrap_block_days=1,
                                bootstrap_repetitions=100, min_valid_days=4, min_coverage=.8)
    result = mean_uncertainty(values, signal_dates=6, options=options)
    assert result["status"] == expected_status
    assert result["valid_dates"] == valid
    assert result["missing_metric_dates"] == missing
    assert result["calendar_slots"] == 6
    assert result["coverage"] == valid / 6
    if valid:
        assert result["mean"] == pytest.approx(np.nanmean(values))
    else:
        assert np.isnan(result["mean"])
    assert result["bootstrap_status"] == "NOT_RUN"
    assert result["bootstrap_valid_repetitions"] == 0
    for key in ("hac_standard_error", "hac_statistic", "hac_p_value", "hac_ci_lower",
                "hac_ci_upper", "bootstrap_ci_lower", "bootstrap_ci_upper"):
        assert np.isnan(result[key])
