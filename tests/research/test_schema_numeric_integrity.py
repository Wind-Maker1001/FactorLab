"""Reject invalid finite-value and close-adjustment evidence at ingestion."""

import numpy as np
import pandas as pd
import pytest

from factorlab.data.schema import (
    ADJ_FACTOR_ATOL, ADJ_FACTOR_RTOL, NUMERIC_BAR_COLUMNS,
    canonicalize_bars, validate_bars,
)
from factorlab.errors import SchemaError
from factorlab.factors.registry import get_factor
from factorlab.labels.forward_return import build_forward_excess_return_labels


def _bars(periods=1):
    frame = pd.DataFrame({
        "date": pd.bdate_range("2024-01-02", periods=periods), "symbol": "600000.SH",
        "open": 10., "high": 11., "low": 9., "close": 10.,
        "volume": 1000., "amount": 10000., "turnover": .01,
    })
    for field in ("open", "high", "low", "close"):
        frame["raw_%s" % field] = frame[field]
        frame["adj_%s" % field] = frame[field] * 2.
    frame["adj_factor"] = 2.
    return frame


@pytest.mark.parametrize("field", NUMERIC_BAR_COLUMNS)
@pytest.mark.parametrize("value", [np.inf, -np.inf, "inf", "-inf"])
def test_all_numeric_fields_reject_infinity_including_vendor_strings(field, value):
    frame = _bars()
    # Assign an object column explicitly, avoiding pandas' incompatible-dtype
    # warning while exercising the production numeric-coercion boundary.
    frame[field] = pd.Series([value], dtype=object)
    with pytest.raises(SchemaError, match="%s must be finite" % field):
        canonicalize_bars(frame)


def test_direct_validator_rejects_infinite_fields():
    frame = canonicalize_bars(_bars())
    frame["amount"] = np.inf
    with pytest.raises(SchemaError, match="amount must be finite"):
        validate_bars(frame)


def test_finite_zero_liquidity_values_are_allowed_without_row_removal():
    frame = _bars(periods=2)
    frame.loc[0, ["volume", "amount", "turnover"]] = 0.
    result = canonicalize_bars(frame)
    assert len(result) == 2
    assert result.loc[0, ["volume", "amount", "turnover"]].eq(0.).all()
    assert result["date"].tolist() == frame["date"].tolist()


@pytest.mark.parametrize("field", ["open", "raw_close", "adj_open", "adj_factor"])
def test_zero_prices_or_adjustment_factor_are_not_valid(field):
    frame = _bars()
    frame[field] = 0.
    with pytest.raises(SchemaError):
        canonicalize_bars(frame)


def test_suspension_nans_and_nullable_numeric_fields_are_preserved():
    frame = _bars(periods=2)
    frame.loc[0, list(NUMERIC_BAR_COLUMNS)] = np.nan
    for field in NUMERIC_BAR_COLUMNS:
        frame[field] = frame[field].astype("Float64")
    result = canonicalize_bars(frame, allow_missing_ohlc=True)
    assert len(result) == 2
    assert result.loc[0, list(NUMERIC_BAR_COLUMNS)].isna().all()
    assert result.loc[1, "adj_factor"] == 2.
    with pytest.raises(SchemaError, match="OHLC values must not be missing"):
        canonicalize_bars(frame, allow_missing_ohlc=False)


@pytest.mark.parametrize("block", ["canonical", "raw"])
@pytest.mark.parametrize("direct_validation", [False, True])
def test_canonical_and_raw_missing_observations_cannot_disagree(
    block, direct_validation
):
    frame = canonicalize_bars(_bars(periods=2))
    fields = ["open", "high", "low", "close"]
    if block == "raw":
        fields = ["raw_" + field for field in fields]
    frame.loc[0, fields] = np.nan
    with pytest.raises(SchemaError, match="identical missing observations"):
        if direct_validation:
            validate_bars(frame, allow_missing_ohlc=True)
        else:
            canonicalize_bars(frame, allow_missing_ohlc=True)


def test_index_missing_turnover_remains_allowed():
    frame = _bars(periods=2)
    frame["symbol"] = "000905.SH"
    frame["turnover"] = np.nan
    for field in ("open", "high", "low", "close"):
        frame["adj_%s" % field] = frame[field]
    frame["adj_factor"] = 1.
    result = canonicalize_bars(frame)
    assert result["turnover"].isna().all()
    assert result["adj_factor"].eq(1.).all()


@pytest.mark.parametrize("factor", [1., 999., -2.])
def test_supplied_inconsistent_or_negative_factor_is_rejected_not_replaced(factor):
    frame = _bars()
    frame["adj_factor"] = factor
    original = frame.copy(deep=True)
    with pytest.raises(SchemaError, match="adj_factor"):
        canonicalize_bars(frame)
    pd.testing.assert_frame_equal(frame, original)


def test_close_ratio_roundoff_tolerance_is_explicit_and_small():
    frame = _bars()
    frame["adj_factor"] = 2. + .5 * (2. * ADJ_FACTOR_RTOL + ADJ_FACTOR_ATOL)
    result = canonicalize_bars(frame)
    assert result.loc[0, "adj_factor"] == frame.loc[0, "adj_factor"]
    frame["adj_factor"] = 2. + 2. * (2. * ADJ_FACTOR_RTOL + ADJ_FACTOR_ATOL)
    with pytest.raises(SchemaError, match="adj_close / raw_close"):
        canonicalize_bars(frame)


def test_affine_adjusted_ohlc_can_have_nonuniform_ratios():
    frame = _bars()
    for field in ("open", "high", "low", "close"):
        frame["adj_%s" % field] = 2. * frame[field] + 1.
    frame["adj_factor"] = frame["adj_close"] / frame["raw_close"]
    result = canonicalize_bars(frame)
    assert result.loc[0, "adj_factor"] == 2.1
    assert result.loc[0, "adj_low"] / result.loc[0, "raw_low"] != 2.1


def test_missing_factor_is_derived_only_when_column_is_absent():
    frame = _bars().drop(columns="adj_factor")
    assert canonicalize_bars(frame).loc[0, "adj_factor"] == 2.
    frame["adj_factor"] = np.nan
    assert canonicalize_bars(frame)["adj_factor"].isna().all()


def test_zero_amount_is_valid_schema_and_missing_amihud_not_corrupted_label():
    stocks = _bars(periods=22)
    stocks["amount"] = 0.
    stocks = canonicalize_bars(stocks)
    benchmark = stocks.copy()
    benchmark["symbol"] = "000905.SH"
    amihud = get_factor("AMIHU20").compute(stocks)
    assert amihud.isna().all()
    labels = build_forward_excess_return_labels(stocks, benchmark)
    assert labels["EXCESS_RET5"].iloc[:-6].eq(0.).all()
    assert labels["EXCESS_RET5"].iloc[-6:].isna().all()


def test_extreme_but_finite_close_ratio_overflow_is_rejected():
    frame = _bars()
    for field in ("open", "high", "low", "close"):
        frame[field] = 1e-300
        frame["raw_%s" % field] = 1e-300
        frame["adj_%s" % field] = 1e300
    frame["adj_factor"] = 1.
    with pytest.raises(SchemaError, match="adj_close / raw_close"):
        canonicalize_bars(frame)
