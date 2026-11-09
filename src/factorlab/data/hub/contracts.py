"""Query envelopes; payloads retain their existing domain schemas."""

from dataclasses import dataclass, field
from typing import Any, Mapping

from factorlab.errors import DataQualityError


class UnsupportedQueryError(DataQualityError):
    """The selected source cannot establish the requested query semantics."""


@dataclass(frozen=True)
class SourceMetadata:
    """One provenance envelope per result, not repeated on market rows.

    Time fields describe evidence, not financial certification. None means
    UNKNOWN. Row-specific effective/announcement times remain in the payload.
    """

    source_id: str
    data_version: str | None
    calendar_id: str | None = None
    calendar_kind: str = "UNKNOWN"
    acquired_at: str | None = None
    provider_available_at: str | None = None
    coverage: str = "UNKNOWN"
    time_fields: Mapping[str, str] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)
    evidence_status: tuple[str, ...] = (
        "PIT_UNVERIFIED", "HFQ_UNVERIFIED", "FORMAL_ADMISSION_BLOCKED",
    )
    field_semantics: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryResult:
    """Existing tables/domain facts plus separate source and time evidence."""

    data: Any
    metadata: SourceMetadata
