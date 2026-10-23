"""Archive real gbbq candidates; an unverified binary layout is never decoded."""

from pathlib import Path
from typing import Sequence

from qbot.research.data.evidence import date, fail, implementation_sha256, snapshot_file, utc_now
from ..contracts import EventCoverage, SourceSnapshot
from . import CorporateActionSource


class LocalTdxActionSource(CorporateActionSource):
    def __init__(self, hq_cache: Path) -> None:
        self.root = Path(hq_cache)

    def snapshot(self, security_ids: Sequence[str], start: str, end: str) -> SourceSnapshot:
        if date(start) > date(end) or not security_ids:
            fail("explicit securities and ordered source range required")
        acquired = utc_now()
        artifacts = tuple(snapshot_file(self.root / name, "TDX_LOCAL_GBBQ_CANDIDATE", acquired)
                          for name in ("gbbq", "gbbq.map"))
        coverage = tuple(EventCoverage(s, start, end, acquired, artifacts[0].sha256)
                         for s in sorted(set(security_ids)))
        version = "candidate-bytes-v1;implementation=" + implementation_sha256([__file__])
        return SourceSnapshot("TDX_LOCAL_GBBQ_CANDIDATE", version, acquired,
                              (), coverage, artifacts, ("TDX_GBBQ_LAYOUT_AND_HISTORY_UNVERIFIED",))
