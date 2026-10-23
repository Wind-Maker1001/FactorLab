"""Base contracts and helpers for factors."""

from abc import ABC, abstractmethod
from typing import Sequence

import pandas as pd

from qbot.research.errors import SchemaError


class Factor(ABC):
    """A single-stock time-series transform with no future observations."""

    name: str
    lookback: int
    required_columns: Sequence[str] = ("date", "symbol", "adj_close")

    def validate(self, bars: pd.DataFrame) -> pd.DataFrame:
        missing = set(self.required_columns).difference(bars.columns)
        if missing:
            raise SchemaError("Factor %s missing columns %s" % (self.name, sorted(missing)))
        ordered = bars.copy()
        ordered["date"] = pd.to_datetime(ordered["date"], errors="raise").dt.normalize()
        if ordered.duplicated(["date", "symbol"]).any():
            raise SchemaError("Factor input has duplicate (date, symbol) rows")
        return ordered.sort_values(["symbol", "date"], kind="mergesort")

    @abstractmethod
    def compute(self, bars: pd.DataFrame) -> pd.Series:
        """Return values indexed like ``bars``."""


def grouped_shift(series: pd.Series, symbols: pd.Series, periods: int) -> pd.Series:
    return series.groupby(symbols, sort=False).shift(periods)


def daily_return(close: pd.Series, symbols: pd.Series) -> pd.Series:
    return close.groupby(symbols, sort=False).pct_change(fill_method=None)


def rolling_transform(
    values: pd.Series, symbols: pd.Series, window: int, operation: str
) -> pd.Series:
    grouped = values.groupby(symbols, sort=False)
    if operation == "mean":
        return grouped.transform(lambda value: value.rolling(window, min_periods=window).mean())
    if operation == "std":
        return grouped.transform(
            lambda value: value.rolling(window, min_periods=window).std(ddof=1)
        )
    raise ValueError("Unsupported rolling operation %s" % operation)
