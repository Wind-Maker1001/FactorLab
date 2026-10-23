"""Runtime-checked evidence contracts, compatible with Python 3.9."""
from dataclasses import dataclass, fields
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import re

class EvidenceError(ValueError):
    """Malformed, altered or unbound evidence; never silently admitted."""

class CertificationRejected(EvidenceError):
    """A requested candidate claim exceeds its evidence."""

def security(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'\d{6}\.(SH|SZ)', value):
        raise EvidenceError('INVALID_CANONICAL_SECURITY')
    return value

def day(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise EvidenceError('INVALID_DATE')
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise EvidenceError('INVALID_DATE') from exc
    return value

def digest(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise EvidenceError('INVALID_SHA256')
    return value

def aware(value: Any) -> str:
    if not isinstance(value, str):
        raise EvidenceError('INVALID_RETRIEVED_AT')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise EvidenceError('INVALID_RETRIEVED_AT') from exc
    if parsed.tzinfo is None:
        raise EvidenceError('TIMEZONE_REQUIRED')
    return value

def strict(cls: Any, obj: Any) -> Any:
    if not isinstance(obj, dict) or set(obj) != {f.name for f in fields(cls)}:
        raise EvidenceError('INVALID_FIELDS:' + cls.__name__)
    data = dict(obj)
    for key in ['source_ids', 'declared_dates']:
        if key in data:
            if not isinstance(data[key], list):
                raise EvidenceError('LIST_REQUIRED:' + key)
            data[key] = tuple(data[key])
    return cls(**data)

@dataclass(frozen=True)
class Artifact:
    source_id: str
    path: str
    sha256: str
    role: str
    media_type: str
    url: Optional[str]
    source_version: str
    locator: str
    retrieved_at: str

    def __post_init__(self) -> None:
        for v in [self.source_id, self.path, self.source_version, self.locator]:
            if not isinstance(v, str) or not v:
                raise EvidenceError('MISSING_SOURCE_BINDING')
        digest(self.sha256)
        aware(self.retrieved_at)
        if self.role not in {'NATIVE_CANDIDATE', 'OFFICIAL_DOCUMENT', 'RELATIONSHIP_REVIEW', 'LINEAGE_ZIP', 'DICTIONARY_TEXT', 'CALENDAR'}:
            raise EvidenceError('UNKNOWN_SOURCE_ROLE')
        if self.media_type not in {'PDF', 'HTML', 'JSON', 'CSV', 'ZIP', 'TEXT'}:
            raise EvidenceError('UNKNOWN_MEDIA_TYPE')
        if self.url is not None and (not isinstance(self.url, str) or not self.url.startswith(('https://', 'http://'))):
            raise EvidenceError('INVALID_PUBLIC_URL')

@dataclass(frozen=True)
class NativeObservation:
    security: str
    trading_date: str
    native_trdsta: Optional[int]
    native_trdsta_raw: Optional[str]
    native_label: Optional[str]
    native_state_code: Optional[int]
    native_filling: Optional[int]
    native_volume: Optional[str]
    native_source_sha256: str
    native_source_coordinate: str
    native_state_source_sha256: str
    native_state_source_coordinate: str
    native_missing: bool
    native_missing_reason: str
    record_source_id: str
    record_coordinate: str
    dictionary_binding: str
    membership_candidate: Optional[bool]
    membership_source_coordinate: str
    source_ids: Tuple[str, ...]

    def __post_init__(self) -> None:
        security(self.security)
        day(self.trading_date)
        digest(self.native_source_sha256)
        digest(self.native_state_source_sha256)
        if type(self.native_missing) is not bool or (self.membership_candidate is not None and type(self.membership_candidate) is not bool):
            raise EvidenceError('BOOLEAN_REQUIRED')
        for key in ['native_trdsta', 'native_state_code', 'native_filling']:
            v = getattr(self, key)
            if v is not None and type(v) is not int:
                raise EvidenceError('INTEGER_ENUM_REQUIRED:' + key)
        if self.native_state_code not in {None, 0, 1, 2} or self.native_filling not in {None, 0, 1, 2}:
            raise EvidenceError('UNKNOWN_NATIVE_STATE_ENUM')
        if self.dictionary_binding != 'UNVERIFIED':
            raise EvidenceError('NO_CERTIFIED_NATIVE_DICTIONARY_VERSION_CONTRACT')
        if self.native_trdsta is not None and not 1 <= self.native_trdsta <= 16:
            raise EvidenceError('UNKNOWN_TRDSTA_ENUM')
        for name in ['native_source_coordinate','native_state_source_coordinate','native_missing_reason','record_source_id','record_coordinate','membership_source_coordinate']:
            if not isinstance(getattr(self,name),str) or not getattr(self,name):
                raise EvidenceError('TEXT_SOURCE_COORDINATE_REQUIRED:' + name)
        if any(not isinstance(x,str) or not x for x in self.source_ids):
            raise EvidenceError('INVALID_SOURCE_ID')
        if self.native_trdsta_raw is not None:
            if not isinstance(self.native_trdsta_raw,str):
                raise EvidenceError('RAW_ENUM_TEXT_REQUIRED')
            try:
                if self.native_trdsta is None or Decimal(self.native_trdsta_raw) != self.native_trdsta:
                    raise EvidenceError('RAW_ENUM_MISMATCH')
            except InvalidOperation as exc:
                raise EvidenceError('INVALID_RAW_ENUM') from exc
        if self.native_label is not None and (self.native_label != 'U' or self.native_trdsta != 10):
            raise EvidenceError('UNBOUND_NATIVE_LABEL')
        if self.native_volume is not None:
            if not isinstance(self.native_volume, str):
                raise EvidenceError('VOLUME_DECIMAL_STRING_REQUIRED')
            try:
                volume = Decimal(self.native_volume)
            except InvalidOperation as exc:
                raise EvidenceError('INVALID_VOLUME') from exc
            if not volume.is_finite() or volume < 0:
                raise EvidenceError('INVALID_VOLUME')
        if self.native_missing and any(getattr(self, k) is not None for k in ['native_trdsta', 'native_trdsta_raw', 'native_label', 'native_state_code', 'native_filling', 'native_volume']):
            raise EvidenceError('FABRICATED_NATIVE_MISSING_ROW')
        if not self.source_ids or self.record_source_id not in self.source_ids or not self.native_source_coordinate or not self.record_coordinate:
            raise EvidenceError('NATIVE_PROVENANCE_MISSING')

ALLOWED = {'LISTING': {'LISTED', 'NOT_LISTED', 'DELISTED'}, 'ST_RISK': {'NORMAL', 'ST', '*ST', 'OTHER'}, 'SUSPENSION': {'SUSPENDED', 'NOT_SUSPENDED'}, 'HAS_TRADES': {'TRUE', 'FALSE'}}

@dataclass(frozen=True)
class OfficialEvidence:
    evidence_id: str
    security: str
    field: str
    value: str
    effective_from: str
    effective_to: str
    declared_dates: Tuple[str, ...]
    implemented: bool
    announcement_id: str
    source_ids: Tuple[str, ...]
    original_document_location: str
    event_date: str
    announced_at: Optional[str]
    source_published_at: Optional[str]
    first_available_at: Optional[str]
    retrieved_at: str
    historical_known_at: Optional[str]
    evidence_scope: str

    def __post_init__(self) -> None:
        security(self.security)
        for v in [self.effective_from, self.effective_to, self.event_date]:
            day(v)
        if self.effective_from > self.effective_to or type(self.implemented) is not bool:
            raise EvidenceError('INVALID_EFFECTIVE_INTERVAL_OR_IMPLEMENTATION_FLAG')
        if self.field not in ALLOWED or self.value not in ALLOWED[self.field]:
            raise EvidenceError('UNSUPPORTED_OFFICIAL_FIELD_VALUE')
        if not self.declared_dates or len(set(self.declared_dates)) != len(self.declared_dates):
            raise EvidenceError('EMPTY_OR_DUPLICATE_DECLARED_DATES')
        for d in self.declared_dates:
            if not self.effective_from <= day(d) <= self.effective_to:
                raise EvidenceError('DECLARED_DATE_OUTSIDE_ORIGINAL_INTERVAL')
        if self.first_available_at is not None or self.historical_known_at is not None:
            raise EvidenceError('PIT_VINTAGE_NOT_SUPPORTED_BY_THIS_CANDIDATE')
        aware(self.retrieved_at)
        for name in ['announced_at','source_published_at']:
            if getattr(self,name) is not None and not isinstance(getattr(self,name),str):
                raise EvidenceError('OPTIONAL_TIME_TEXT_REQUIRED')
        if any(not isinstance(x,str) or not x for x in self.source_ids):
            raise EvidenceError('INVALID_SOURCE_ID')
        if not self.source_ids or not self.original_document_location or not self.evidence_scope or not self.evidence_id:
            raise EvidenceError('OFFICIAL_PROVENANCE_MISSING')

@dataclass(frozen=True)
class Relationship:
    old_security: str
    new_security: str
    relation_type: str
    issuer_continues: bool
    source_ids: Tuple[str, ...]
    source_coordinate: str

    def __post_init__(self) -> None:
        security(self.old_security)
        security(self.new_security)
        if self.old_security == self.new_security or type(self.issuer_continues) is not bool:
            raise EvidenceError('INVALID_RELATIONSHIP')
        if self.relation_type not in {'CODE_CHANGE', 'SHARE_EXCHANGE_MERGER'}:
            raise EvidenceError('UNSUPPORTED_RELATIONSHIP_TYPE')
        if not self.source_ids or not self.source_coordinate:
            raise EvidenceError('RELATIONSHIP_SOURCE_MISSING')
        if self.relation_type == 'CODE_CHANGE' and not self.issuer_continues:
            raise EvidenceError('CODE_CHANGE_ISSUER_DISCONTINUITY')
        if self.relation_type == 'SHARE_EXCHANGE_MERGER' and self.issuer_continues:
            raise EvidenceError('MERGER_IS_NOT_SIMPLE_CODE_CHANGE')
