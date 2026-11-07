"""Leakage-safe time-series validation tools for FactorLab."""

from factorlab.validation.factor_selection import (
    FactorSelectionConfig, FactorSelectionError, FactorSelectionResult, TrainFactorSelector,
)

from factorlab.validation.splitter import (
    BoundaryAudit,
    ExpandingWalkForwardSplitter,
    WalkForwardError,
    WalkForwardFold,
)
from factorlab.validation.walk_forward import (
    DIAGNOSTIC_STAGES,
    diagnostic_frame_sha256,
    WalkForwardResult,
    WalkForwardRunError,
    WalkForwardRunner,
    run_walk_forward,
)

__all__ = [
    "FactorSelectionConfig",
    "FactorSelectionError",
    "FactorSelectionResult",
    "TrainFactorSelector",
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
