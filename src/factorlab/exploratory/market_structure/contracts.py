"""Independent synthetic research contracts. NO LIVE MONEY; Python 3.9."""

from dataclasses import dataclass, field
import hashlib
import json
import math
from types import MappingProxyType
from typing import Mapping, Optional, Protocol, Tuple

import pandas as pd

SCHEMA = "MARKET_STRUCTURE_EXPLORATORY_V1"
BASELINE = "39eecf869c9c2075ba3f6e3c76aeba3338c63432"
FLOW_DEFINITION = "SYNTHETIC_AGGRESSOR_SIDE_V1"
MARKERS = dict(
    purpose="EXPLORATORY",
    qualification="UNVERIFIED",
    synthetic_only=True,
    formal_admission=False,
    forward_allowed=False,
    live_allowed=False,
)


class ContractError(ValueError):
    """An explicit input/knowledge contract failed."""


def instant(value):
    try:
        result = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractError("valid timezone-aware instant required") from exc
    if pd.isna(result) or result.tzinfo is None:
        raise ContractError("timezone-aware instant required")
    return result.tz_convert("UTC")


def primitive(value):
    if isinstance(value, Mapping):
        return {str(k): primitive(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [primitive(v) for v in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def json_bytes(value):
    return (
        json.dumps(
            primitive(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(json_bytes(value)).hexdigest()


def freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class Record:
    fact_id: str
    revision: int
    kind: str
    payload: Mapping
    valid_from: object
    published_at: object
    available_at: object
    captured_at: object
    evidence: Tuple[str, ...]
    valid_to: Optional[object] = None
    observation_end: Optional[object] = None
    source: str = "SYNTHETIC_TEST_ONLY"
    definition_version: str = "v1"
    historical_availability_proven: bool = True
    withdrawn: bool = False
    known_at: object = field(init=False)
    sha256: str = field(init=False)

    def __post_init__(self):
        if not isinstance(self.kind, str) or self.kind not in {
            "scope",
            "source_definition",
            "membership",
            "edge",
            "minute",
            "benchmark",
        }:
            raise ContractError("unknown record kind")
        if (
            not isinstance(self.fact_id, str)
            or not self.fact_id.strip()
            or type(self.revision) is not int
            or self.revision < 1
            or not isinstance(self.evidence, (tuple, list))
            or not self.evidence
            or not isinstance(self.source, str)
            or self.source != "SYNTHETIC_TEST_ONLY"
        ):
            raise ContractError("synthetic identity, revision and evidence required")
        required = {
            "scope": (
                "family",
                "namespace",
                "complete",
                "group_ids",
                "taxonomy_version",
            ),
            "source_definition": ("definition_id", "vendor", "unit", "currency"),
            "membership": (
                "security_id",
                "family",
                "namespace",
                "taxonomy_version",
                "change_date",
                "group_id",
                "weight",
            ),
            "edge": (
                "upstream",
                "downstream",
                "relation_type",
                "evidence_status",
                "confidence",
            ),
            "minute": (
                "security_id",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "unit",
                "price_basis",
                "buy",
                "sell",
                "unclassified",
                "flow_definition",
                "flow_unit",
                "limit_definition",
                "limit_state",
            ),
            "benchmark": ("session_id", "adj_close", "complete"),
        }
        if not isinstance(self.payload, Mapping) or not set(
            required[self.kind]
        ).issubset(self.payload):
            raise ContractError("record payload schema missing fields")
        if (
            not isinstance(self.definition_version, str)
            or not self.definition_version.strip()
            or any(not isinstance(e, str) or not e.strip() for e in self.evidence)
        ):
            raise ContractError(
                "explicit definition version and evidence references required"
            )
        if self.kind in ("minute", "benchmark") and self.observation_end is None:
            raise ContractError("observation_end required")
        for name in ("historical_availability_proven", "withdrawn"):
            if type(getattr(self, name)) is not bool:
                raise ContractError("explicit boolean required: " + name)
        for name in ("valid_from", "published_at", "available_at", "captured_at"):
            object.__setattr__(self, name, instant(getattr(self, name)))
        for name in ("valid_to", "observation_end"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, instant(getattr(self, name)))
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ContractError("empty/reversed validity interval")
        clocks = [self.published_at, self.available_at]
        if self.observation_end is not None:
            clocks.append(self.observation_end)
        if not self.historical_availability_proven:
            clocks.append(self.captured_at)
        object.__setattr__(self, "known_at", max(clocks))
        object.__setattr__(self, "payload", freeze(self.payload))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        try:
            checksum = digest(self.as_dict())
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "record requires JSON-compatible finite evidence"
            ) from exc
        object.__setattr__(self, "sha256", checksum)

    def as_dict(self):
        return {k: primitive(v) for k, v in self.__dict__.items() if k != "sha256"}


@dataclass(frozen=True)
class ComputeRequest:
    observation_start: object
    observation_end: object
    as_of: object
    decision_time: object
    frequency: int
    universe: Tuple[str, ...]
    session_id: str
    scenario: str = "historical_simulation"
    synthetic_only: bool = True

    def __post_init__(self):
        for name in ("observation_start", "observation_end", "as_of", "decision_time"):
            object.__setattr__(self, name, instant(getattr(self, name)))
        if self.synthetic_only is not True:
            raise ContractError("REAL_RESEARCH_BLOCKED")
        if self.as_of > self.decision_time:
            raise ContractError("as_of exceeds signal decision_time")
        if type(self.frequency) is not int or self.frequency not in (5, 15, 30):
            raise ContractError("frequency must be 5/15/30 minutes")
        if self.observation_end - self.observation_start != pd.Timedelta(
            minutes=self.frequency
        ):
            raise ContractError("window duration/frequency mismatch")
        if (
            not isinstance(self.universe, (tuple, list))
            or not self.universe
            or any(not isinstance(s, str) or not s.strip() for s in self.universe)
        ):
            raise ContractError(
                "explicit universe requires nonempty string security IDs"
            )
        if (
            not self.universe
            or len(set(self.universe)) != len(self.universe)
            or not isinstance(self.session_id, str)
            or not self.session_id.strip()
            or not isinstance(self.scenario, str)
            or not self.scenario.strip()
        ):
            raise ContractError("explicit unique universe/session/scenario required")
        object.__setattr__(self, "universe", tuple(self.universe))

    def as_dict(self):
        return primitive(self.__dict__)


@dataclass(frozen=True)
class FeatureSpec:
    feature_id: str
    family: str
    grain: str
    formula: str
    required_inputs: Tuple[str, ...]
    implemented: bool
    unit: str = "RATIO"
    definition_version: str = "v1"
    minimum_input_coverage: float = 0.8
    missing_policy: str = "NaN with reason; no filling; zero denominator is missing"
    temporal_boundary: str = "completed observations; known_at<=as_of<=decision_time"
    lookback: str = "one observation window"

    def as_dict(self):
        result = primitive(self.__dict__)
        result["implementation_status"] = (
            "IMPLEMENTED_SYNTHETIC" if self.implemented else "CONTRACT_ONLY"
        )
        result["structural_signature"] = digest(
            {
                k: v
                for k, v in result.items()
                if k not in ("feature_id", "implementation_status")
            }
        )
        return result


class SourceProvider(Protocol):
    def records(self) -> Tuple[Record, ...]:
        """Return immutable source records; this PR implements synthetic only."""


def real_number(value, name, nonnegative=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (nonnegative and value < 0)
    ):
        raise ContractError("invalid real number: " + name)
    return float(value)
