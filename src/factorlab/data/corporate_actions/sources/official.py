"""Normalize only explicitly reviewed local facts; announcement bytes alone
do not establish event terms, coverage or contemporaneous availability.
"""

import json
from pathlib import Path
from typing import Sequence

from factorlab.data.evidence import date, fail, implementation_sha256, snapshot_file, utc_now
from ..contracts import EventCoverage, SourceSnapshot
from ..store import load_snapshot
from . import CorporateActionSource


class LocalReviewedActionSource(CorporateActionSource):
    """Consume a self-contained event generation with exact, versioned facts."""

    def __init__(self, generation: Path) -> None:
        self.generation = Path(generation)

    def snapshot(self, security_ids: Sequence[str], start: str, end: str) -> SourceSnapshot:
        snapshot = load_snapshot(self.generation)
        if date(start) > date(end):
            fail("invalid requested event range")
        supplied = {c.security_id for c in snapshot.coverage}
        if not set(security_ids).issubset(supplied):
            fail("requested identities absent from event coverage")
        return snapshot


class OfficialNoticeEvidenceSource(CorporateActionSource):
    """Archive existing official files; deliberately does not invent PDF facts."""

    def __init__(self, files: Sequence[Path]) -> None:
        self.files = tuple(Path(p) for p in files)

    def snapshot(self, security_ids: Sequence[str], start: str, end: str) -> SourceSnapshot:
        if date(start) > date(end) or not self.files or not security_ids:
            fail("official evidence files, identities and range required")
        acquired = utc_now()
        artifacts = tuple(snapshot_file(p, "OFFICIAL_NOTICE_BYTES", acquired) for p in self.files)
        version = "unparsed-local-evidence-v1;implementation=" + implementation_sha256([__file__])
        return SourceSnapshot("OFFICIAL_NOTICE_BYTES", version, acquired, (),
                              tuple(EventCoverage(s, start, end, acquired, artifacts[0].sha256)
                                    for s in sorted(set(security_ids))), artifacts,
                              ("OFFICIAL_TERMS_AND_COMPLETE_EVENT_HISTORY_REQUIRE_REVIEW",))
