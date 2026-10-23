"""Explicit historical identity intervals; never infer identity from ticker."""

from dataclasses import dataclass
from typing import Tuple

import pandas as pd

from factorlab.data.evidence import SourceArtifact, date, fail, require_hash, require_text, timestamp
from factorlab.data.schema import normalize_symbol


@dataclass(frozen=True)
class SecurityIdentity:
    security_id: str
    symbol: str
    start: str
    end: str
    listing_date: str
    available_at: str
    source_sha256: str
    delisting_date: str = ""
    relationship: str = "SAME_SECURITY"
    security_type: str = "UNKNOWN"


@dataclass(frozen=True)
class IdentitySnapshot:
    intervals: Tuple[SecurityIdentity, ...]
    artifacts: Tuple[SourceArtifact, ...]
    synthetic_only: bool = False

    def validate(self) -> None:
        if not isinstance(self.synthetic_only, bool) or not self.intervals:
            fail("explicit nonempty identity snapshot required")
        hashes = {a.sha256 for a in self.artifacts}
        for a in self.artifacts:
            a.read()
        for item in self.intervals:
            require_text(item.security_id, "stable security_id")
            require_hash(item.source_sha256)
            if item.source_sha256 not in hashes:
                fail("identity has no bound source")
            if normalize_symbol(item.symbol) != item.symbol:
                fail("identity requires canonical SH/SZ code; BJ remains diagnostic")
            if item.security_type != "A_SHARE":
                fail("independently specified historical A_SHARE identity required")
            if date(item.start) > date(item.end) or date(item.listing_date) > date(item.start):
                fail("invalid historical identity interval")
            timestamp(item.available_at)
            if item.relationship != "SAME_SECURITY":
                fail("merger/relisting requires a separate identity contract")
            if item.delisting_date and date(item.end) > date(item.delisting_date):
                fail("identity extends beyond delisting")
        ordered = sorted(self.intervals, key=lambda x: (x.symbol, x.start))
        for i, left in enumerate(ordered):
            for right in ordered[i + 1:]:
                if left.symbol == right.symbol or left.security_id == right.security_id:
                    if max(date(left.start), date(right.start)) <= min(date(left.end), date(right.end)):
                        fail("overlapping identity/code intervals")

    def resolve(self, keys: pd.DataFrame, signal_as_of) -> pd.DataFrame:
        self.validate()
        if set(keys.columns) != {"date", "symbol"} or keys.duplicated(["date", "symbol"]).any():
            fail("unique explicit identity keys required")
        rows = []
        for row in keys.itertuples(index=False):
            day = pd.Timestamp(row.date)
            matches = [i for i in self.intervals if i.symbol == row.symbol and date(i.start) <= day <= date(i.end)]
            if len(matches) != 1:
                fail("historical identity gap/ambiguity at %s/%s" % (day.date(), row.symbol))
            item = matches[0]
            if day not in signal_as_of or timestamp(item.available_at) > timestamp(signal_as_of[day]):
                fail("historical identity unavailable at signal")
            rows.append({"date": day, "symbol": row.symbol, "security_id": item.security_id})
        return pd.DataFrame(rows)
