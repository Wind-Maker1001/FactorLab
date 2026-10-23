import numpy as np
import pandas as pd
import pytest

from factorlab.data.schema import (
    CANONICAL_BAR_COLUMNS,
    SchemaError,
    canonicalize_bars,
    normalize_symbol,
)


def _minimal_bars():
    return pd.DataFrame(
        {
            "date": ["2024-01-03", "2024-01-02"],
            "symbol": ["600000", "sh600000"],
            "open": [10.0, 9.0],
            "high": [11.0, 10.0],
            "low": [9.5, 8.5],
            "close": [10.5, 9.5],
            "volume": [200.0, 100.0],
            "amount": [2_000.0, 900.0],
            "turnover": [0.02, 0.01],
        }
    )


@pytest.mark.parametrize(
    "source,expected",
    [
        ("600000", "600000.SH"),
        ("sh600000", "600000.SH"),
        ("600000.XSHG", "600000.SH"),
        ("000001", "000001.SZ"),
        ("SZ.000001", "000001.SZ"),
    ],
)
def test_normalize_symbol(source, expected):
    assert normalize_symbol(source) == expected


def test_canonicalize_sorts_dates_and_derives_price_copies():
    result = canonicalize_bars(_minimal_bars())
    assert list(result.columns[: len(CANONICAL_BAR_COLUMNS)]) == list(
        CANONICAL_BAR_COLUMNS
    )
    assert result["date"].tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]
    assert result["date"].dtype == np.dtype("datetime64[ns]")
    assert result["symbol"].unique().tolist() == ["600000.SH"]
    pd.testing.assert_series_equal(
        result["raw_close"], result["close"], check_names=False
    )
    pd.testing.assert_series_equal(
        result["adj_close"], result["close"], check_names=False
    )
    assert result["adj_factor"].eq(1.0).all()


def test_duplicate_primary_key_is_rejected():
    frame = pd.concat([_minimal_bars().iloc[[0]], _minimal_bars().iloc[[0]]])
    with pytest.raises(SchemaError, match="duplicate"):
        canonicalize_bars(frame)


def test_invalid_ohlc_is_rejected():
    frame = _minimal_bars().iloc[[0]].copy()
    frame["high"] = 9.0
    with pytest.raises(SchemaError, match="OHLC"):
        canonicalize_bars(frame)


def test_partial_suspension_row_is_rejected_but_fully_missing_is_allowed():
    frame = _minimal_bars().iloc[[0]].copy()
    for column in ("open", "high", "low", "close"):
        frame[column] = np.nan
    result = canonicalize_bars(frame, allow_missing_ohlc=True)
    assert result["raw_close"].isna().all()

    partial = _minimal_bars().iloc[[0]].copy()
    partial["close"] = np.nan
    with pytest.raises(SchemaError, match="all present or all missing"):
        canonicalize_bars(partial, allow_missing_ohlc=True)
