"""Frozen v1 contracts shared by discovery components (Python 3.9)."""

from dataclasses import asdict, dataclass, field
from enum import Enum
import math
import re
from typing import Any, Dict, Optional, Tuple

import pandas as pd

FIELDS = (
    "adj_open",
    "adj_high",
    "adj_low",
    "adj_close",
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
    "volume",
    "amount",
    "turnover",
    "RET1",
)
OPERATORS = (
    "Field",
    "Const",
    "Lag",
    "Delta",
    "RollingMean",
    "RollingStd",
    "Add",
    "Sub",
    "Mul",
    "SafeDiv",
)
SCHEMA = "FACTOR_DISCOVERY_EXPLORATORY_V1"
GENERATOR_VERSION = "finite-economic-templates-v1"


class ErrorCode(str, Enum):
    INVALID_AST = "INVALID_AST"
    UNAUTHORIZED_NODE = "UNAUTHORIZED_NODE"
    UNAUTHORIZED_FIELD = "UNAUTHORIZED_FIELD"
    INVALID_PARAMETER = "INVALID_PARAMETER"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    PRICE_BASIS_MISMATCH = "PRICE_BASIS_MISMATCH"
    NON_DIMENSIONLESS = "NON_DIMENSIONLESS"
    COMPLEXITY_LIMIT = "COMPLEXITY_LIMIT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    INVALID_INPUT = "INVALID_INPUT"


class DiscoveryError(ValueError):
    def __init__(self, code, message):
        self.code = ErrorCode(code)
        super().__init__("%s: %s" % (self.code.value, message))


@dataclass(frozen=True)
class Node:
    op: str
    args: Tuple["Node", ...] = ()
    value: Any = None
    param: Optional[int] = None


@dataclass(frozen=True)
class DiscoveryConfig:
    development_start: str = "2023-07-03"
    development_end: str = "2023-12-15"
    generator_version: str = GENERATOR_VERSION
    seed: int = 20261014
    operators: Tuple[str, ...] = OPERATORS
    fields: Tuple[str, ...] = FIELDS
    windows: Tuple[int, ...] = (5, 10, 20, 60)
    lags: Tuple[int, ...] = (1, 5, 10, 20, 60)
    max_depth: int = 5
    max_nodes: int = 15
    max_rolling_operators: int = 2
    max_lookback: int = 130
    max_candidates: int = 40
    max_panel_rows: int = 250000
    max_cell_operations: int = 50000000
    max_cached_subexpressions: int = 2000
    max_cache_bytes: int = 134217728
    max_diagnostic_records: int = 500000
    max_expression_seconds: float = 10.0
    max_total_seconds: float = 120.0
    minimum_cross_section: int = 20
    minimum_evaluation_dates: int = 60
    minimum_pair_coverage: float = 0.8
    redundancy_threshold: float = 0.98
    minimum_candidate_coverage: float = 0.8
    mad_multiplier: float = 5.0
    holding_days: int = 5
    quantiles: int = 5
    ordering: str = "TEMPLATE_ORDER_THEN_ASCENDING_PARAMETERS_FIRST_UNIQUE"

    def __post_init__(self):
        for name in ("development_start", "development_end"):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", value
            ):
                raise DiscoveryError(
                    ErrorCode.INVALID_PARAMETER, "ISO date required: " + name
                )
            try:
                pd.Timestamp(value)
            except ValueError as exc:
                raise DiscoveryError(ErrorCode.INVALID_PARAMETER, name) from exc
        if self.development_start > self.development_end:
            raise DiscoveryError(
                ErrorCode.INVALID_PARAMETER, "development dates reversed"
            )
        for name, ceiling in (
            ("max_depth", 5),
            ("max_nodes", 15),
            ("max_rolling_operators", 2),
            ("max_lookback", 130),
            ("max_candidates", 50),
            ("max_panel_rows", 250000),
            ("max_cell_operations", 50000000),
            ("max_cached_subexpressions", 2000),
            ("max_cache_bytes", 134217728),
            ("max_diagnostic_records", 500000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise DiscoveryError(
                    ErrorCode.INVALID_PARAMETER, "invalid bounded " + name
                )
        for name in (
            "minimum_cross_section",
            "minimum_evaluation_dates",
            "holding_days",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise DiscoveryError(ErrorCode.INVALID_PARAMETER, name)
        if type(self.seed) is not int or self.generator_version != GENERATOR_VERSION:
            raise DiscoveryError(ErrorCode.INVALID_PARAMETER, "seed/generator version")
        if (
            self.quantiles != 5
            or self.ordering != "TEMPLATE_ORDER_THEN_ASCENDING_PARAMETERS_FIRST_UNIQUE"
        ):
            raise DiscoveryError(
                ErrorCode.INVALID_PARAMETER, "frozen quantiles/ordering"
            )
        for name, allowed in (
            ("operators", OPERATORS),
            ("fields", FIELDS),
            ("windows", (5, 10, 20, 60)),
            ("lags", (1, 5, 10, 20, 60)),
        ):
            values = getattr(self, name)
            if (
                type(values) is not tuple
                or not values
                or len(set(values)) != len(values)
                or any(value not in allowed for value in values)
                or (
                    name in ("windows", "lags")
                    and any(type(value) is not int for value in values)
                )
            ):
                raise DiscoveryError(
                    ErrorCode.INVALID_PARAMETER, "invalid whitelist " + name
                )
        for name in ("max_expression_seconds", "max_total_seconds", "mad_multiplier"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise DiscoveryError(ErrorCode.INVALID_PARAMETER, name)
        for name in (
            "minimum_pair_coverage",
            "redundancy_threshold",
            "minimum_candidate_coverage",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not math.isfinite(value)
                or not 0 < value <= 1
            ):
                raise DiscoveryError(ErrorCode.INVALID_PARAMETER, name)

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class CompiledExpression:
    node: Node
    canonical_ast: Dict[str, Any]
    canonical_json: str
    expression: str
    expression_sha256: str
    dimension: Any
    complexity: Dict[str, int]
    lookback: int


@dataclass
class ExecutionResult:
    values: pd.Series
    status: str
    reason_counts: Dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    estimated_cell_operations: int = 0
    valid_count: int = 0
    cache_hits: int = 0
    peak_cache_bytes: int = 0
