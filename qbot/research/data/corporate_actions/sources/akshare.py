"""Read the pinned AKShare Sina factor source safely, without SDK eval/ffill.

Factor records are observations, not normalized corporate actions. The
sentinel is retained as a separate observation, never treated as an event.
"""

from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Sequence, Tuple
from urllib.request import Request, urlopen

from qbot.research.data.evidence import archive_bytes, date, fail, implementation_sha256, utc_now
from ..contracts import EventCoverage, SourceSnapshot
from ..validation import decimal
from . import CorporateActionSource


def parse_sina_factors(content: bytes) -> Tuple[Dict[str, str], ...]:
    try:
        text = content.decode("utf-8-sig")
        match = re.match(r"\s*var\s+[A-Za-z_$][\w$]*\s*=\s*", text)
        if not match:
            fail("unexpected Sina factor assignment wrapper")
        payload, end = json.JSONDecoder().raw_decode(text[match.end():])
        tail = text[match.end() + end:].strip()
        if tail.startswith(";"):
            tail = tail[1:].strip()
        # The actual source appends a block comment. No JavaScript is evaluated.
        if tail and not re.fullmatch(r"/\*(?:(?!\*/)[\s\S])*\*/", tail):
            fail("unexpected executable suffix in factor response")
        if not isinstance(payload, dict) or set(payload) != {"total", "data"}:
            fail("factor payload fields mismatch")
        rows = payload["data"]
        if not isinstance(rows, list) or len(rows) > 10000:
            fail("invalid factor observation count")
        if isinstance(payload["total"], bool) or str(payload["total"]) != str(len(rows)):
            fail("factor total does not match observations")
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"d", "f"}:
                fail("factor observation fields mismatch")
            date(row["d"])
            value = decimal(row["f"], "source factor", positive=True)
            if row["d"] in seen:
                fail("duplicate factor date")
            seen.add(row["d"])
            result.append({"date": row["d"], "factor": str(value),
                           "kind": "SENTINEL" if row["d"] == "1900-01-01" else "UNVERIFIED_FACTOR_OBSERVATION"})
        return tuple(result)
    except (UnicodeError, ValueError, TypeError, KeyError) as exc:
        fail("malformed Sina factor response: %s" % type(exc).__name__)


class SinaFactorSource(CorporateActionSource):
    def __init__(self, archive_root: Path, timeout_seconds: float = 15.0) -> None:
        if isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 30:
            fail("bounded source timeout required")
        self.root, self.timeout = Path(archive_root), timeout_seconds

    def snapshot(self, security_ids: Sequence[str], start: str, end: str) -> SourceSnapshot:
        if date(start) > date(end) or not 1 <= len(security_ids) <= 8 or len(set(security_ids)) != len(security_ids):
            fail("diagnostic factor source accepts 1..8 unique explicit vendor symbols")
        acquired = utc_now()
        artifacts, coverage = [], []
        for symbol in security_ids:
            if not re.fullmatch(r"(?:sh|sz)\d{6}", symbol):
                fail("explicit Sina sh/sz symbol required; identity is not inferred")
            url = "https://finance.sina.com.cn/realstock/company/%s/hfq.js" % symbol
            request = Request(url, headers={"User-Agent": "FactorLab-evidence-diagnostic/1.0"})
            with urlopen(request, timeout=self.timeout) as response:
                if response.geturl() != url:
                    fail("unexpected source redirect")
                content = response.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                fail("factor response exceeds diagnostic byte limit")
            # Archive before parsing, so a rejected response remains available for audit.
            artifact = archive_bytes(content, self.root, "SINA_HFQ_FACTOR_OBSERVATIONS", url, acquired)
            parse_sina_factors(content)
            artifacts.append(artifact)
            coverage.append(EventCoverage(symbol, start, end, acquired, artifact.sha256))
        version = "akshare-1.18.88-endpoint-safe-parser-v1;implementation=" + implementation_sha256([__file__])
        return SourceSnapshot("SINA_HFQ_FACTOR_OBSERVATIONS", version,
                              acquired, (), tuple(coverage), tuple(artifacts),
                              ("FACTOR_ONLY_NO_EVENT_TERMS_OR_ANNOUNCEMENT_TIMES", "HISTORICAL_COVERAGE_UNVERIFIED"))
