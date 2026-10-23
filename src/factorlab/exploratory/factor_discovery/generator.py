"""Finite, data-independent economic templates for exploratory factor discovery.

The template tuple below is the frozen ordering for generator v1. Within each
template the configured window values are ascending. The duplicate probe uses
only the smallest configured window; it demonstrates commutative normalization
without creating a second computation task. There is no random search: the
frozen seed is retained by DiscoveryConfig for the run receipt.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple

from .contracts import DiscoveryConfig, DiscoveryError, ErrorCode, Node
from .engine import compile_expression


@dataclass
class GenerationResult:
    records: List[Dict[str, Any]]
    stats: Dict[str, int]


@dataclass(frozen=True)
class _Template:
    name: str
    family: str
    meaning: str
    build: Callable[[int], Node]
    first_window_only: bool = False


def _field(name: str) -> Node:
    return Node("Field", value=name)


def _rolling(op: str, field: str, window: int) -> Node:
    return Node(op, (_field(field),), param=window)


def _relative_level(field: str, window: int) -> Node:
    return Node("SafeDiv", (_field(field), _rolling("RollingMean", field, window)))


def _relative_change(field: str, window: int) -> Node:
    return Node(
        "SafeDiv",
        (
            Node("Delta", (_field(field),), param=window),
            Node("Lag", (_field(field),), param=window),
        ),
    )


def _level_interaction(window: int, reverse: bool = False) -> Node:
    operands = (_relative_level("adj_close", window), _relative_level("volume", window))
    return Node("Mul", operands[::-1] if reverse else operands)


def _lagged_rolling_ratio(op: str, field: str, window: int) -> Node:
    current = _rolling(op, field, window)
    return Node("SafeDiv", (current, Node("Lag", (current,), param=1)))


def _relative_price_range(window: int) -> Node:
    spread = Node(
        "Sub",
        (
            _rolling("RollingMean", "adj_high", window),
            _rolling("RollingMean", "adj_low", window),
        ),
    )
    return Node("SafeDiv", (spread, _field("adj_close")))


# Four economic families appear in the first six templates. All formulas have
# dimensionless final outputs and use at most two rolling operators. A complete
# default enumeration has 49 records (48 unique expressions and one audit probe).
_TEMPLATES: Tuple[_Template, ...] = (
    _Template(
        "close_relative_mean",
        "TREND_MOMENTUM",
        "复权收盘价相对过去{n}个交易日均价，描述价格趋势位置。",
        lambda n: _relative_level("adj_close", n),
    ),
    _Template(
        "mean_return_to_volatility",
        "RETURN_VOLATILITY",
        "过去{n}个交易日平均单日收益除以同期收益标准差，描述收益与风险的关系。",
        lambda n: Node(
            "SafeDiv",
            (_rolling("RollingMean", "RET1", n), _rolling("RollingStd", "RET1", n)),
        ),
    ),
    _Template(
        "volume_relative_mean",
        "ACTIVITY_DYNAMICS",
        "当日成交股数相对过去{n}个交易日均值，描述成交活跃程度。",
        lambda n: _relative_level("volume", n),
    ),
    _Template(
        "amount_relative_mean",
        "ACTIVITY_DYNAMICS",
        "当日成交金额相对过去{n}个交易日均值，描述资金成交活跃程度。",
        lambda n: _relative_level("amount", n),
    ),
    _Template(
        "turnover_relative_mean",
        "ACTIVITY_DYNAMICS",
        "当日比例换手率相对过去{n}个交易日均值，描述换手活跃程度。",
        lambda n: _relative_level("turnover", n),
    ),
    _Template(
        "price_volume_relative_levels",
        "PRICE_VOLUME_INTERACTION",
        "价格相对{n}日均价与成交量相对{n}日均量的乘积，描述价量共同偏离。",
        _level_interaction,
    ),
    _Template(
        "price_volume_commutative_audit",
        "PRICE_VOLUME_INTERACTION",
        "同一{n}日价量共同偏离表达式交换乘法操作数，仅用于结构重复审计。",
        lambda n: _level_interaction(n, reverse=True),
        first_window_only=True,
    ),
    _Template(
        "close_lagged_return",
        "TREND_MOMENTUM",
        "复权收盘价过去{n}个交易日的变化除以当时价格，描述区间价格动量。",
        lambda n: _relative_change("adj_close", n),
    ),
    _Template(
        "return_volatility_change",
        "RETURN_VOLATILITY",
        "{n}日收益波动率相对上一交易日同窗口波动率，描述风险扩张或收缩。",
        lambda n: _lagged_rolling_ratio("RollingStd", "RET1", n),
    ),
    _Template(
        "volume_lagged_change",
        "ACTIVITY_DYNAMICS",
        "成交股数相对{n}个交易日前的比例变化，描述交易活动变化。",
        lambda n: _relative_change("volume", n),
    ),
    _Template(
        "momentum_volume_interaction",
        "PRICE_VOLUME_INTERACTION",
        "{n}日价格动量与成交量相对{n}日均量的乘积，描述成交活动下的动量。",
        lambda n: Node(
            "Mul", (_relative_change("adj_close", n), _relative_level("volume", n))
        ),
    ),
    _Template(
        "amount_mean_change",
        "ACTIVITY_DYNAMICS",
        "{n}日平均成交金额相对上一交易日同窗口均值，描述成交金额趋势变化。",
        lambda n: _lagged_rolling_ratio("RollingMean", "amount", n),
    ),
    _Template(
        "mean_range_relative_close",
        "TREND_MOMENTUM",
        "{n}日复权最高价与最低价均值之差相对当日复权收盘价，描述相对价格区间。",
        _relative_price_range,
    ),
)

# Serializable public rule descriptions for receipts; the builders remain
# internal. Expressions, not a changed constant or name, define a candidate.
TEMPLATE_MANIFEST = tuple(
    {
        "name": template.name,
        "family": template.family,
        "meaning": template.meaning,
        "parameters": (
            "SMALLEST_CONFIGURED_WINDOW"
            if template.first_window_only
            else "ASCENDING_CONFIGURED_WINDOWS"
        ),
    }
    for template in _TEMPLATES
)


def _original_expression(node: Node) -> str:
    """Render our own finite AST, preserving pre-normalization operand order."""
    if node.op == "Field":
        return str(node.value)
    if node.op == "Const":
        return repr(node.value)
    args = [_original_expression(arg) for arg in node.args]
    if node.param is not None:
        args.append(str(node.param))
    return "%s(%s)" % (node.op, ",".join(args))


def generate_candidates(
    config: DiscoveryConfig = DiscoveryConfig(),
) -> GenerationResult:
    """Compile and deduplicate bounded templates, then truncate audit records.

    The only input is frozen configuration. Narrower field/operator whitelists
    and complexity limits are passed unchanged to the compiler; rejected
    templates remain visible instead of being silently substituted. Sequence
    numbers are one-based. EXACT_DUPLICATE records reference the first canonical
    expression and must not become additional computation tasks.
    """
    if not isinstance(config, DiscoveryConfig):
        raise DiscoveryError(ErrorCode.INVALID_PARAMETER, "DiscoveryConfig required")
    windows = tuple(sorted(config.windows))
    records: List[Dict[str, Any]] = []
    first_by_hash: Dict[str, str] = {}
    for template in _TEMPLATES:
        parameters = windows[:1] if template.first_window_only else windows
        for window in parameters:
            node = template.build(window)
            sequence = len(records) + 1
            record: Dict[str, Any] = {
                "candidate_id": "FD_%03d" % sequence,
                "generation_sequence": sequence,
                "family": template.family,
                "meaning": template.meaning.format(n=window),
                "original_expression": _original_expression(node),
                "node": node,
                "compiled": None,
                "status": "REJECTED",
                "rejection_reason": None,
                "duplicate_of": None,
            }
            try:
                compiled = compile_expression(node, config, require_dimensionless=True)
            except DiscoveryError as exc:
                record["rejection_reason"] = str(exc)
            else:
                record["compiled"] = compiled
                duplicate_of = first_by_hash.get(compiled.expression_sha256)
                if duplicate_of is None:
                    first_by_hash[compiled.expression_sha256] = record["candidate_id"]
                    record["status"] = "GENERATED"
                else:
                    record["status"] = "EXACT_DUPLICATE"
                    record["duplicate_of"] = duplicate_of
            records.append(record)

    enumerated = len(records)
    records = records[: config.max_candidates]
    unique = sum(record["status"] == "GENERATED" for record in records)
    duplicate = sum(record["status"] == "EXACT_DUPLICATE" for record in records)
    rejected = sum(record["status"] == "REJECTED" for record in records)
    stats = {
        "total_records": len(records),
        "generated": unique,
        "structurally_unique": unique,
        "exact_duplicates": duplicate,
        "rejected": rejected,
        "enumerated": enumerated,
        "truncated": enumerated - len(records),
        "family_count": len(
            {record["family"] for record in records if record["status"] == "GENERATED"}
        ),
        "generated_count": unique,
        "unique_count": unique,
        "duplicate_count": duplicate,
        "rejected_count": rejected,
    }
    return GenerationResult(records=records, stats=stats)
