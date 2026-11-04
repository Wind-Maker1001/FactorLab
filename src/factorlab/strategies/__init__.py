"""Reusable strategies, independent of model training and score ranking."""

from .dual_ma import DualMAConfig, moving_average_signals, run_software_backtest

__all__ = ["DualMAConfig", "moving_average_signals", "run_software_backtest"]
