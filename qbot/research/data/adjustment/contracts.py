"""Explicit adjustment methods, proved anchors and independent validation."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Tuple

import pandas as pd


AFFINE = "SOURCE_AFFINE_HFQ_V1"
RATIO = "SOURCE_RATIO_HFQ_V1"


@dataclass(frozen=True)
class AdjustmentAnchor:
    security_id: str
    date: str
    method: str
    multiplier: Decimal
    offset: Decimal
    available_at: str
    source_sha256: str
    synthetic_only: bool = False


@dataclass(frozen=True)
class AdjustmentResult:
    bars: pd.DataFrame
    coefficients: pd.DataFrame
    method: str
    evidence: Dict[str, Any]
    issues: Tuple[str, ...]
    synthetic_only: bool


@dataclass(frozen=True)
class SourceAdjustmentContract:
    source_id: str
    source_version: str
    method: str
    anchor_sha256: str
    reference_sha256: str
    # Only an independently established nearest-rounding contract permits half a quantum.
    rounding: str = "EXACT"
    price_quantum: Decimal = Decimal(0)
    synthetic_only: bool = False


@dataclass(frozen=True)
class ValidationReport:
    status: str
    method: str
    result_sha256: str
    reference_sha256: str
    source_contract_sha256: str
    checked_rows: int
    max_absolute_error: float
    issues: Tuple[str, ...]
    synthetic_only: bool
