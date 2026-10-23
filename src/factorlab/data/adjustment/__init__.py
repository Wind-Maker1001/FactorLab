"""Source-specific price representations, never investor return accounting."""

from .contracts import AdjustmentAnchor, AdjustmentResult, SourceAdjustmentContract
from .engine import build_hfq
from .validation import validate_adjustment

__all__ = ["AdjustmentAnchor", "AdjustmentResult", "SourceAdjustmentContract",
           "build_hfq", "validate_adjustment"]
