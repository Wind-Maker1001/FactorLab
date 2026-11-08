"""Evaluation-only research results with explicit comparison contracts."""

from .research import (
    EvaluationContext, EvaluationResult, MetricValue, ResearchEvaluation,
    compare_results, paired_inference, reprice_costs,
)

__all__ = [
    "EvaluationContext", "EvaluationResult", "MetricValue", "ResearchEvaluation",
    "compare_results", "paired_inference", "reprice_costs",
]
