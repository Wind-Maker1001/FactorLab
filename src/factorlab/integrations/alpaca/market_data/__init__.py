"""Explicit-feed US equity data; no order placement capability."""

_EXPORTS = {
    'Credentials': '.client',
    'MarketDataClient': '.client',
    'MarketDataError': '.client',
    'MarketCache': '.cache',
    'Series': '.cache',
    'Instrument': '.inputs',
    'InstrumentQuote': '.inputs',
    'OpeningObservation': '.inputs',
    'MarketDataService': '.service',
    'StrategyInputs': '.service',
    'PreparedHistory': '.service',
    'AlpacaSessionCalendar': '.sessions',
}

__all__ = ['Credentials', 'MarketDataClient', 'MarketDataError', 'MarketCache', 'Series',
           'Instrument', 'InstrumentQuote', 'OpeningObservation', 'MarketDataService',
           'StrategyInputs', 'PreparedHistory', 'AlpacaSessionCalendar']


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    return getattr(import_module(module_name, __name__), name)


def __dir__():
    return sorted(list(globals()) + __all__)
