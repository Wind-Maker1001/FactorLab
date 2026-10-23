"""Bounded internal AST interpreter for exploratory, close-known signals.

Input rows may arrive in any order: keys are checked, sorted on a separate
copy, and expanded on the explicit calendar without filling observations.
Results always retain the caller's original, unique index. No extra input
columns (including supplied RET1, labels, or adjustment metadata) are read.
"""

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import math
import re
import time
from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd

from factorlab.data.calendar import TradingCalendar
from factorlab.data.schema import normalize_symbol, validate_bars
from factorlab.errors import SchemaError

from .contracts import (
    FIELDS,
    OPERATORS,
    CompiledExpression,
    DiscoveryConfig,
    DiscoveryError,
    ErrorCode,
    ExecutionResult,
    Node,
)


# Separate adjusted currency prevents implicit conversion of HFQ to raw cash.
# Coordinate order: raw currency, adjusted currency, shares.
_DIMENSIONLESS = (0, 0, 0)
_MARKET_FIELDS = tuple(name for name in FIELDS if name != "RET1")
_BINARY = frozenset(("Add", "Sub", "Mul", "SafeDiv"))
_ROLLING = frozenset(("RollingMean", "RollingStd"))
_TEMPORAL = _ROLLING | frozenset(("Lag", "Delta"))
_SCALES = frozenset((0, 1, 100))


@dataclass(frozen=True)
class Dimension:
    """Physical units, economic value type, and retained price provenance."""

    units: Tuple[int, int, int] = _DIMENSIONLESS
    value_type: str = "dimensionless"
    price_bases: Tuple[str, ...] = ()

    @property
    def is_dimensionless(self):
        return self.units == _DIMENSIONLESS

    def as_dict(self):
        return {
            "value_type": self.value_type,
            "units": dict(
                zip(("raw_currency", "adjusted_currency", "shares"), self.units)
            ),
            "price_basis": self.price_bases[0] if self.price_bases else None,
            "is_dimensionless": self.is_dimensionless,
        }


def _field_dimension(name):
    if name.startswith("adj_"):
        return Dimension((0, 1, -1), "adjusted_price", ("HFQ",))
    if name.startswith("raw_"):
        return Dimension((1, 0, -1), "raw_price", ("RAW",))
    if name == "volume":
        return Dimension((0, 0, 1), "shares")
    if name == "amount":
        return Dimension((1, 0, 0), "amount")
    if name == "turnover":
        return Dimension(value_type="turnover")
    return Dimension(value_type="return", price_bases=("HFQ",))


def _check_config(config):
    if type(config) is not DiscoveryConfig:
        raise DiscoveryError(ErrorCode.INVALID_PARAMETER, "DiscoveryConfig required")
    # Frozen dataclasses can still be changed with object.__setattr__. Recheck
    # bounds instead of treating a user-provided object as trusted bytecode.
    config.__post_init__()


def _validate_shape(node, config):
    """Iteratively reject cycles, malformed shapes, and oversized trees first."""
    active = set()
    stack = [(node, 1, False)]
    nodes = rolling = depth = 0
    while stack:
        current, level, leaving = stack.pop()
        if leaving:
            active.remove(id(current))
            continue
        if type(current) is not Node:
            raise DiscoveryError(
                ErrorCode.INVALID_AST, "only internal Node instances allowed"
            )
        if id(current) in active:
            raise DiscoveryError(ErrorCode.INVALID_AST, "AST contains a cycle")
        nodes += 1
        depth = max(depth, level)
        if nodes > config.max_nodes or level > config.max_depth:
            raise DiscoveryError(ErrorCode.COMPLEXITY_LIMIT, "AST node/depth limit")
        if (
            type(current.op) is not str
            or current.op not in OPERATORS
            or current.op not in config.operators
        ):
            raise DiscoveryError(
                ErrorCode.UNAUTHORIZED_NODE, "operator is not whitelisted"
            )
        if type(current.args) is not tuple:
            raise DiscoveryError(ErrorCode.INVALID_AST, "Node.args must be a tuple")
        arity = 2 if current.op in _BINARY else 1 if current.op in _TEMPORAL else 0
        if len(current.args) != arity:
            raise DiscoveryError(ErrorCode.INVALID_AST, "operator arity mismatch")
        if current.op == "Field":
            if (
                type(current.value) is not str
                or current.value not in config.fields
                or current.value not in FIELDS
            ):
                raise DiscoveryError(
                    ErrorCode.UNAUTHORIZED_FIELD, "field is not whitelisted"
                )
            if current.param is not None:
                raise DiscoveryError(
                    ErrorCode.INVALID_AST, "Field cannot have a parameter"
                )
        elif current.op == "Const":
            if (
                type(current.value) not in (int, float)
                or not math.isfinite(current.value)
                or current.value not in _SCALES
            ):
                raise DiscoveryError(
                    ErrorCode.INVALID_PARAMETER, "fixed constants are 0, 1, and 100"
                )
            if current.param is not None:
                raise DiscoveryError(
                    ErrorCode.INVALID_AST, "Const cannot have a parameter"
                )
        else:
            if current.value is not None:
                raise DiscoveryError(
                    ErrorCode.INVALID_AST, "operator cannot carry a value"
                )
            if current.op in _TEMPORAL:
                allowed = config.windows if current.op in _ROLLING else config.lags
                if type(current.param) is not int or current.param not in allowed:
                    raise DiscoveryError(
                        ErrorCode.INVALID_PARAMETER,
                        "fixed positive temporal parameter required",
                    )
            elif current.param is not None:
                raise DiscoveryError(
                    ErrorCode.INVALID_AST, "binary operator cannot carry a parameter"
                )
        rolling += int(current.op in _ROLLING)
        if rolling > config.max_rolling_operators:
            raise DiscoveryError(ErrorCode.COMPLEXITY_LIMIT, "rolling operator limit")
        active.add(id(current))
        stack.append((current, level, True))
        stack.extend((child, level + 1, False) for child in reversed(current.args))
    return {"depth": depth, "nodes": nodes, "rolling_operators": rolling}


def _json(ast):
    return json.dumps(
        ast, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _canonical(current):
    if current.op == "Field":
        return {"op": "Field", "value": current.value}
    if current.op == "Const":
        return {"op": "Const", "value": int(current.value)}
    children = [_canonical(child) for child in current.args]
    # Binary commutativity is safe. Associativity, subtraction, and division
    # rewrites are deliberately absent (floating-point evaluation matters).
    if current.op in ("Add", "Mul"):
        children.sort(key=_json)
    result = {"op": current.op, "args": children}
    if current.param is not None:
        result["param"] = current.param
    return result


def normalize_ast(node):
    config = DiscoveryConfig()
    _validate_shape(node, config)
    return _canonical(node)


def _to_node(ast):
    return Node(
        ast["op"],
        tuple(_to_node(child) for child in ast.get("args", ())),
        ast.get("value"),
        ast.get("param"),
    )


def _expression(ast):
    if ast["op"] == "Field":
        return ast["value"]
    if ast["op"] == "Const":
        return str(ast["value"])
    arguments = [_expression(child) for child in ast["args"]]
    if "param" in ast:
        arguments.append(str(ast["param"]))
    return "%s(%s)" % (ast["op"], ",".join(arguments))


def _infer(node):
    if node.op == "Field":
        return _field_dimension(node.value), int(node.value == "RET1"), True
    if node.op == "Const":
        return Dimension(), 0, False
    children = [_infer(child) for child in node.args]
    lookback = max(child[1] for child in children)
    has_field = any(child[2] for child in children)
    first = children[0][0]
    if node.op in _TEMPORAL:
        lookback += node.param - 1 if node.op in _ROLLING else node.param
        return first, lookback, has_field
    second = children[1][0]
    bases = tuple(sorted(set(first.price_bases + second.price_bases)))
    if len(bases) > 1:
        raise DiscoveryError(
            ErrorCode.PRICE_BASIS_MISMATCH, "RAW and HFQ provenance cannot mix"
        )
    if node.op in ("Add", "Sub"):
        if first.units != second.units:
            raise DiscoveryError(
                ErrorCode.TYPE_MISMATCH,
                "addition/subtraction requires equal dimensions",
            )
        semantic = {first.value_type, second.value_type} - {"dimensionless"}
        if len(semantic) > 1:
            raise DiscoveryError(
                ErrorCode.TYPE_MISMATCH, "incompatible economic value types"
            )
        value_type = next(iter(semantic)) if semantic else "dimensionless"
        # Price ratios minus the fixed unity scale are explicit past returns.
        if (
            node.op == "Sub"
            and first.is_dimensionless
            and first.price_bases
            and node.args[1].op == "Const"
            and node.args[1].value == 1
        ):
            value_type = "return"
        return Dimension(first.units, value_type, bases), lookback, has_field
    sign = 1 if node.op == "Mul" else -1
    units = tuple(left + sign * right for left, right in zip(first.units, second.units))
    value_type = "dimensionless" if units == _DIMENSIONLESS else "derived"
    if node.args[1].op == "Const":
        value_type = first.value_type
    elif node.op == "Mul" and node.args[0].op == "Const":
        value_type = second.value_type
    # A cash/share quotient is a RAW traded price even without a raw_* leaf.
    # Preserve its basis after later ratios cancel the physical dimensions.
    if units[0] and units[1]:
        raise DiscoveryError(
            ErrorCode.PRICE_BASIS_MISMATCH,
            "raw and adjusted monetary dimensions cannot mix",
        )
    raw_price_power = units[0] != 0 and units[1] == 0 and units[2] == -units[0]
    adj_price_power = units[1] != 0 and units[0] == 0 and units[2] == -units[1]
    if raw_price_power or adj_price_power:
        basis = "RAW" if raw_price_power else "HFQ"
        bases = tuple(sorted(set(bases + (basis,))))
        if len(bases) > 1:
            raise DiscoveryError(
                ErrorCode.PRICE_BASIS_MISMATCH, "inferred RAW and HFQ prices cannot mix"
            )
        if units in ((1, 0, -1), (0, 1, -1)):
            value_type = "raw_price" if basis == "RAW" else "adjusted_price"
    return Dimension(units, value_type, bases), lookback, has_field


def compile_expression(node, config=DiscoveryConfig(), require_dimensionless=True):
    _check_config(config)
    if type(require_dimensionless) is not bool:
        raise DiscoveryError(
            ErrorCode.INVALID_PARAMETER, "require_dimensionless must be bool"
        )
    complexity = _validate_shape(node, config)
    dimension, lookback, has_field = _infer(node)
    if not has_field:
        raise DiscoveryError(
            ErrorCode.INVALID_AST, "expression must use a market field"
        )
    if lookback > config.max_lookback:
        raise DiscoveryError(ErrorCode.COMPLEXITY_LIMIT, "effective lookback limit")
    if require_dimensionless and not dimension.is_dimensionless:
        raise DiscoveryError(
            ErrorCode.NON_DIMENSIONLESS, "final candidate must be dimensionless"
        )
    canonical = _canonical(node)
    canonical_json = _json(canonical)
    # Execute the canonical operand order too: equal hashes have equal numeric
    # semantics, including any operation-specific diagnostics.
    canonical_node = _to_node(canonical)
    return CompiledExpression(
        canonical_node,
        canonical,
        canonical_json,
        _expression(canonical),
        hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
        dimension,
        complexity,
        lookback,
    )


def _strict_dates(values, name):
    values = pd.Series(values, copy=False)
    if pd.api.types.is_numeric_dtype(values.dtype):
        raise DiscoveryError(
            ErrorCode.INVALID_INPUT, "%s cannot be numeric dates" % name
        )
    for value in values:
        if isinstance(value, str):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise DiscoveryError(
                    ErrorCode.INVALID_INPUT, "%s requires ISO dates" % name
                )
        elif not isinstance(value, (date, datetime, pd.Timestamp, np.datetime64)):
            raise DiscoveryError(
                ErrorCode.INVALID_INPUT, "%s contains invalid dates" % name
            )
        try:
            stamp = pd.Timestamp(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DiscoveryError(
                ErrorCode.INVALID_INPUT, "%s contains invalid dates" % name
            ) from exc
        if pd.isna(stamp) or stamp.tzinfo is not None or stamp != stamp.normalize():
            raise DiscoveryError(
                ErrorCode.INVALID_INPUT, "%s requires naive midnight dates" % name
            )
    try:
        return pd.DatetimeIndex(pd.to_datetime(values, errors="raise"), name="date")
    except (TypeError, ValueError, OverflowError) as exc:
        raise DiscoveryError(
            ErrorCode.INVALID_INPUT, "%s contains invalid dates" % name
        ) from exc


def _strict_symbols(values):
    result = []
    for value in values:
        if type(value) is not str:
            raise DiscoveryError(
                ErrorCode.INVALID_INPUT, "canonical string symbol required"
            )
        try:
            normalized = normalize_symbol(value)
        except SchemaError as exc:
            raise DiscoveryError(ErrorCode.INVALID_INPUT, str(exc)) from exc
        if normalized != value:
            raise DiscoveryError(
                ErrorCode.INVALID_INPUT, "symbols must already be canonical"
            )
        result.append(value)
    return result


def _validate_market_copy(frame):
    """Reuse schema on a whitelist-only validation copy, without filling data.

    Nonfinite observations were already sanitized. Partial OHLC missingness is
    legal for expression execution. Check every available price relationship,
    then mask partial blocks only in this schema-validation copy, whose normal
    contract otherwise requires complete or wholly missing OHLC blocks.
    """
    checked = frame.copy()
    for prefix in ("raw_", "adj_"):
        columns = [prefix + field for field in ("open", "high", "low", "close")]
        block = checked[columns]
        open_, high, low, close = (block[column] for column in columns)
        invalid = (
            (block <= 0).any(axis=1)
            | (high < open_)
            | (high < close)
            | (low > open_)
            | (low > close)
            | (high < low)
        )
        if invalid.any():
            raise DiscoveryError(
                ErrorCode.INVALID_INPUT, "invalid %sOHLC relationship" % prefix
            )
        partial = block.isna().any(axis=1) & block.notna().any(axis=1)
        checked.loc[partial, columns] = np.nan
    for field in ("open", "high", "low", "close"):
        checked[field] = checked["raw_" + field]
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        checked["adj_factor"] = checked["adj_close"] / checked["raw_close"]
    factor = checked["adj_factor"].to_numpy(dtype=float)
    checked.loc[~np.isfinite(factor), "adj_factor"] = np.nan
    checked = checked.sort_values(["date", "symbol"], kind="mergesort").reset_index(
        drop=True
    )
    try:
        validate_bars(checked, allow_missing_ohlc=True)
    except SchemaError as exc:
        raise DiscoveryError(ErrorCode.INVALID_INPUT, str(exc)) from exc


@dataclass
class _CacheEntry:
    values: np.ndarray
    # Reasons belong to individual canonical subexpressions. Merging by key
    # counts repeated children once, matching their cached execution.
    diagnostics: Dict[str, Dict[str, int]]


class ExpressionEngine:
    """Calendar-dense, independent stock execution with cooperative budgets."""

    def __init__(self, bars, calendar, config=DiscoveryConfig(), universe=None):
        _check_config(config)
        self.config = config
        if not isinstance(bars, pd.DataFrame) or bars.empty:
            raise DiscoveryError(
                ErrorCode.INVALID_INPUT, "nonempty bars DataFrame required"
            )
        if not bars.columns.is_unique or not bars.index.is_unique:
            raise DiscoveryError(ErrorCode.INVALID_INPUT, "duplicate columns or index")
        if isinstance(bars.index, pd.MultiIndex):
            null_index = bars.index.to_frame(index=False).isna().any().any()
        else:
            null_index = bars.index.hasnans
        if null_index:
            raise DiscoveryError(ErrorCode.INVALID_INPUT, "missing index values")
        required = ("date", "symbol") + _MARKET_FIELDS
        if not set(required).issubset(bars.columns):
            raise DiscoveryError(
                ErrorCode.INVALID_INPUT,
                "explicit raw/adjusted whitelist market fields required",
            )
        if len(bars) > config.max_panel_rows:
            raise DiscoveryError(ErrorCode.BUDGET_EXCEEDED, "input row limit")
        # Selection happens before copying or inspecting any data columns.
        frame = bars.loc[:, list(required)].copy()
        frame["date"] = _strict_dates(frame["date"], "bars").to_numpy()
        frame["symbol"] = _strict_symbols(frame["symbol"])
        if frame.duplicated(["date", "symbol"]).any():
            raise DiscoveryError(
                ErrorCode.INVALID_INPUT, "duplicate security/date keys"
            )
        if isinstance(calendar, TradingCalendar):
            calendar = calendar.dates
        if not isinstance(calendar, (list, tuple, pd.Index, np.ndarray, pd.Series)):
            raise DiscoveryError(
                ErrorCode.INVALID_INPUT, "explicit finite calendar required"
            )
        if len(calendar) > config.max_panel_rows:
            raise DiscoveryError(ErrorCode.BUDGET_EXCEEDED, "calendar row limit")
        self.calendar = _strict_dates(calendar, "calendar")
        if (
            self.calendar.empty
            or self.calendar.has_duplicates
            or not self.calendar.is_monotonic_increasing
        ):
            raise DiscoveryError(
                ErrorCode.INVALID_INPUT, "calendar must be unique and increasing"
            )
        if not frame["date"].isin(self.calendar).all():
            raise DiscoveryError(
                ErrorCode.INVALID_INPUT, "bar date outside explicit calendar"
            )
        actual = set(frame["symbol"])
        if universe is None:
            symbols = sorted(actual)
        else:
            if not isinstance(universe, (list, tuple, pd.Index)) or not len(universe):
                raise DiscoveryError(
                    ErrorCode.INVALID_INPUT, "explicit finite universe required"
                )
            symbols = _strict_symbols(universe)
            if len(set(symbols)) != len(symbols) or not actual.issubset(set(symbols)):
                raise DiscoveryError(
                    ErrorCode.INVALID_INPUT, "universe mismatch/duplicates"
                )
            symbols = sorted(symbols)
        panel_rows = len(self.calendar) * len(symbols)
        if panel_rows > config.max_panel_rows:
            raise DiscoveryError(ErrorCode.BUDGET_EXCEEDED, "dense panel row limit")
        nonfinite = {}
        for column in _MARKET_FIELDS:
            source = frame[column]
            if (
                not pd.api.types.is_numeric_dtype(source.dtype)
                or pd.api.types.is_complex_dtype(source.dtype)
                or pd.api.types.is_bool_dtype(source.dtype)
            ):
                raise DiscoveryError(
                    ErrorCode.INVALID_INPUT,
                    "%s must contain real numeric values" % column,
                )
            values = source.to_numpy(dtype=np.float64, na_value=np.nan)
            nonfinite[column] = np.isinf(values)
            values[~np.isfinite(values)] = np.nan
            frame[column] = values
        _validate_market_copy(frame)
        panel_index = pd.MultiIndex.from_product(
            [self.calendar, symbols], names=["date", "symbol"]
        )
        original_keys = pd.MultiIndex.from_frame(frame[["date", "symbol"]])
        self._original_index = bars.index.copy()
        self._original_positions = panel_index.get_indexer(original_keys)
        self.panel = (
            frame.set_index(["date", "symbol"]).reindex(panel_index).reset_index()
        )
        self._shape = (len(self.calendar), len(symbols))
        self._row_count = panel_rows
        self._inputs = {}
        self._input_nonfinite = {}
        for column in _MARKET_FIELDS:
            self._inputs[column] = self.panel[column].to_numpy(dtype=float, copy=True)
            mask = np.zeros(panel_rows, dtype=bool)
            mask[self._original_positions] = nonfinite[column]
            self._input_nonfinite[column] = mask
        self._cache = {}
        self._cache_hits = 0
        self._cell_operations = 0
        self._cache_bytes = 0
        self._total_seconds = 0.0

    @staticmethod
    def _weight(node):
        if node.op in _ROLLING:
            return node.param
        if node.op == "Delta":
            return 2
        return 4 if node.op == "Field" and node.value == "RET1" else 1

    def _estimate(self, node):
        unique = {}
        stack = [node]
        while stack:
            current = stack.pop()
            unique[_json(_canonical(current))] = self._weight(current)
            stack.extend(current.args)
        return self._row_count * sum(unique.values())

    def _check_budget(self, started):
        elapsed = time.perf_counter() - started
        if (
            elapsed >= self.config.max_expression_seconds
            or self._total_seconds + elapsed >= self.config.max_total_seconds
        ):
            raise DiscoveryError(
                ErrorCode.BUDGET_EXCEEDED, "cooperative execution time limit"
            )

    def _lag(self, values, periods):
        result = np.full(self._shape, np.nan)
        if periods < self._shape[0]:
            result[periods:] = values.reshape(self._shape)[:-periods]
        return result.ravel()

    def _evaluate(self, node, started):
        self._check_budget(started)
        key = _json(_canonical(node))
        if key in self._cache:
            self._cache_hits += 1
            return self._cache[key]
        cost = self._row_count * self._weight(node)
        if self._cell_operations + cost > self.config.max_cell_operations:
            raise DiscoveryError(
                ErrorCode.BUDGET_EXCEEDED, "total cell operation limit"
            )
        if len(self._cache) >= self.config.max_cached_subexpressions:
            raise DiscoveryError(
                ErrorCode.BUDGET_EXCEEDED, "cached subexpression limit"
            )
        if self._cache_bytes + self._row_count * 8 > self.config.max_cache_bytes:
            raise DiscoveryError(ErrorCode.BUDGET_EXCEEDED, "cached array byte limit")
        self._cell_operations += cost
        children = [self._evaluate(child, started) for child in node.args]
        reasons = {}
        diagnostics = {}
        for child in children:
            diagnostics.update(child.diagnostics)
        eligible = None
        with np.errstate(
            over="ignore", divide="ignore", invalid="ignore", under="ignore"
        ):
            if node.op == "Field":
                name = "adj_close" if node.value == "RET1" else node.value
                values = self._inputs[name].copy()
                reasons["INPUT_NONFINITE"] = int(self._input_nonfinite[name].sum())
                reasons["INPUT_MISSING"] = (
                    int(np.isnan(values).sum()) - reasons["INPUT_NONFINITE"]
                )
                if node.value == "RET1":
                    previous = self._lag(values, 1)
                    eligible = np.isfinite(values) & np.isfinite(previous)
                    reasons["HISTORY_OR_GAP_MISSING"] = int(
                        (np.isfinite(values) & ~np.isfinite(previous)).sum()
                    )
                    zero = previous == 0
                    reasons["ZERO_DENOMINATOR"] = int((eligible & zero).sum())
                    eligible &= ~zero
                    values = values / np.where(zero, np.nan, previous) - 1.0
            elif node.op == "Const":
                values = np.full(self._row_count, float(node.value))
            elif node.op in ("Lag", "Delta"):
                source = children[0].values
                lagged = self._lag(source, node.param)
                reasons["HISTORY_OR_GAP_MISSING"] = int(
                    (np.isfinite(source) & ~np.isfinite(lagged)).sum()
                )
                eligible = np.isfinite(lagged)
                values = lagged
                if node.op == "Delta":
                    eligible &= np.isfinite(source)
                    values = source - lagged
            elif node.op in _ROLLING:
                source = children[0].values.reshape(self._shape)
                rolling = pd.DataFrame(source).rolling(
                    node.param, min_periods=node.param
                )
                transformed = (
                    rolling.mean() if node.op == "RollingMean" else rolling.std(ddof=1)
                )
                values = transformed.to_numpy(dtype=float).ravel()
                count = (
                    pd.DataFrame(np.isfinite(source).astype(float))
                    .rolling(node.param, min_periods=node.param)
                    .sum()
                    .to_numpy()
                    .ravel()
                )
                eligible = count == node.param
                reasons["HISTORY_OR_GAP_MISSING"] = int((~eligible).sum())
            else:
                left, right = (child.values for child in children)
                eligible = np.isfinite(left) & np.isfinite(right)
                if node.op == "Add":
                    values = left + right
                elif node.op == "Sub":
                    values = left - right
                elif node.op == "Mul":
                    values = left * right
                else:
                    zero = right == 0
                    reasons["ZERO_DENOMINATOR"] = int((eligible & zero).sum())
                    eligible &= ~zero
                    values = left / np.where(zero, np.nan, right)
        invalid = ~np.isfinite(values)
        if eligible is not None:
            reasons["NONFINITE_RESULT"] = int((invalid & eligible).sum())
        values[invalid] = np.nan
        self._check_budget(started)
        # Children may have consumed the remaining cache slots.
        if len(self._cache) >= self.config.max_cached_subexpressions:
            raise DiscoveryError(
                ErrorCode.BUDGET_EXCEEDED, "cached subexpression limit"
            )
        if self._cache_bytes + values.nbytes > self.config.max_cache_bytes:
            raise DiscoveryError(ErrorCode.BUDGET_EXCEEDED, "cached array byte limit")
        values.setflags(write=False)
        diagnostics[key] = {name: count for name, count in reasons.items() if count}
        entry = _CacheEntry(values, diagnostics)
        self._cache[key] = entry
        self._cache_bytes += values.nbytes
        return entry

    def compute(self, compiled):
        if type(compiled) is not CompiledExpression:
            raise DiscoveryError(ErrorCode.INVALID_AST, "CompiledExpression required")
        verified = compile_expression(
            compiled.node, self.config, require_dimensionless=False
        )
        if (
            verified.canonical_json != compiled.canonical_json
            or verified.expression_sha256 != compiled.expression_sha256
            or verified.canonical_ast != compiled.canonical_ast
            or verified.dimension != compiled.dimension
            or verified.lookback != compiled.lookback
            or verified.complexity != compiled.complexity
        ):
            raise DiscoveryError(
                ErrorCode.INVALID_AST, "compiled metadata does not match validated AST"
            )
        estimated = self._estimate(verified.node)
        started = time.perf_counter()
        hits_before = self._cache_hits
        status = "SUCCESS"
        reasons = {}
        try:
            if estimated > self.config.max_cell_operations:
                raise DiscoveryError(
                    ErrorCode.BUDGET_EXCEEDED, "static expression operation limit"
                )
            entry = self._evaluate(verified.node, started)
            values = entry.values[self._original_positions].copy()
            for local in entry.diagnostics.values():
                for reason, count in local.items():
                    reasons[reason] = reasons.get(reason, 0) + count
            reasons["OUTPUT_MISSING"] = int(np.isnan(values).sum())
            self._check_budget(started)
        except DiscoveryError as exc:
            if exc.code != ErrorCode.BUDGET_EXCEEDED:
                raise
            status = ErrorCode.BUDGET_EXCEEDED.value
            values = np.full(len(self._original_index), np.nan)
            reasons = {"BUDGET_EXCEEDED": len(values), "OUTPUT_MISSING": len(values)}
        elapsed = time.perf_counter() - started
        self._total_seconds += elapsed
        result = pd.Series(
            values, index=self._original_index.copy(), name=verified.expression_sha256
        )
        return ExecutionResult(
            result,
            status,
            reasons,
            elapsed,
            estimated,
            int(np.isfinite(values).sum()),
            self._cache_hits - hits_before,
            self._cache_bytes,
        )


__all__ = ["Dimension", "ExpressionEngine", "compile_expression", "normalize_ast"]
