"""Vendor-neutral market-data layer for Qbot FactorLab."""

from .cache import BarCacheSnapshot, ParquetBarCache
from .calendar import TradingCalendar, reindex_bars_to_calendar
from .providers import (
    AKShareProvider, LocalParquetProvider, MarketDataProvider,
    publish_local_market_data,
)
from .schema import (
    CANONICAL_BAR_COLUMNS,
    canonicalize_bars,
    empty_bars,
    normalize_symbol,
    validate_bars,
)

__all__ = [
    "AKShareProvider",
    "BarCacheSnapshot",
    "CANONICAL_BAR_COLUMNS",
    "LocalParquetProvider",
    "MarketDataProvider",
    "ParquetBarCache",
    "TradingCalendar",
    "canonicalize_bars",
    "empty_bars",
    "normalize_symbol",
    "publish_local_market_data",
    "reindex_bars_to_calendar",
    "validate_bars",
]
