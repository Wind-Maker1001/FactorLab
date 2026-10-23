"""Leakage-safe time-series validation tools for FactorLab."""

from qbot.research.validation.splitter import (
    BoundaryAudit,
    ExpandingWalkForwardSplitter,
    WalkForwardError,
    WalkForwardFold,
)
from qbot.research.validation.walk_forward import (
    DIAGNOSTIC_STAGES,
    diagnostic_frame_sha256,
    WalkForwardResult,
    WalkForwardRunError,
    WalkForwardRunner,
    run_walk_forward,
)

__all__ = [
    "BoundaryAudit",
    "DIAGNOSTIC_STAGES",
    "diagnostic_frame_sha256",
    "ExpandingWalkForwardSplitter",
    "WalkForwardError",
    "WalkForwardFold",
    "WalkForwardResult",
    "WalkForwardRunError",
    "WalkForwardRunner",
    "run_walk_forward",
]
