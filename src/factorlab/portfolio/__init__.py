"""Cross-sectional portfolio construction utilities."""

_EXPORTS = {
    "TopKPortfolio": ".topk",
    "rebalance_dates": ".topk",
    "select_top_k": ".topk",
    "equal_weights": ".weights",
    "OptimizationError": ".optimizer",
    "OptimizationInput": ".optimizer",
    "OptimizationResult": ".optimizer",
    "OptimizerConfig": ".optimizer",
    "PortfolioOptimizer": ".optimizer",
    "ExecutionFeedback": ".feedback",
    "FeedbackDecision": ".feedback",
    "SaleRestriction": ".feedback",
}

__all__ = ["ExecutionFeedback", "FeedbackDecision", "SaleRestriction", "TopKPortfolio", "equal_weights", "rebalance_dates", "select_top_k",
           "OptimizationError", "OptimizationInput", "OptimizationResult",
           "OptimizerConfig", "PortfolioOptimizer"]


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    return getattr(import_module(module_name, __name__), name)


def __dir__():
    return sorted(list(globals()) + __all__)
