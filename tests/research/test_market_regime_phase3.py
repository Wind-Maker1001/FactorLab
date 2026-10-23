"""Phase-3 CSI500 market-regime tests."""

import numpy as np
import pandas as pd
import pytest

from qbot.research.errors import SchemaError
from qbot.research.market.regime import (
    MARKET_FEATURE_COLUMNS,
    build_market_regime_features,
    compute_market_regime,
)


def _benchmark(close, *, column="adj_close"):
    values = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2023-01-02", periods=len(values)),
            "symbol": "000905.SH",
            column: values,
        }
    )


@pytest.mark.parametrize(
    ("close", "expected"),
    [
        (np.arange(1.0, 101.0), "BULL"),
        (np.arange(101.0, 1.0, -1.0), "BEAR"),
        (np.full(100, 50.0), "SIDEWAYS"),
    ],
)
def test_market_regime_matches_frozen_ma20_ma60_rule(close, expected):
    result = build_market_regime_features(_benchmark(close))
    assert tuple(result.columns) == MARKET_FEATURE_COLUMNS
    assert result.iloc[-1]["MARKET_REGIME"] == expected
    assert set(result["MARKET_REGIME"]).issubset({"BULL", "BEAR", "SIDEWAYS"})
    assert result.iloc[:59]["MARKET_REGIME"].eq("SIDEWAYS").all()


def test_index_return_and_volatility_features_match_trailing_pandas_formulas():
    close = pd.Series(100.0 * np.cumprod(1.0 + np.linspace(-0.01, 0.02, 90)))
    result = compute_market_regime(_benchmark(close))
    daily_return = close.pct_change(fill_method=None)
    expected = {
        "INDEX_RET5": close / close.shift(5) - 1.0,
        "INDEX_RET20": close / close.shift(20) - 1.0,
        "INDEX_RET60": close / close.shift(60) - 1.0,
        "INDEX_VOL20": daily_return.rolling(20, min_periods=20).std(ddof=1),
    }
    for column, values in expected.items():
        pd.testing.assert_series_equal(
            result[column], values, check_names=False, rtol=1e-12, atol=1e-12
        )


def test_adjusted_close_is_preferred_and_close_is_supported_as_fallback():
    adjusted = np.arange(1.0, 101.0)
    benchmark = _benchmark(adjusted)
    benchmark["close"] = adjusted[::-1]
    assert build_market_regime_features(benchmark).iloc[-1]["MARKET_REGIME"] == "BULL"
    assert (
        build_market_regime_features(benchmark, price_column="close")
        .iloc[-1]["MARKET_REGIME"]
        == "BEAR"
    )

    fallback = _benchmark(adjusted, column="close")
    assert build_market_regime_features(fallback).iloc[-1]["MARKET_REGIME"] == "BULL"


def test_future_price_mutation_cannot_change_past_regime_or_features():
    benchmark = _benchmark(100.0 * np.cumprod(np.full(100, 1.003)))
    cutoff = benchmark.loc[69, "date"]
    baseline = build_market_regime_features(benchmark)
    mutated = benchmark.copy()
    mutated.loc[mutated["date"] > cutoff, "adj_close"] *= 1000.0
    changed = build_market_regime_features(mutated)
    mask = baseline["date"] <= cutoff
    pd.testing.assert_frame_equal(
        baseline.loc[mask].reset_index(drop=True),
        changed.loc[mask].reset_index(drop=True),
    )


def test_missing_prices_are_not_filled_across_time():
    benchmark = _benchmark(np.arange(100.0, 180.0))
    benchmark.loc[30, "adj_close"] = np.nan
    result = build_market_regime_features(benchmark)
    assert pd.isna(result.loc[30, "INDEX_RET5"])
    assert pd.isna(result.loc[35, "INDEX_RET5"])
    assert result.loc[30:49, "INDEX_VOL20"].isna().all()
    assert result.loc[30:59, "MARKET_REGIME"].eq("SIDEWAYS").all()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda frame: frame.drop(columns="date"),
        lambda frame: pd.concat([frame, frame.iloc[[0]]], ignore_index=True),
        lambda frame: frame.iloc[::-1].reset_index(drop=True),
        lambda frame: frame.assign(symbol="000300.SH"),
        lambda frame: frame.assign(adj_close=-1.0),
        lambda frame: frame.assign(adj_close=np.inf),
    ],
)
def test_invalid_benchmark_schema_fails_fast(mutation):
    with pytest.raises(SchemaError):
        build_market_regime_features(mutation(_benchmark(np.arange(1.0, 81.0))))


def test_timezone_aware_dates_are_rejected():
    benchmark = _benchmark(np.arange(1.0, 81.0))
    benchmark["date"] = benchmark["date"].dt.tz_localize("Asia/Shanghai")
    with pytest.raises(SchemaError, match="timezone-naive"):
        build_market_regime_features(benchmark)
