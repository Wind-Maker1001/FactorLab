"""Past-only daily eligibility filters applied after PIT membership."""

from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd

from factorlab.errors import PointInTimeUniverseError, SchemaError

from .csi500 import PIT_REQUIRED, validate_daily_membership


@dataclass(frozen=True)
class UniverseFilterConfig:
    min_listed_days: int = 120
    exclude_st: bool = True
    exclude_suspended: bool = True
    zero_volume_window: int = 20
    quality_window: int = 60
    min_quality_ratio: float = 0.80


def _normalise_calendar(calendar: Iterable[object]) -> pd.DatetimeIndex:
    result = pd.DatetimeIndex(pd.to_datetime(list(calendar), errors="raise"))
    if result.tz is not None:
        result = result.tz_localize(None)
    if result.has_duplicates or not result.is_monotonic_increasing:
        raise SchemaError("Trade calendar must be unique and monotonically increasing")
    return result.normalize()


def build_filter_flags(
    bars: pd.DataFrame,
    status: pd.DataFrame,
    listings: pd.DataFrame,
    calendar: Iterable[object],
    config: UniverseFilterConfig,
) -> pd.DataFrame:
    """Calculate daily flags using only observations at or before each date."""

    required = {"date", "symbol", "open", "high", "low", "close", "volume"}
    missing = required.difference(bars.columns)
    if missing:
        raise SchemaError("Bars missing filter columns: %s" % sorted(missing))
    dates = _normalise_calendar(calendar)
    symbols = pd.Index(bars["symbol"].drop_duplicates().sort_values())
    grid = pd.MultiIndex.from_product([dates, symbols], names=["date", "symbol"])
    ordered = bars.copy()
    ordered["date"] = pd.to_datetime(ordered["date"]).dt.normalize()
    ordered = ordered.sort_values(["symbol", "date"])
    if ordered.duplicated(["date", "symbol"]).any():
        raise SchemaError("Bars contain duplicate (date, symbol) rows")
    indexed = ordered.set_index(["date", "symbol"]).reindex(grid)

    valid_ohlcv = indexed[["open", "high", "low", "close", "volume"]].notna().all(axis=1)
    indexed["_valid"] = valid_ohlcv.astype(float)
    indexed["_positive_volume"] = indexed["volume"].gt(0).astype(float)
    quality_count = indexed["_valid"].groupby(level="symbol", sort=False).transform(
        lambda values: values.rolling(
            config.quality_window, min_periods=config.quality_window
        ).sum()
    )
    positive_count = indexed["_positive_volume"].groupby(
        level="symbol", sort=False
    ).transform(
        lambda values: values.rolling(
            config.zero_volume_window, min_periods=config.zero_volume_window
        ).sum()
    )

    listing_map = listings.copy()
    if not {"symbol", "listing_date"}.issubset(listing_map.columns):
        raise SchemaError("Listings require symbol and listing_date")
    listing_map["listing_date"] = pd.to_datetime(listing_map["listing_date"]).dt.normalize()
    if listing_map["symbol"].duplicated().any():
        raise SchemaError("Listings contain duplicate symbols")
    listing_series = listing_map.set_index("symbol")["listing_date"]
    grid_dates = grid.get_level_values("date")
    grid_symbols = grid.get_level_values("symbol")
    listing_dates = listing_series.reindex(grid_symbols).to_numpy(dtype="datetime64[ns]")
    date_values = grid_dates.to_numpy(dtype="datetime64[ns]")
    date_positions = pd.Series(np.arange(len(dates)), index=dates)
    listing_positions = np.searchsorted(dates.values, listing_dates, side="left")
    current_positions = date_positions.reindex(grid_dates).to_numpy()
    listed_days = current_positions - listing_positions + 1
    listing_known = ~pd.isna(listing_dates)

    normal_status = status.copy()
    if not normal_status.empty:
        required_status = {"date", "symbol", "trade_status", "is_st"}
        if not required_status.issubset(normal_status.columns):
            raise SchemaError("Status requires date, symbol, trade_status, is_st")
        normal_status["date"] = pd.to_datetime(normal_status["date"]).dt.normalize()
        if normal_status.duplicated(["date", "symbol"]).any():
            raise SchemaError("Status contains duplicate (date, symbol) rows")
        normal_status = normal_status.set_index(["date", "symbol"]).reindex(grid)
    else:
        normal_status = pd.DataFrame(index=grid, columns=["trade_status", "is_st"])

    result = pd.DataFrame(index=grid).reset_index()
    result["listed_days"] = listed_days
    result["has_listing_date"] = listing_known
    result["listed_long_enough"] = listing_known & (listed_days >= config.min_listed_days)
    # Only an explicitly false ST flag is eligible.  Missing status remains
    # conservatively excluded, including on the factor warm-up grid.
    result["is_st"] = ~normal_status["is_st"].eq(False).fillna(False).to_numpy(dtype=bool)
    result["is_suspended"] = normal_status["trade_status"].ne(1).fillna(True).to_numpy()
    result["has_recent_volume"] = positive_count.gt(0).fillna(False).to_numpy()
    result["quality_ratio"] = (quality_count / float(config.quality_window)).to_numpy()
    result["has_sufficient_data"] = result["quality_ratio"].ge(config.min_quality_ratio)
    eligible = result["listed_long_enough"] & result["has_recent_volume"] & result["has_sufficient_data"]
    if config.exclude_st:
        eligible &= ~result["is_st"]
    if config.exclude_suspended:
        eligible &= ~result["is_suspended"]
    result["eligible"] = eligible
    return result


def apply_universe_filters(
    membership: pd.DataFrame,
    flags: pd.DataFrame,
    requested_dates: Iterable[object],
    expected_member_count: int = 500,
) -> pd.DataFrame:
    """Apply eligibility flags only after strict PIT membership validation."""

    members = validate_daily_membership(
        membership, requested_dates=requested_dates, expected_member_count=expected_member_count
    )
    merged = members.merge(flags, on=["date", "symbol"], how="left", validate="one_to_one")
    if merged["eligible"].isna().any():
        raise PointInTimeUniverseError(
            "%s: eligibility data missing for one or more members" % PIT_REQUIRED
        )
    return merged.loc[merged["eligible"]].sort_values(["date", "symbol"]).reset_index(drop=True)
