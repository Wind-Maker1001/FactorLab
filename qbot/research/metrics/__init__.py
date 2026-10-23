"""Public research-metric APIs for factors, predictions, and portfolios."""

from .factor import FactorEvaluation, evaluate_factors
from .portfolio import calculate_portfolio_metrics
from .prediction import (
    PredictionEvaluation,
    PredictionMetricError,
    evaluate_predictions,
)
from .robustness import (
    RobustnessError,
    RobustnessEvaluation,
    RobustnessOptions,
    evaluate_model_robustness,
    holm_adjust,
    inference_protocol,
    mean_uncertainty,
)
from .risk import (
    annualized_return,
    annualized_volatility,
    calmar_ratio,
    cumulative_return,
    drawdown_series,
    information_ratio,
    maximum_drawdown,
    sharpe_ratio,
    sortino_ratio,
    tracking_error,
)

__all__ = [
    "FactorEvaluation",
    "PredictionEvaluation",
    "PredictionMetricError",
    "RobustnessError",
    "RobustnessEvaluation",
    "RobustnessOptions",
    "annualized_return",
    "annualized_volatility",
    "calculate_portfolio_metrics",
    "calmar_ratio",
    "cumulative_return",
    "drawdown_series",
    "evaluate_factors",
    "evaluate_predictions",
    "evaluate_model_robustness",
    "holm_adjust",
    "inference_protocol",
    "mean_uncertainty",
    "information_ratio",
    "maximum_drawdown",
    "sharpe_ratio",
    "sortino_ratio",
    "tracking_error",
]
