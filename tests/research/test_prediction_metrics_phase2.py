"""Prediction metrics for out-of-sample cross-sectional scores."""

import numpy as np
import pandas as pd
import pytest

from factorlab.metrics.prediction import (
    PredictionMetricError,
    evaluate_predictions,
)


def _predictions():
    dates = pd.to_datetime(
        ["2021-12-31", "2022-01-03", "2022-04-01", "2022-04-04"]
    )
    rows = []
    for date_number, date in enumerate(dates):
        for number in range(6):
            rows.append(
                {
                    "date": date,
                    "symbol": "%06d.SZ" % (number + 1),
                    "score": float(number),
                    "EXCESS_RET5": float(number) / 100.0,
                    "fold": 0 if date_number < 2 else 1,
                    "MARKET_REGIME": "BULL" if date_number != 3 else "BEAR",
                }
            )
    return pd.DataFrame(rows)


def test_prediction_metrics_cover_daily_and_requested_decompositions():
    result = evaluate_predictions(
        _predictions(),
        top_k=2,
        minimum_cross_section=4,
        regime_col="MARKET_REGIME",
    )

    assert len(result.daily) == 4
    np.testing.assert_allclose(result.daily["prediction_ic"], 1.0)
    np.testing.assert_allclose(result.daily["prediction_rank_ic"], 1.0)
    np.testing.assert_allclose(result.daily["top_k_future_return"], 0.045)
    np.testing.assert_allclose(result.daily["bottom_k_future_return"], 0.005)
    np.testing.assert_allclose(result.daily["top_bottom_spread"], 0.04)
    assert set(result.summary["scope"]) == {
        "OVERALL",
        "YEAR",
        "QUARTER",
        "FOLD",
        "REGIME",
    }
    assert set(result.by_year["period"]) == {"2021", "2022"}
    assert set(result.by_quarter["period"]) == {
        "2021Q4",
        "2022Q1",
        "2022Q2",
    }
    assert set(result.by_fold["period"]) == {"0", "1"}
    assert set(result.by_regime["period"]) == {"BULL", "BEAR"}
    assert result.overall.iloc[0]["prediction_rank_ic_mean"] == pytest.approx(1.0)


def test_missing_future_labels_reduce_coverage_without_being_filled():
    frame = _predictions().loc[lambda value: value["date"] == pd.Timestamp("2022-01-03")].copy()
    frame.loc[frame["symbol"].isin(["000001.SZ", "000002.SZ"]), "EXCESS_RET5"] = np.nan

    result = evaluate_predictions(frame, top_k=2, minimum_cross_section=4)

    daily = result.daily.iloc[0]
    assert daily["universe_size"] == 6
    assert daily["observations"] == 4
    assert daily["coverage"] == pytest.approx(4.0 / 6.0)
    assert daily["prediction_rank_ic"] == pytest.approx(1.0)
    assert frame["EXCESS_RET5"].isna().sum() == 2


def test_missing_top_label_never_promotes_the_next_ranked_stock():
    frame = pd.DataFrame(
        {
            "date": pd.Timestamp("2024-01-02"),
            "symbol": ["%06d.SZ" % number for number in range(1, 7)],
            "score": [6.0, 5.0, 4.0, 3.0, 2.0, 1.0],
            "EXCESS_RET5": [np.nan, 0.50, 0.40, 0.30, 0.20, 0.10],
            "fold": 0,
        }
    )

    daily = evaluate_predictions(
        frame, top_k=2, minimum_cross_section=4
    ).daily.iloc[0]

    # The selected Top-2 remains score 6/5.  Score 4 is not promoted merely
    # because the highest-ranked stock's future label is still unavailable.
    assert daily["top_count"] == 2
    assert daily["top_label_count"] == 1
    assert daily["top_label_coverage"] == pytest.approx(0.5)
    assert daily["top_k_future_return"] == pytest.approx(0.50)
    assert daily["score_coverage"] == pytest.approx(1.0)


def test_small_cross_section_emits_nan_metrics_instead_of_false_signal():
    frame = _predictions().head(3).copy()
    frame.loc[0, "EXCESS_RET5"] = np.nan

    result = evaluate_predictions(frame, top_k=1, minimum_cross_section=3)

    daily = result.daily.iloc[0]
    assert daily["observations"] == 2
    assert np.isnan(daily["prediction_ic"])
    assert np.isnan(daily["top_bottom_spread"])
    assert result.overall.iloc[0]["valid_dates"] == 0


def test_prediction_metrics_fail_on_duplicates_bad_columns_and_infinity():
    frame = _predictions()
    duplicate = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    with pytest.raises(PredictionMetricError, match="duplicate prediction"):
        evaluate_predictions(duplicate)

    with pytest.raises(PredictionMetricError, match="missing required columns"):
        evaluate_predictions(frame.drop(columns="score"))

    invalid = frame.copy()
    invalid.loc[0, "score"] = np.inf
    with pytest.raises(PredictionMetricError, match="infinity"):
        evaluate_predictions(invalid)


def test_regime_and_fold_must_be_constant_within_each_prediction_date():
    frame = _predictions()
    frame.loc[0, "MARKET_REGIME"] = "BEAR"
    with pytest.raises(PredictionMetricError, match="exactly one value"):
        evaluate_predictions(frame, regime_col="MARKET_REGIME")

    frame = _predictions()
    frame.loc[0, "fold"] = 99
    with pytest.raises(PredictionMetricError, match="exactly one value"):
        evaluate_predictions(frame)
