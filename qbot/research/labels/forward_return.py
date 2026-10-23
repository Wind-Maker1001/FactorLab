"""Execution-aligned next-open forward excess-return labels."""

from typing import Iterable, Optional

import numpy as np
import pandas as pd

from qbot.research.errors import ConfigurationError, SchemaError


def _calendar_from_benchmark(
    benchmark: pd.DataFrame, calendar: Optional[Iterable[object]]
) -> pd.DatetimeIndex:
    if calendar is None:
        values = benchmark["date"]
    else:
        values = list(calendar)
    result = pd.DatetimeIndex(pd.to_datetime(values, errors="raise"))
    if result.tz is not None:
        result = result.tz_localize(None)
    result = result.normalize()
    if result.has_duplicates:
        result = result.drop_duplicates()
    result = result.sort_values()
    if result.empty:
        raise SchemaError("Label calendar is empty")
    return result


def build_forward_excess_return_labels(
    bars: pd.DataFrame,
    benchmark: pd.DataFrame,
    holding_days: int = 5,
    entry_price: str = "next_open",
    calendar: Optional[Iterable[object]] = None,
) -> pd.DataFrame:
    """Build labels for a signal calculated after the close of date ``t``.

    For the default five-day holding period, entry is ``adj_open[t+1]`` and
    exit is ``adj_open[t+6]``. No missing value is filled.
    """

    if entry_price != "next_open":
        raise ConfigurationError("Phase 1 labels only support entry_price=next_open")
    if not isinstance(holding_days, int) or holding_days < 1:
        raise ConfigurationError("holding_days must be a positive integer")
    required_stock = {"date", "symbol", "adj_open"}
    required_benchmark = {"date", "adj_open"}
    if not required_stock.issubset(bars.columns):
        raise SchemaError("Stock bars missing %s" % sorted(required_stock.difference(bars.columns)))
    if not required_benchmark.issubset(benchmark.columns):
        raise SchemaError(
            "Benchmark bars missing %s" % sorted(required_benchmark.difference(benchmark.columns))
        )
    dates = _calendar_from_benchmark(benchmark, calendar)

    stock = bars[["date", "symbol", "adj_open"]].copy()
    stock["date"] = pd.to_datetime(stock["date"], errors="raise").dt.tz_localize(None).dt.normalize()
    if stock.duplicated(["date", "symbol"]).any():
        raise SchemaError("Stock bars contain duplicate (date, symbol) rows")
    symbols = pd.Index(stock["symbol"].drop_duplicates().sort_values())
    full_index = pd.MultiIndex.from_product([dates, symbols], names=["date", "symbol"])
    stock = stock.set_index(["date", "symbol"]).reindex(full_index).reset_index()
    stock = stock.sort_values(["symbol", "date"], kind="mergesort")
    grouped_open = stock.groupby("symbol", sort=False)["adj_open"]
    stock["_entry_open"] = grouped_open.shift(-1)
    stock["_exit_open"] = grouped_open.shift(-(holding_days + 1))
    stock_return = stock["_exit_open"] / stock["_entry_open"] - 1.0
    stock_return = stock_return.replace([np.inf, -np.inf], np.nan)

    benchmark_frame = benchmark[["date", "adj_open"]].copy()
    benchmark_frame["date"] = (
        pd.to_datetime(benchmark_frame["date"], errors="raise").dt.tz_localize(None).dt.normalize()
    )
    if benchmark_frame["date"].duplicated().any():
        raise SchemaError("Benchmark contains duplicate dates")
    benchmark_open = benchmark_frame.set_index("date")["adj_open"].reindex(dates)
    benchmark_return = benchmark_open.shift(-(holding_days + 1)) / benchmark_open.shift(-1) - 1.0
    benchmark_return = benchmark_return.replace([np.inf, -np.inf], np.nan)

    entry_dates = pd.Series(dates, index=dates).shift(-1)
    exit_dates = pd.Series(dates, index=dates).shift(-(holding_days + 1))
    stock["entry_date"] = stock["date"].map(entry_dates)
    stock["exit_date"] = stock["date"].map(exit_dates)
    stock["STOCK_FWD_RET%d" % holding_days] = stock_return
    stock["BENCHMARK_FWD_RET%d" % holding_days] = stock["date"].map(benchmark_return)
    stock["EXCESS_RET%d" % holding_days] = (
        stock["STOCK_FWD_RET%d" % holding_days]
        - stock["BENCHMARK_FWD_RET%d" % holding_days]
    )
    columns = [
        "date",
        "symbol",
        "entry_date",
        "exit_date",
        "STOCK_FWD_RET%d" % holding_days,
        "BENCHMARK_FWD_RET%d" % holding_days,
        "EXCESS_RET%d" % holding_days,
    ]
    return stock[columns].sort_values(["date", "symbol"], kind="mergesort").reset_index(drop=True)
