"""Reusable strategies, independent of model training and score ranking."""

__all__ = ["DualMAConfig", "moving_average_signals", "run_software_backtest"]


def __getattr__(name):
    if name in __all__:
        from . import dual_ma
        return getattr(dual_ma, name)
    raise AttributeError(name)
