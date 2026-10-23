"""Benchmark-derived trading-calendar utilities."""

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Union

import pandas as pd

try:
    from factorlab.errors import SchemaError
except ImportError:  # pragma: no cover - isolated bootstrapping only
    class SchemaError(ValueError):
        """Fallback used only when the package foundation is unavailable."""

from factorlab.data.schema import canonicalize_bars, normalize_symbol


DateLike = Union[str, pd.Timestamp]


def _date(value: DateLike) -> pd.Timestamp:
    try:
        result = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise SchemaError("invalid trading date: %r" % (value,)) from exc
    if result.tzinfo is not None:
        result = result.tz_localize(None)
    return result.normalize()


@dataclass(frozen=True)
class TradingCalendar:
    """An immutable ordered collection of actual benchmark sessions."""

    dates: pd.DatetimeIndex

    def __post_init__(self) -> None:
        values = pd.DatetimeIndex(pd.to_datetime(self.dates), name="date")
        if values.tz is not None:
            values = values.tz_localize(None)
        values = values.normalize()
        if values.hasnans:
            raise SchemaError("trading calendar contains a missing date")
        if values.has_duplicates:
            raise SchemaError("trading calendar contains duplicate dates")
        if not values.is_monotonic_increasing:
            raise SchemaError("trading calendar must be monotonically increasing")
        object.__setattr__(self, "dates", values)

    @classmethod
    def from_benchmark_bars(cls, bars: pd.DataFrame) -> "TradingCalendar":
        canonical = canonicalize_bars(bars, allow_missing_ohlc=False)
        if canonical.empty:
            raise SchemaError("benchmark bars are empty")
        if canonical["symbol"].nunique() != 1:
            raise SchemaError("benchmark bars must contain exactly one symbol")
        if canonical["date"].dt.dayofweek.ge(5).any():
            raise SchemaError("A-share benchmark calendar contains a weekend observation")
        return cls(pd.DatetimeIndex(canonical["date"], name="date"))

    def sessions(
        self, start: Optional[DateLike] = None, end: Optional[DateLike] = None
    ) -> pd.DatetimeIndex:
        start_date = self.dates.min() if start is None else _date(start)
        end_date = self.dates.max() if end is None else _date(end)
        if start_date > end_date:
            raise SchemaError("calendar start must be on or before end")
        return self.dates[(self.dates >= start_date) & (self.dates <= end_date)]

    def contains(self, value: DateLike) -> bool:
        return _date(value) in self.dates

    def position(self, value: DateLike) -> int:
        date = _date(value)
        location = self.dates.get_indexer([date])[0]
        if location < 0:
            raise SchemaError("date is not a trading session: %s" % date.date())
        return int(location)

    def shift(self, value: DateLike, sessions: int) -> pd.Timestamp:
        position = self.position(value) + int(sessions)
        if position < 0 or position >= len(self.dates):
            raise SchemaError("calendar shift is outside the available range")
        return self.dates[position]

    def __len__(self) -> int:
        return len(self.dates)


def reindex_bars_to_calendar(
    bars: pd.DataFrame,
    calendar: Union[TradingCalendar, pd.DatetimeIndex, Sequence[DateLike]],
    symbols: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """Rebuild stock series on market sessions without filling prices.

    Newly introduced suspension rows contain the canonical date and symbol but
    retain missing market fields.  There is deliberately no forward or backward
    filling in this function.
    """

    canonical = canonicalize_bars(bars, allow_missing_ohlc=True)
    sessions = calendar.dates if isinstance(calendar, TradingCalendar) else TradingCalendar(pd.DatetimeIndex(calendar)).dates
    if symbols is None:
        symbol_values = sorted(canonical["symbol"].unique())
    else:
        symbol_values = sorted({normalize_symbol(symbol) for symbol in symbols})
    if not symbol_values:
        return canonical.iloc[0:0].copy()
    grid = pd.MultiIndex.from_product(
        [sessions, symbol_values], names=["date", "symbol"]
    )
    indexed = canonical.set_index(["date", "symbol"])
    if not indexed.index.is_unique:
        raise SchemaError("duplicate bars cannot be reindexed to a calendar")
    output = indexed.reindex(grid).reset_index()
    return canonicalize_bars(output, allow_missing_ohlc=True)


__all__ = ["TradingCalendar", "reindex_bars_to_calendar"]
