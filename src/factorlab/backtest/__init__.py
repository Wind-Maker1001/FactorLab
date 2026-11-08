"""Fast research and rule-aware daily-bar portfolio backtesting."""

from .fast import FastBacktestConfig, FastBacktestResult, FastResearchBacktester, run_fast_backtest
from .execution import (
    AShareExecutionBacktester,
    ExecutionBacktestConfig,
    ExecutionBacktestResult,
    PositionBook,
    OrderSizeRule,
    run_execution_backtest,
)
from .session import ExecutionSession, HoldingSnapshot, HoldingsSnapshot
from .fees import FeeBreakdown, FeeRule, FeeSchedule
from .rules import PriceLimitBounds, PriceLimitRule, PriceLimitSchedule, classify_board

__all__ = [
    "ExecutionSession",
    "HoldingSnapshot",
    "HoldingsSnapshot",
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
    "OrderSizeRule",
    "PriceLimitBounds",
    "PriceLimitRule",
    "PriceLimitSchedule",
    "classify_board",
    "run_execution_backtest",
    "run_fast_backtest",
]
