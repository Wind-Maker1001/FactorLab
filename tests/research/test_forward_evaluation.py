"""Offline checks of genuine production forward-label/metric calculations."""

from dataclasses import FrozenInstanceError
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from factorlab.data.schema import canonicalize_bars, empty_bars
from factorlab.errors import SchemaError
from factorlab.forward.evaluation import (
    ForwardEvaluationError,
    evaluate_forward_predictions,
)


SYMBOLS = ("600001.SH", "600002.SH", "600003.SH", "600004.SH")


def _bars(dates, symbol, growth=0.01, raw_growth=None):
    steps = np.arange(len(dates), dtype=float)
    adjusted = 100.0 * (1 + steps * growth)
    raw = adjusted if raw_growth is None else 100.0 * (1 + steps * raw_growth)
    return canonicalize_bars(pd.DataFrame({
        "date": dates, "symbol": symbol,
        "open": raw, "high": raw * 1.02, "low": raw * 0.98, "close": raw * 1.005,
        "adj_open": adjusted, "adj_high": adjusted * 1.02,
        "adj_low": adjusted * 0.98, "adj_close": adjusted * 1.005,
        "volume": 10000.0, "amount": 1000000.0, "turnover": 0.01,
    }))


@pytest.fixture
def inputs():
    dates = pd.bdate_range("2026-01-05", periods=15)
    stock = pd.concat([_bars(dates, symbol, growth=0.10 - i * 0.02, raw_growth=0)
                       for i, symbol in enumerate(SYMBOLS)], ignore_index=True)
    benchmark = _bars(dates, "000905.SH", growth=0.01)
    predictions = pd.DataFrame([
        {"signal_date": day.date().isoformat(), "symbol": symbol,
         "score": float(4 - i), "rank": i + 1,
         "model_name": "ridge", "signal_version": "v0001",
         "signal_manifest_sha256": "a" * 64, "timeliness": "ON_TIME",
         "run_id": "run-" + day.date().isoformat()}
        for day in dates[[0, 1, -2, -1]] for i, symbol in enumerate(SYMBOLS)
    ])
    return predictions, stock, benchmark, dates


def _evaluate(inputs, **kwargs):
    predictions, stock, benchmark, dates = inputs
    options = dict(as_of=dates[-1], top_k=1, minimum_cross_section=2)
    options.update(kwargs)
    return evaluate_forward_predictions(predictions, stock, benchmark, **options)


def test_execution_aligned_adjusted_formula_and_metrics(inputs):
    result = _evaluate(inputs)
    first = result.predictions.iloc[0]
    dates = inputs[-1]
    assert first.entry_date == dates[1]
    assert first.exit_date == dates[6]
    assert first.STOCK_FWD_RET5 == pytest.approx(160 / 110 - 1)
    assert first.BENCHMARK_FWD_RET5 == pytest.approx(106 / 101 - 1)
    assert first.EXCESS_RET5 == pytest.approx((160 / 110 - 1) - (106 / 101 - 1))
    assert first.label_status == "MATURE"
    assert first.holding_days == 5
    assert result.as_of == dates[-1].date().isoformat()
    daily = result.daily.iloc[0]
    assert daily.prediction_rank_ic == pytest.approx(1.0)
    assert daily.prediction_ic > 0.99
    assert daily.top_bottom_spread > 0
    assert set(result.summary.scope) == {"OVERALL", "YEAR", "QUARTER", "REGIME"}
    assert set(result.predictions.EVALUATION_MARKET_REGIME) == {"SIDEWAYS"}
    overall = result.summary.loc[result.summary.scope == "OVERALL"].iloc[0]
    assert overall.rank_ic_valid_dates == 2
    assert overall.scored_observations == 16
    assert overall.observations == 8
    assert overall.coverage == 0.5


def test_original_metadata_order_and_inputs_are_unchanged(inputs):
    predictions, stock, benchmark, dates = inputs
    predictions = predictions.sample(frac=1, random_state=42)
    snapshots = [frame.copy(deep=True) for frame in (predictions, stock, benchmark)]
    result = _evaluate((predictions, stock, benchmark, dates))
    assert_frame_equal(result.predictions[predictions.columns], predictions.reset_index(drop=True))
    for original, snapshot in zip((predictions, stock, benchmark), snapshots):
        assert_frame_equal(original, snapshot)
    with pytest.raises(FrozenInstanceError):
        result.as_of = "2020-01-01"


def test_existing_equal_date_is_allowed(inputs):
    predictions, stock, benchmark, dates = inputs
    predictions = predictions.assign(date=pd.to_datetime(predictions.signal_date))
    result = _evaluate((predictions, stock, benchmark, dates))
    assert result.predictions.date.equals(pd.to_datetime(result.predictions.signal_date))


def test_holding_period_is_configurable(inputs):
    result = _evaluate(inputs, holding_days=2)
    first = result.predictions.iloc[0]
    assert first.exit_date == inputs[-1][3]
    assert first.STOCK_FWD_RET2 == pytest.approx(130 / 110 - 1)
    assert first.EXCESS_RET2 == pytest.approx((130 / 110 - 1) - (103 / 101 - 1))
    assert "EXCESS_RET5" not in result.predictions


def test_tail_six_sessions_remain_immature_and_in_coverage(inputs):
    predictions, stock, benchmark, dates = inputs
    predictions = pd.concat([predictions.iloc[:4].assign(signal_date=day.date().isoformat())
                             for day in dates[-6:]], ignore_index=True)
    result = _evaluate((predictions, stock, benchmark, dates))
    assert len(result.predictions) == 24
    assert result.predictions.label_status.eq("IMMATURE").all()
    assert result.predictions.EXCESS_RET5.isna().all()
    assert result.predictions.exit_date.isna().all()
    assert result.daily.universe_size.eq(4).all()
    assert result.daily.coverage.eq(0).all()
    assert result.daily.prediction_ic.isna().all()
    assert result.daily.top_k_future_return.isna().all()
    overall = result.summary.loc[result.summary.scope == "OVERALL"].iloc[0]
    assert overall.valid_dates == 0
    assert overall.scored_observations == 24
    assert overall.coverage == 0
    assert pd.isna(overall.prediction_rank_ic_mean)


@pytest.mark.parametrize("missing_session", [1, 6])
def test_missing_entry_or_exit_does_not_promote_next_stock(inputs, missing_session):
    predictions, stock, benchmark, dates = inputs
    stock = stock.loc[~((stock.symbol == SYMBOLS[0]) & (stock.date == dates[missing_session]))]
    result = _evaluate((predictions, stock, benchmark, dates))
    missing = result.predictions.iloc[0]
    assert missing.symbol == SYMBOLS[0]
    assert missing["rank"] == 1
    assert missing.entry_date == dates[1]
    assert missing.exit_date == dates[6]
    assert missing.label_status == "MATURE_MISSING_PRICE"
    assert pd.isna(missing.EXCESS_RET5)
    daily = result.daily.iloc[0]
    assert daily.top_count == 1
    assert daily.top_label_count == 0
    assert daily.top_label_coverage == 0
    assert pd.isna(daily.top_k_future_return)
    assert daily.observations == 3
    assert daily.coverage == 0.75
    assert daily.bottom_label_count == 1


def test_suspension_in_middle_does_not_shorten_market_window(inputs):
    predictions, stock, benchmark, dates = inputs
    stock = stock.loc[~((stock.symbol == SYMBOLS[0]) & stock.date.isin(dates[2:6]))]
    result = _evaluate((predictions, stock, benchmark, dates))
    first = result.predictions.iloc[0]
    assert first.exit_date == dates[6]
    assert first.STOCK_FWD_RET5 == pytest.approx(160 / 110 - 1)


def test_whole_missing_symbol_keeps_calendar_maturity(inputs):
    predictions, stock, benchmark, dates = inputs
    stock = stock.loc[stock.symbol != SYMBOLS[0]]
    result = _evaluate((predictions, stock, benchmark, dates))
    missing = result.predictions.loc[result.predictions.symbol == SYMBOLS[0]]
    assert len(missing) == 4
    assert missing.iloc[:2].label_status.eq("MATURE_MISSING_PRICE").all()
    assert missing.iloc[:2].exit_date.notna().all()
    assert missing.EXCESS_RET5.isna().all()
    assert missing.iloc[2:].label_status.eq("IMMATURE").all()


def test_no_stock_bars_is_valid_missing_price_coverage(inputs):
    predictions, _, benchmark, dates = inputs
    result = _evaluate((predictions, empty_bars(), benchmark, dates))
    assert len(result.predictions) == len(predictions)
    assert result.predictions.iloc[:8].label_status.eq("MATURE_MISSING_PRICE").all()
    assert result.daily.coverage.eq(0).all()


def test_extra_unpredicted_stocks_are_not_added(inputs):
    predictions, stock, benchmark, dates = inputs
    stock = pd.concat([stock, _bars(dates, "000001.SZ")], ignore_index=True)
    result = _evaluate((predictions, stock, benchmark, dates))
    assert len(result.predictions) == len(predictions)
    assert set(result.predictions.symbol) == set(SYMBOLS)


def test_valid_future_prices_do_not_affect_asof_output(inputs):
    predictions, stock, benchmark, dates = inputs
    predictions = predictions.iloc[:8].copy()
    cutoff = dates[7]
    result = _evaluate((predictions, stock, benchmark, dates), as_of=cutoff)
    changed_stock = stock.copy(deep=True)
    changed_benchmark = benchmark.copy(deep=True)
    for frame in (changed_stock, changed_benchmark):
        future = frame.date > cutoff
        for name in ("open", "high", "low", "close", "raw_open", "raw_high",
                     "raw_low", "raw_close", "adj_open", "adj_high", "adj_low", "adj_close"):
            frame.loc[future, name] *= 1000
    changed = _evaluate((predictions, changed_stock, changed_benchmark, dates), as_of=cutoff)
    assert_frame_equal(result.predictions, changed.predictions)
    assert_frame_equal(result.daily, changed.daily)
    assert_frame_equal(result.summary, changed.summary)


def test_original_regime_is_preserved_and_used(inputs):
    predictions, stock, benchmark, dates = inputs
    predictions = predictions.assign(MARKET_REGIME="BULL")
    result = _evaluate((predictions, stock, benchmark, dates))
    assert result.predictions.MARKET_REGIME.eq("BULL").all()
    assert "EVALUATION_MARKET_REGIME" not in result.predictions
    assert set(result.summary.loc[result.summary.scope == "REGIME", "period"]) == {"BULL"}


def test_regime_disabled_does_not_change_original_metadata(inputs):
    predictions, stock, benchmark, dates = inputs
    predictions = predictions.assign(MARKET_REGIME="BULL")
    result = _evaluate((predictions, stock, benchmark, dates), include_regime=False)
    assert result.predictions.MARKET_REGIME.eq("BULL").all()
    assert "REGIME" not in set(result.summary.scope)


@pytest.mark.parametrize("value", [True, False, 20260123, 1.0, None, pd.NaT, np.datetime64("NaT"),
                                  "2026-1-23", "2026/01/23", "2026-01-23T00:00:00",
                                  " 2026-01-23", "2026-01-23 ", "2026-02-30",
                                  pd.Timestamp("2026-01-23", tz="Asia/Shanghai"),
                                  pd.Timestamp("2026-01-23 09:30:00")])
def test_strict_asof_dates(inputs, value):
    with pytest.raises(ForwardEvaluationError):
        _evaluate(inputs, as_of=value)


@pytest.mark.parametrize("kind", ["iso", "date", "datetime", "timestamp", "numpy"])
def test_canonical_midnight_date_types_are_allowed(inputs, kind):
    day = inputs[-1][-1]
    values = {"iso": day.date().isoformat(), "date": day.date(),
              "datetime": datetime.combine(day.date(), datetime.min.time()),
              "timestamp": day, "numpy": day.to_datetime64()}
    assert _evaluate(inputs, as_of=values[kind]).as_of == day.date().isoformat()


@pytest.mark.parametrize("name,value", [("holding_days", True), ("holding_days", 0),
                                       ("holding_days", 1.0), ("top_k", False), ("top_k", 0),
                                       ("minimum_cross_section", 1), ("minimum_cross_section", True),
                                       ("include_regime", 1), ("regime_price_column", "qfq")])
def test_parameter_types_are_strict(inputs, name, value):
    with pytest.raises(ForwardEvaluationError):
        _evaluate(inputs, **{name: value})


def test_asof_nontrading_date_and_missing_signal_calendar_fail(inputs):
    with pytest.raises(ForwardEvaluationError, match="as_of"):
        _evaluate(inputs, as_of="2026-01-24")
    predictions, stock, benchmark, dates = inputs
    predictions = predictions.copy()
    predictions.loc[:3, "signal_date"] = "2026-01-04"
    with pytest.raises(ForwardEvaluationError, match="signal_date"):
        _evaluate((predictions, stock, benchmark, dates))


def test_signal_date_after_asof_fails(inputs):
    with pytest.raises(ForwardEvaluationError, match="later"):
        _evaluate(inputs, as_of=inputs[-1][7])


@pytest.mark.parametrize("column", ["EXCESS_RET5", "EXCESS_RET10", "STOCK_FWD_RET5", "BENCHMARK_FWD_RET5",
                                   "entry_date", "exit_date", "holding_days", "as_of", "label_status",
                                   "EVALUATION_MARKET_REGIME"])
def test_existing_label_or_evaluation_columns_fail(inputs, column):
    predictions, stock, benchmark, dates = inputs
    predictions = predictions.assign(**{column: np.nan})
    with pytest.raises(ForwardEvaluationError, match="already contain"):
        _evaluate((predictions, stock, benchmark, dates))


@pytest.mark.parametrize("value", [True, 1, "2026-01-05 00:00:00", "2026-1-5", None,
                                  pd.Timestamp("2026-01-05", tz="UTC"), pd.Timestamp("2026-01-05 01:00")])
def test_prediction_dates_are_strict(inputs, value):
    predictions, stock, benchmark, dates = inputs
    predictions = predictions.copy()
    predictions.loc[0, "signal_date"] = value
    with pytest.raises(ForwardEvaluationError):
        _evaluate((predictions, stock, benchmark, dates))


def test_conflicting_existing_date_fails(inputs):
    predictions, stock, benchmark, dates = inputs
    predictions = predictions.assign(date=dates[0])
    with pytest.raises(ForwardEvaluationError, match="date must equal"):
        _evaluate((predictions, stock, benchmark, dates))


@pytest.mark.parametrize("column,value", [("symbol", "SH600001"), ("symbol", "600001.sh"),
                                        ("symbol", None), ("score", np.nan), ("score", np.inf),
                                        ("score", -np.inf), ("score", True), ("score", "4.0"),
                                        ("score", 4 + 0j), ("rank", 0), ("rank", 1.5),
                                        ("rank", np.nan), ("rank", np.inf), ("rank", True)])
def test_invalid_prediction_values_fail(inputs, column, value):
    predictions, stock, benchmark, dates = inputs
    predictions = predictions.copy()
    predictions[column] = predictions[column].astype(object)
    predictions.loc[0, column] = value
    with pytest.raises(ForwardEvaluationError):
        _evaluate((predictions, stock, benchmark, dates))


def test_duplicate_predictions_columns_and_rows_fail(inputs):
    predictions, stock, benchmark, dates = inputs
    for bad in (pd.concat([predictions, predictions.iloc[[0]]], ignore_index=True),
                pd.concat([predictions, predictions[["score"]]], axis=1),
                predictions.iloc[:0], predictions.drop(columns="rank")):
        with pytest.raises(ForwardEvaluationError):
            _evaluate((bad, stock, benchmark, dates))


def test_ranking_requires_score_order_continuity_and_symbol_ties(inputs):
    predictions, stock, benchmark, dates = inputs
    for replacement in ([2, 1, 3, 4], [1, 1, 3, 4], [1, 2, 3, 5]):
        bad = predictions.copy()
        bad.loc[:3, "rank"] = replacement
        with pytest.raises(ForwardEvaluationError, match="ranks must"):
            _evaluate((bad, stock, benchmark, dates))
    tied = predictions.copy()
    tied.loc[:3, "score"] = 1.0
    assert pd.isna(_evaluate((tied, stock, benchmark, dates)).daily.iloc[0].prediction_ic)
    tied.loc[:3, "rank"] = [2, 1, 3, 4]
    with pytest.raises(ForwardEvaluationError):
        _evaluate((tied, stock, benchmark, dates))


@pytest.mark.parametrize("role", ["stock", "benchmark"])
def test_explicit_adjusted_open_required(inputs, role):
    predictions, stock, benchmark, dates = inputs
    if role == "stock":
        stock = stock.drop(columns="adj_open")
    else:
        benchmark = benchmark.drop(columns="adj_open")
    with pytest.raises(SchemaError, match="explicit adj_open"):
        _evaluate((predictions, stock, benchmark, dates))


def test_benchmark_identity_and_ohlc_schema_fail(inputs):
    predictions, stock, benchmark, dates = inputs
    wrong = benchmark.assign(symbol="000300.SH")
    with pytest.raises(SchemaError, match="requested symbol"):
        _evaluate((predictions, stock, wrong, dates))
    with pytest.raises(SchemaError, match="duplicate"):
        _evaluate((predictions, pd.concat([stock, stock.iloc[[0]]]), benchmark, dates))
    invalid = stock.copy()
    invalid.loc[0, ["open", "raw_open"]] = 0
    with pytest.raises(SchemaError):
        _evaluate((predictions, invalid, benchmark, dates))
    partial = stock.copy()
    partial.loc[0, "adj_open"] = np.nan
    with pytest.raises(SchemaError, match="all present or all missing"):
        _evaluate((predictions, partial, benchmark, dates))


def test_regime_has_no_implicit_raw_close_fallback(inputs):
    predictions, stock, benchmark, dates = inputs
    benchmark = benchmark.drop(columns="adj_close")
    with pytest.raises(SchemaError, match="explicit adj_close"):
        _evaluate((predictions, stock, benchmark, dates))
    assert "REGIME" in set(_evaluate((predictions, stock, benchmark, dates),
                                    regime_price_column="close").summary.scope)


@pytest.mark.parametrize("state", [None, "UNKNOWN"])
def test_unknown_original_regime_fails(inputs, state):
    predictions, stock, benchmark, dates = inputs
    predictions = predictions.assign(MARKET_REGIME=state)
    with pytest.raises(ForwardEvaluationError, match="states"):
        _evaluate((predictions, stock, benchmark, dates))


def test_conflicting_original_regime_per_date_fails(inputs):
    predictions, stock, benchmark, dates = inputs
    predictions = predictions.assign(MARKET_REGIME="BULL")
    predictions.loc[0, "MARKET_REGIME"] = "BEAR"
    with pytest.raises(ForwardEvaluationError, match="constant"):
        _evaluate((predictions, stock, benchmark, dates))
