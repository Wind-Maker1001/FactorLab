"""Versioned event facts, distinct effective and knowledge times (Python 3.9)."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple

from factorlab.data.evidence import SourceArtifact


@dataclass(frozen=True)
class CorporateAction:
    security_id: str
    event_id: str
    revision: int
    event_type: str
    effective_date: str
    announced_at: str
    available_at: str
    source_id: str
    source_sha256: str
    status: str = "PROPOSED"
    basis: str = "PER_OLD_SHARE"
    sequence: Optional[int] = None
    currency: str = "UNKNOWN"
    cash: Optional[Decimal] = None
    bonus: Optional[Decimal] = None
    capitalization: Optional[Decimal] = None
    rights: Optional[Decimal] = None
    rights_price: Optional[Decimal] = None
    split_ratio: Optional[Decimal] = None
    reference_close: Optional[Decimal] = None
    reference_price: Optional[Decimal] = None
    record_date: Optional[str] = None
    pay_date: Optional[str] = None
    credit_date: Optional[str] = None
    tradable_date: Optional[str] = None
    subscription_start: Optional[str] = None
    subscription_end: Optional[str] = None
    eligibility: Optional[str] = None
    rule_id: str = "UNVERIFIED"
    original_terms: str = ""
    synthetic_only: bool = False


@dataclass(frozen=True)
class EventCoverage:
    security_id: str
    start: str
    end: str
    available_at: str
    source_sha256: str
    # COMPLETE means an explicit coverage claim with evidence, not an empty response.
    status: str = "UNKNOWN"


@dataclass(frozen=True)
class SourceSnapshot:
    source_id: str
    source_version: str
    acquired_at: str
    events: Tuple[CorporateAction, ...]
    coverage: Tuple[EventCoverage, ...]
    artifacts: Tuple[SourceArtifact, ...] = ()
    issues: Tuple[str, ...] = ()
    synthetic_only: bool = False
    history_kind: str = "HISTORICALLY_RECONSTRUCTED"


@dataclass(frozen=True)
class EventView:
    snapshot: SourceSnapshot
    events: Tuple[CorporateAction, ...]
    effective_through: str
    knowledge_as_of: str
