"""Fast research and rule-aware daily-bar portfolio backtesting."""

_EXPORTS = {
    "FastBacktestConfig": ".fast",
    "FastBacktestResult": ".fast",
    "FastResearchBacktester": ".fast",
    "run_fast_backtest": ".fast",
    "AShareExecutionBacktester": ".execution",
    "ExecutionBacktestConfig": ".execution",
    "ExecutionBacktestResult": ".execution",
    "PositionBook": ".execution",
    "OrderSizeRule": ".execution",
    "run_execution_backtest": ".execution",
    "ExecutionSession": ".session",
    "HoldingSnapshot": ".session",
    "HoldingsSnapshot": ".session",
    "FeeBreakdown": ".fees",
    "FeeRule": ".fees",
    "FeeSchedule": ".fees",
    "PriceLimitBounds": ".rules",
    "PriceLimitRule": ".rules",
    "PriceLimitSchedule": ".rules",
    "classify_board": ".rules",
}

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


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    module = import_module(module_name, __name__)
    return getattr(module, name)


def __dir__():
    return sorted(list(globals()) + __all__)
