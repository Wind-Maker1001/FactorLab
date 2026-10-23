import numpy as np
import pandas as pd
import pytest

from qbot.research.data.calendar import (
    SchemaError,
    TradingCalendar,
    reindex_bars_to_calendar,
)
from qbot.research.data.schema import canonicalize_bars


def _bars(dates):
    count = len(dates)
    values = [10.0 + offset for offset in range(count)]
    return canonicalize_bars(
        pd.DataFrame(
            {
                "date": dates,
                "symbol": "600000.SH",
                "open": values,
                "high": [value + 1 for value in values],
                "low": [value - 1 for value in values],
                "close": values,
                "volume": 1000.0,
                "amount": 10_000.0,
                "turnover": 0.01,
            }
        )
    )


def test_calendar_is_ordered_and_shift_uses_sessions_not_calendar_days():
    calendar = TradingCalendar(
        pd.DatetimeIndex(["2024-01-05", "2024-01-08", "2024-01-09"])
    )
    assert calendar.shift("2024-01-05", 1) == pd.Timestamp("2024-01-08")
    assert calendar.position("2024-01-09") == 2
    with pytest.raises(SchemaError, match="not a trading session"):
        calendar.position("2024-01-06")


def test_reindex_inserts_missing_suspension_day_without_any_fill():
    bars = _bars(["2024-01-02", "2024-01-04"])
    calendar = TradingCalendar(
        pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04"])
    )
    aligned = reindex_bars_to_calendar(bars, calendar)
    suspended = aligned.loc[aligned["date"] == pd.Timestamp("2024-01-03")].iloc[0]
    assert suspended["symbol"] == "600000.SH"
    assert np.isnan(suspended["open"])
    assert np.isnan(suspended["adj_close"])
    assert np.isnan(suspended["volume"])


def test_calendar_rejects_duplicates_and_unsorted_dates():
    with pytest.raises(SchemaError, match="duplicate"):
        TradingCalendar(pd.DatetimeIndex(["2024-01-02", "2024-01-02"]))
    with pytest.raises(SchemaError, match="monotonically"):
        TradingCalendar(pd.DatetimeIndex(["2024-01-03", "2024-01-02"]))


@pytest.mark.parametrize("weekend", ["2024-01-06", "2024-01-07"])
def test_benchmark_calendar_rejects_weekend_instead_of_inventing_session(weekend):
    bars = _bars(["2024-01-05", weekend, "2024-01-08"])
    with pytest.raises(SchemaError, match="weekend"):
        TradingCalendar.from_benchmark_bars(bars)


def test_benchmark_calendar_does_not_insert_weekdays_or_assume_holidays():
    dates = ["2024-01-05", "2024-01-09"]
    calendar = TradingCalendar.from_benchmark_bars(_bars(dates))
    assert list(calendar.dates) == list(pd.to_datetime(dates))
