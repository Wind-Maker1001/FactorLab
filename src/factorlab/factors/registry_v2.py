"""Opt-in, immutable factor identities over existing trusted computation engines.

Registration validates software definitions only. It cannot attest data provenance,
point-in-time availability, adjustment quality, or financial admission.
"""

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Iterable

import numpy as np
import pandas as pd

from factorlab.errors import ConfigurationError, SchemaError
from factorlab.exploratory.factor_discovery.contracts import (
    FIELDS, CompiledExpression, DiscoveryConfig, Node,
)
from factorlab.exploratory.factor_discovery.engine import (
    ExpressionEngine, compile_expression,
)

from .liquidity import AmihudFactor, TurnoverFactor, VolumeRatioFactor
from .momentum import MomentumFactor
from .reversal import ReversalFactor
from .trend import TrendFactor
from .volatility import VolatilityFactor


_MARKET_FIELDS = tuple(name for name in FIELDS if name != "RET1")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,95}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
# A closed catalog of fresh constructors, independent of the mutable legacy dict.
_BUILTINS = {
    "REV1": lambda: ReversalFactor(1), "REV5": lambda: ReversalFactor(5),
    "MOM20": lambda: MomentumFactor(20), "MOM60": lambda: MomentumFactor(60),
    "VOL5": lambda: VolatilityFactor(5), "VOL20": lambda: VolatilityFactor(20),
    "VOL60": lambda: VolatilityFactor(60), "TURN5": lambda: TurnoverFactor(5),
    "TURN20": lambda: TurnoverFactor(20), "VOLUME_RATIO": VolumeRatioFactor,
    "AMIHU20": AmihudFactor, "PRICE_MA20": lambda: TrendFactor("PRICE_MA20"),
    "MA5_MA20": lambda: TrendFactor("MA5_MA20"),
    "MA20_MA60": lambda: TrendFactor("MA20_MA60"),
}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FactorRef:
    factor_id: str
    version: str
    definition_hash: str

    def __post_init__(self):
        for value, pattern in ((self.factor_id, _NAME), (self.version, _VERSION),
                               (self.definition_hash, _SHA256)):
            if type(value) is not str or not pattern.fullmatch(value):
                raise ConfigurationError("Invalid factor reference")


def _verified(compiled):
    if type(compiled) is not CompiledExpression:
        raise ConfigurationError("A validated CompiledExpression is required")
    verified = compile_expression(compiled.node, require_dimensionless=False)
    if verified != compiled:
        raise ConfigurationError("CompiledExpression metadata differs from validated AST")
    return verified


def _decode_expression(value):
    # Stored JSON is data, never Python source. Bound before parsing/recursing.
    if type(value) is not str or len(value) > 8192:
        raise ConfigurationError("Invalid expression JSON")

    def node(data, depth=1):
        if type(data) is not dict or depth > 5:
            raise ConfigurationError("Invalid expression shape/depth")
        op = data.get("op")
        if op == "Field" and set(data) == {"op", "value"}:
            return Node(op, value=data["value"])
        if op == "Const" and set(data) == {"op", "value"}:
            return Node(op, value=data["value"])
        if (set(data) not in ({"op", "args"}, {"op", "args", "param"})
                or type(data.get("args")) is not list or len(data["args"]) > 2):
            raise ConfigurationError("Invalid expression fields")
        return Node(op, tuple(node(child, depth + 1) for child in data["args"]),
                    param=data.get("param"))

    try:
        return compile_expression(node(json.loads(value)), require_dimensionless=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ConfigurationError("Invalid validated expression payload") from exc


def _expression_fields(compiled):
    fields = set()
    pending = [compiled.node]
    while pending:
        current = pending.pop()
        if current.op == "Field":
            fields.add("adj_close" if current.value == "RET1" else current.value)
        pending.extend(current.args)
    return tuple(sorted(fields))


@dataclass(frozen=True)
class FactorDefinition:
    """Definition hash excludes catalog labels and all data/admission claims.

    lookback is the number of *prior sessions* needed, excluding the current
    session. Missing observations are never filled. All signals are close-known.
    """

    factor_id: str
    version: str
    kind: str
    implementation: str
    input_fields: tuple[str, ...]
    lookback: int
    price_basis: tuple[str, ...]
    dependencies: tuple[FactorRef, ...] = ()
    expression_json: str = ""
    frequency: str = "1d"
    availability: str = "SESSION_CLOSE"
    window_policy: str = "EXPLICIT_CALENDAR_NO_FILL"
    definition_hash: str = field(init=False)

    def __post_init__(self):
        if (type(self.factor_id) is not str or not _NAME.fullmatch(self.factor_id)
                or self.factor_id in ("date", "symbol")
                or self.factor_id in FIELDS):
            raise ConfigurationError("Invalid/reserved factor ID")
        if type(self.version) is not str or not _VERSION.fullmatch(self.version):
            raise ConfigurationError("Invalid explicit factor version")
        if (type(self.input_fields) is not tuple
                or any(type(name) is not str or name not in _MARKET_FIELDS
                       for name in self.input_fields)
                or tuple(sorted(set(self.input_fields))) != self.input_fields):
            raise ConfigurationError("Input fields must be sorted, unique market fields")
        if type(self.lookback) is not int or not 0 <= self.lookback <= 130:
            raise ConfigurationError("Invalid prior-session lookback")
        if (type(self.price_basis) is not tuple
                or self.price_basis not in ((), ("RAW",), ("HFQ",), ("HFQ", "RAW"))):
            raise ConfigurationError("Invalid explicit price basis")
        if (type(self.dependencies) is not tuple
                or any(type(ref) is not FactorRef for ref in self.dependencies)
                or len(set(self.dependencies)) != len(self.dependencies)):
            raise ConfigurationError("Dependencies must be unique immutable FactorRefs")
        if (self.kind not in ("builtin", "expression", "alias")
                or type(self.implementation) is not str
                or type(self.expression_json) is not str
                or self.frequency != "1d" or self.availability != "SESSION_CLOSE"
                or self.window_policy != "EXPLICIT_CALENDAR_NO_FILL"):
            raise ConfigurationError("Unsupported factor execution contract")
        payload = {key: value for key, value in vars(self).items()
                   if key not in ("factor_id", "version", "definition_hash", "dependencies")}
        payload["dependencies"] = [asdict(ref) for ref in self.dependencies]
        object.__setattr__(self, "definition_hash", _hash(payload))

    @property
    def consumption_requirements(self):
        """Field-scoped research semantics; units must be declared by the consumer."""
        return {name: ("RAW_FACTOR" if name.startswith("raw_") else
                       "ADJUSTED_FACTOR" if name.startswith("adj_") else "UNIT_FACTOR")
                for name in self.input_fields
                if name.startswith(("raw_", "adj_")) or name in ("amount", "volume", "turnover")}

    @property
    def ref(self):
        return FactorRef(self.factor_id, self.version, self.definition_hash)

    @classmethod
    def builtin(cls, name, *, version="1", factor_id=None):
        if type(name) is not str or name not in _BUILTINS:
            raise ConfigurationError("Unknown exact built-in factor name")
        factor = _BUILTINS[name]()
        # Legacy lookback attributes mix total observations and lag counts.
        prior = (factor.days if isinstance(factor, (ReversalFactor, MomentumFactor, VolatilityFactor))
                 else 20 if name == "AMIHU20" else factor.lookback - 1)
        fields = tuple(sorted(set(factor.required_columns) - {"date", "symbol"}))
        basis = ("HFQ",) if "adj_close" in fields else ()
        return cls(factor_id or name, version, "builtin", "phase1-v1:" + name,
                   fields, prior, basis)

    @classmethod
    def from_expression(cls, factor_id, version, compiled):
        verified = _verified(compiled)
        return cls(factor_id, version, "expression", "discovery-ast-v1",
                   _expression_fields(verified), verified.lookback,
                   tuple(sorted(verified.dimension.price_bases)),
                   expression_json=verified.canonical_json)

    @classmethod
    def alias(cls, factor_id, version, target):
        if type(target) is not cls:
            raise ConfigurationError("Alias target must be a FactorDefinition")
        return cls(factor_id, version, "alias", "dependency-alias-v1",
                   target.input_fields, target.lookback, target.price_basis,
                   dependencies=(target.ref,))


def _validate_definition(definition):
    if type(definition) is not FactorDefinition:
        raise ConfigurationError("FactorDefinition required; callable registration is forbidden")
    # Reconstruct even frozen objects; metadata cannot override executable semantics.
    if definition.kind == "builtin":
        expected = FactorDefinition.builtin(
            definition.implementation.removeprefix("phase1-v1:"),
            version=definition.version, factor_id=definition.factor_id)
    elif definition.kind == "expression":
        expected = FactorDefinition.from_expression(
            definition.factor_id, definition.version,
            _decode_expression(definition.expression_json))
    else:
        expected = FactorDefinition(**{key: value for key, value in vars(definition).items()
                                       if key != "definition_hash"})
        if definition != expected:
            raise ConfigurationError("Factor identity conflicts with definition")
        if (definition.kind != "alias" or definition.implementation != "dependency-alias-v1"
                or len(definition.dependencies) != 1 or definition.expression_json):
            raise ConfigurationError("Only a single pinned dependency alias is supported")
        return
    if definition != expected:
        raise ConfigurationError("Factor metadata/identity conflicts with implementation")


@dataclass(frozen=True)
class FactorDataContext:
    """Caller-declared run provenance, kept outside mathematical identity."""

    data_source: str = "UNSPECIFIED"
    availability_evidence: str = "UNVERIFIED"
    no_live_money: bool = field(default=True, init=False)
    pit_status: str = field(default="PIT_UNVERIFIED", init=False)
    hfq_status: str = field(default="HFQ_UNVERIFIED", init=False)
    admission_status: str = field(default="FORMAL_ADMISSION_BLOCKED", init=False)

    def __post_init__(self):
        if any(type(value) is not str or not value.strip()
               for value in (self.data_source, self.availability_evidence)):
            raise ConfigurationError("Nonempty provenance descriptions required")


def _resolve(catalog, refs):
    ordered, visited, active = [], set(), set()

    def visit(ref):
        key = (ref.factor_id, ref.version)
        if key in active:
            raise ConfigurationError("Factor dependency cycle")
        if key not in catalog:
            raise ConfigurationError("Missing dependency: %s@%s" % key)
        definition = catalog[key]
        if definition.ref != ref:
            raise ConfigurationError("Dependency definition hash conflict")
        if key in visited:
            return
        active.add(key)
        for dependency in definition.dependencies:
            visit(dependency)
        active.remove(key)
        visited.add(key)
        ordered.append(definition)

    for ref in refs:
        visit(ref)
    return tuple(ordered)


@dataclass(frozen=True)
class FactorRegistrySnapshot:
    definitions: tuple[FactorDefinition, ...]
    snapshot_hash: str = field(init=False)

    def __post_init__(self):
        if (type(self.definitions) is not tuple
                or any(type(d) is not FactorDefinition for d in self.definitions)):
            raise ConfigurationError("Immutable definitions required")
        catalog = {(d.factor_id, d.version): d for d in self.definitions}
        if len(catalog) != len(self.definitions):
            raise ConfigurationError("Duplicate snapshot factor ID/version")
        for definition in self.definitions:
            _validate_definition(definition)
        # Check graph shape before pins, so cycles have a useful diagnostic.
        active, done = set(), set()

        def check(key):
            if key in active:
                raise ConfigurationError("Factor dependency cycle")
            if key in done:
                return
            if key not in catalog:
                raise ConfigurationError("Missing dependency: %s@%s" % key)
            active.add(key)
            for ref in catalog[key].dependencies:
                check((ref.factor_id, ref.version))
            active.remove(key)
            done.add(key)

        for key in catalog:
            check(key)
        for definition in self.definitions:
            _resolve(catalog, (definition.ref,))
            if definition.kind == "alias":
                target = catalog[(definition.dependencies[0].factor_id,
                                  definition.dependencies[0].version)]
                if definition != FactorDefinition.alias(
                        definition.factor_id, definition.version, target):
                    raise ConfigurationError("Alias metadata conflicts with dependency")
        canonical = tuple(sorted(self.definitions, key=lambda d: (d.factor_id, d.version)))
        object.__setattr__(self, "definitions", canonical)
        object.__setattr__(self, "snapshot_hash", _hash([asdict(d) for d in canonical]))

    def get(self, factor_id, version=None):
        matches = [d for d in self.definitions
                   if d.factor_id == factor_id and (version is None or d.version == version)]
        if len(matches) != 1:
            raise ConfigurationError("Unknown or ambiguous factor: %s@%s" % (factor_id, version))
        return matches[0]

    def resolve(self, refs: Iterable[FactorRef]):
        refs = tuple(refs)
        if any(type(ref) is not FactorRef for ref in refs):
            raise ConfigurationError("Explicit versioned FactorRefs required")
        if len({ref.factor_id for ref in refs}) != len(refs):
            raise ConfigurationError("Duplicate output factor ID")
        return _resolve({(d.factor_id, d.version): d for d in self.definitions}, refs)

    def compute(self, bars, refs, *, calendar, context=FactorDataContext(),
                expression_config=DiscoveryConfig()):
        """Return original keys, sorted date/symbol, then requested factor columns.

        Prepare inputs once; built-ins share the ordered panel. Expressions
        share one bounded ExpressionEngine/cache, which validates its own panel.
        """
        refs = tuple(refs)
        definitions = self.resolve(refs)
        if not refs:
            raise ConfigurationError("Explicit nonempty factor list required")
        if type(context) is not FactorDataContext:
            raise ConfigurationError("FactorDataContext required")
        expected_context = FactorDataContext(context.data_source, context.availability_evidence)
        if context != expected_context:
            raise ConfigurationError("Run context cannot override financial status")
        panel, positions, keys = _prepare_panel(bars, definitions, calendar)
        values, engine = {}, None
        for definition in definitions:
            if definition.kind == "builtin":
                name = definition.implementation.removeprefix("phase1-v1:")
                series = _BUILTINS[name]()._compute_ordered(panel)
            elif definition.kind == "alias":
                series = values[definition.dependencies[0]]
            else:
                if engine is None:
                    engine = ExpressionEngine(panel, calendar, config=expression_config)
                result = engine.compute(_decode_expression(definition.expression_json))
                if result.status != "SUCCESS":
                    raise ConfigurationError("Expression execution failed: " + result.status)
                series = result.values
            values[definition.ref] = series
        output = pd.DataFrame({
            "date": keys["date"].to_numpy(), "symbol": keys["symbol"].to_numpy(),
            **{ref.factor_id: values[ref].to_numpy(dtype=float, na_value=np.nan)[positions]
               for ref in refs},
        })
        output.attrs["factor_registry_v2"] = {
            "snapshot_hash": self.snapshot_hash,
            "factors": [asdict(ref) for ref in refs],
            "data_context": asdict(context),
            "registration_status": "SOFTWARE_VALIDATED_ONLY",
        }
        return output

    def compute_chunked(self, bars, refs, *, calendar, output_dir,
                        context=FactorDataContext(), config=None):
        """Persist raw factors using independent full-history security groups."""
        from factorlab.dataset.partitioned import build_partitioned
        return build_partitioned(self, tuple(refs), bars, calendar, output_dir,
                                 context=context, config=config)


def _prepare_panel(bars, definitions, calendar):
    if (not isinstance(bars, pd.DataFrame) or bars.empty
            or not bars.columns.is_unique):
        raise SchemaError("Nonempty bars with unique columns required")
    if len(bars) > 250000:
        raise SchemaError("V2 input limit is 250000 rows")
    required = {name for definition in definitions for name in definition.input_fields}
    # Existing interpreter deliberately validates the complete market whitelist.
    if any(d.kind == "expression" for d in definitions):
        required.update(_MARKET_FIELDS)
    columns = ["date", "symbol"] + sorted(required)
    if not set(columns).issubset(bars.columns):
        raise SchemaError("Missing factor inputs: %s" % sorted(set(columns) - set(bars.columns)))
    frame = bars.loc[:, columns].copy()
    if (pd.api.types.is_numeric_dtype(frame["date"].dtype)
            or pd.api.types.is_numeric_dtype(pd.Index(calendar).dtype)):
        raise SchemaError("Numeric dates are not daily session keys")
    try:
        dates = pd.DatetimeIndex(pd.to_datetime(calendar, errors="raise"))
        bar_dates = pd.to_datetime(frame["date"], errors="raise")
    except (TypeError, ValueError) as exc:
        raise SchemaError("Invalid daily dates/calendar") from exc
    if (dates.empty or dates.hasnans or dates.has_duplicates
            or not dates.is_monotonic_increasing or dates.tz is not None
            or not dates.equals(dates.normalize())
            or bar_dates.dt.tz is not None or bar_dates.isna().any()
            or not bar_dates.equals(bar_dates.dt.normalize())):
        raise SchemaError("Unique sorted calendar and timezone-naive daily dates required")
    frame["date"] = bar_dates
    if (frame["symbol"].map(lambda v: type(v) is not str or not v.strip()).any()
            or frame.duplicated(["date", "symbol"]).any()
            or not frame["date"].isin(dates).all()):
        raise SchemaError("Invalid/duplicate security-date keys or date outside calendar")
    for name in required:
        source = frame[name]
        if (not pd.api.types.is_numeric_dtype(source.dtype)
                or pd.api.types.is_bool_dtype(source.dtype)
                or pd.api.types.is_complex_dtype(source.dtype)):
            raise SchemaError("Real numeric market field required: " + name)
        if np.isinf(source.to_numpy(dtype=float, na_value=np.nan)).any():
            raise SchemaError("Infinite market field: " + name)
    keys = frame[["date", "symbol"]].sort_values(["date", "symbol"], kind="mergesort")
    symbols = sorted(frame["symbol"].unique())
    if len(dates) * len(symbols) > 250000:
        raise SchemaError("V2 dense panel limit is 250000 rows")
    index = pd.MultiIndex.from_product([symbols, dates], names=["symbol", "date"])
    positions = index.get_indexer(pd.MultiIndex.from_frame(keys[["symbol", "date"]]))
    panel = frame.set_index(["symbol", "date"]).reindex(index).reset_index()
    return panel, positions, keys


class FactorRegistryV2:
    """Mutable builder; freeze() creates an independent immutable snapshot."""

    def __init__(self):
        self._definitions = {}

    def register(self, definition):
        self.register_many((definition,))
        return definition.ref

    def register_expression(self, factor_id, version, compiled):
        return self.register(FactorDefinition.from_expression(factor_id, version, compiled))

    def register_many(self, definitions):
        candidate = dict(self._definitions)
        for definition in definitions:
            _validate_definition(definition)
            key = (definition.factor_id, definition.version)
            if key in candidate:
                message = ("Duplicate factor ID/version" if candidate[key] == definition
                           else "Factor version conflict")
                raise ConfigurationError(message)
            candidate[key] = definition
        # Atomic graph validation: failed additions never partially register.
        FactorRegistrySnapshot(tuple(candidate.values()))
        self._definitions = candidate

    def get(self, factor_id, version=None):
        matches = [d for (name, rev), d in self._definitions.items()
                   if name == factor_id and (version is None or rev == version)]
        if len(matches) != 1:
            raise ConfigurationError("Unknown or ambiguous factor: %s@%s" % (factor_id, version))
        return matches[0]

    def resolve(self, refs):
        return self.freeze().resolve(refs)

    def freeze(self):
        return FactorRegistrySnapshot(tuple(self._definitions.values()))

    def compute(self, bars, refs, **kwargs):
        return self.freeze().compute(bars, refs, **kwargs)

    def compute_chunked(self, bars, refs, **kwargs):
        return self.freeze().compute_chunked(bars, refs, **kwargs)

    @classmethod
    def with_builtins(cls):
        registry = cls()
        registry.register_many(FactorDefinition.builtin(name) for name in _BUILTINS)
        return registry


__all__ = ["FactorDefinition", "FactorRef", "FactorDataContext", "FactorRegistryV2",
           "FactorRegistrySnapshot"]
