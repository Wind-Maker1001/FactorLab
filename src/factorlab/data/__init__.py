"""Vendor-neutral market-data layer for Qbot FactorLab."""

_EXPORTS = {
    "BarCacheSnapshot": ".cache",
    "ParquetBarCache": ".cache",
    "TradingCalendar": ".calendar",
    "reindex_bars_to_calendar": ".calendar",
    "AKShareProvider": ".providers",
    "LocalParquetProvider": ".providers",
    "MarketDataProvider": ".providers",
    "publish_local_market_data": ".providers",
    "CANONICAL_BAR_COLUMNS": ".schema",
    "canonicalize_bars": ".schema",
    "empty_bars": ".schema",
    "normalize_symbol": ".schema",
    "validate_bars": ".schema",
}

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


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    return getattr(import_module(module_name, __name__), name)


def __dir__():
    return sorted(list(globals()) + __all__)
