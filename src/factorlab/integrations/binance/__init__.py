"""Read-only Binance TradFi collection; NO LIVE MONEY.

PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED.
"""

_EXPORTS = {
    'PublicClient': '.client',
    'Collector': '.collector',
    'Reader': '.store',
}

__all__ = ['Collector', 'PublicClient', 'Reader']


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    return getattr(import_module(module_name, __name__), name)


def __dir__():
    return sorted(list(globals()) + __all__)
