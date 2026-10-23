"""Regression cases for each canonical OHLC relationship invariant."""

import pandas as pd
import pytest

from factorlab.data.schema import canonicalize_bars
from factorlab.errors import SchemaError


def _bar(high, low):
    return pd.DataFrame(
        {
            "date": ["2024-01-02"],
            "symbol": ["600000.SH"],
            "open": [10.0],
            "high": [high],
            "low": [low],
            "close": [10.0],
            "volume": [1000.0],
            "amount": [10000.0],
            "turnover": [0.01],
        }
    )


@pytest.mark.parametrize(
    "high,low",
    [
        (9.75, 9.0),  # high below open/close while still above low
        (11.0, 10.25),  # low above open/close while still below high
        (9.0, 9.5),  # high below low
    ],
)
def test_each_ohlc_relationship_violation_is_rejected(high, low):
    with pytest.raises(SchemaError, match="OHLC relationship"):
        canonicalize_bars(_bar(high, low))
