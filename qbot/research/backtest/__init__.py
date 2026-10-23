"""Fast research and rule-aware daily-bar portfolio backtesting."""

from .fast import FastBacktestConfig, FastBacktestResult, FastResearchBacktester, run_fast_backtest
from .execution import (
    AShareExecutionBacktester,
    ExecutionBacktestConfig,
    ExecutionBacktestResult,
    PositionBook,
    run_execution_backtest,
)
from .fees import FeeBreakdown, FeeRule, FeeSchedule
from .rules import PriceLimitBounds, PriceLimitRule, PriceLimitSchedule, classify_board

__all__ = [
    "AShareExecutionBacktester",
    "ExecutionBacktestConfig",
    "ExecutionBacktestResult",
    "FastBacktestConfig",
    "FastBacktestResult",
    "FastResearchBacktester",
    "FeeBreakdown",
    "FeeRule",
    "FeeSchedule",
    "PositionBook",
    "PriceLimitBounds",
    "PriceLimitRule",
    "PriceLimitSchedule",
    "classify_board",
    "run_execution_backtest",
    "run_fast_backtest",
]
