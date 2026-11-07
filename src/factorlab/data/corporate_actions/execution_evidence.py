"""Evidence supplementation for SOFTWARE_ONLY cash execution, never admission.

Original candidates remain immutable. A reviewed revision, field-level sources
and a content-bound association are required separately from range completeness.
JSON evidence is a review claim, not independent certification of its truth.
"""
from dataclasses import dataclass
import json
from typing import Tuple

import pandas as pd

from factorlab.data.evidence import SourceArtifact, date, fail, json_bytes, require_text, sha256, timestamp
from .contracts import CorporateAction, SourceSnapshot
from .validation import decimal, event_terms, validate_event_identity, validate_snapshot


EXECUTION_FIELDS = ("status", "record_date", "effective_date", "pay_date", "cash",
                    "currency", "basis", "rule_id", "original_terms", "bonus",
                    "capitalization", "rights", "reference_close", "announced_at")


@dataclass(frozen=True)
class EventSupplement:
    original: CorporateAction
    source_version: str
    association: SourceArtifact


@dataclass(frozen=True)
class DividendTax:
    event_id: str
    status: str = "UNKNOWN"
    rate: str = None
    rule_id: str = "UNKNOWN"


@dataclass(frozen=True)
class DividendExecutionPlan:
    snapshot: SourceSnapshot
    supplements: Tuple[EventSupplement, ...]
    taxes: Tuple[DividendTax, ...] = ()
    scope: str = "SOFTWARE_ONLY"


def _read_json(artifact):
    try:
        payload = json.loads(artifact.read())
        if not isinstance(payload, dict):
            fail("execution evidence JSON object required")
        return payload
    except (ValueError, TypeError) as exc:
        fail("invalid execution evidence JSON: " + str(exc))


def validate_execution_plan(plan, security_id, dates):
    """Validate all inputs before trading; missing/conflicting evidence fails closed.

    Date-only knowledge uses the shared end-of-day upper bound. Event terms
    must be available by record close (15:00 Shanghai); a later revision cannot
    silently rewrite an entitlement. Coverage must be known before first open.
    """
    if not isinstance(plan, DividendExecutionPlan) or plan.scope != "SOFTWARE_ONLY":
        fail("typed SOFTWARE_ONLY dividend execution plan required")
    snap = plan.snapshot
    validate_snapshot(snap)
    if snap.issues:
        fail("unresolved event conflicts/missing fields: " + repr(snap.issues))
    artifacts = {a.sha256: a for a in snap.artifacts}
    events = sorted(snap.events, key=lambda e: (e.effective_date, e.event_id))
    ids = [e.event_id for e in events]
    if len(ids) != len(set(ids)):
        fail("duplicate/revised event; resolve revisions explicitly")
    supplements = {s.original.event_id: s for s in plan.supplements}
    if len(supplements) != len(plan.supplements) or set(supplements) != set(ids):
        fail("exact original event associations required")
    first_open = dates[0].tz_localize("Asia/Shanghai") + pd.Timedelta(hours=9, minutes=30)
    covers = [c for c in snap.coverage if c.security_id == security_id and c.status == "COMPLETE"
              and date(c.start) <= dates[0] and date(c.end) >= dates[-1]]
    if not covers:
        fail("INCOMPLETE range coverage; two dividends do not prove completeness")
    # A COMPLETE label alone is insufficient: bind enumerated identities and a
    # review statement to the actual coverage artifact, including empty ranges.
    for cover in covers:
        claim = _read_json(artifacts[cover.source_sha256])
        expected = sorted([[e.event_id, e.revision] for e in events])
        if (claim.get("security_id") != security_id or claim.get("start") != cover.start
                or claim.get("end") != cover.end or claim.get("event_revisions") != expected
                or not claim.get("completeness_statement")
                or timestamp(cover.available_at) >= first_open):
            fail("range completeness evidence/availability differs")
    for event in events:
        if event.security_id != security_id:
            fail("wrong security in execution events")
        if event.event_type != "CASH_DIVIDEND":
            fail("UNSUPPORTED corporate action execution: " + event.event_type)
        event_terms(event)  # Reuse reviewed terms, explicit zeroes and date validation.
        record, ex, pay = map(date, (event.record_date, event.effective_date, event.pay_date))
        if record not in dates or ex not in dates or not record < ex <= pay:
            fail("complete record/ex observations and ordered payment date required")
        supplement = supplements[event.event_id]
        original = supplement.original
        validate_event_identity(original)
        require_text(supplement.source_version, "original source version")
        if (original.source_sha256 not in artifacts
                or supplement.association.sha256 not in artifacts):
            fail("missing original version/source/association evidence")
        # Immutable source identity; normalization changes get their own revision.
        for field in ("security_id", "event_id", "source_id", "source_sha256", "event_type", "synthetic_only"):
            if getattr(original, field) != getattr(event, field):
                fail("original source identity differs: " + field)
        if event.revision != original.revision + 1 or timestamp(event.available_at) <= timestamp(original.available_at):
            fail("supplement requires a later revision and availability")
        link = _read_json(supplement.association)
        if (link.get("original_sha256") != sha256(json_bytes(original))
                or link.get("implemented_sha256") != sha256(json_bytes(event))
                or link.get("source_version") != supplement.source_version):
            fail("announcement/original event association differs")
        fields = link.get("fields", {})
        cutoff = record.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15)
        if timestamp(event.available_at) > cutoff:
            fail("event evidence unavailable at record close")
        for name in EXECUTION_FIELDS:
            fact = fields.get(name, {})
            if (fact.get("value") != str(getattr(event, name)) or not fact.get("locator")
                    or fact.get("source_sha256") not in artifacts):
                fail("missing/conflicting field source: " + name)
            if timestamp(fact.get("available_at")) > timestamp(event.available_at):
                fail("field evidence exceeds event availability")
    taxes = {t.event_id: t for t in plan.taxes}
    if len(taxes) != len(plan.taxes) or set(taxes) - set(ids):
        fail("duplicate or unknown tax event identity")
    rates = {}
    for event in events:
        tax = taxes.get(event.event_id, DividendTax(event.event_id))
        if tax.status == "UNKNOWN" and tax.rate is None:
            rates[event.event_id] = None
        elif (tax.status == "SYNTHETIC_EXPLICIT" and event.synthetic_only
              and tax.rule_id == "SYNTHETIC_FLAT_RATE_V1"):
            rate = decimal(tax.rate, "synthetic tax rate")
            if rate > 1:
                fail("tax rate exceeds one")
            rates[event.event_id] = rate
        else:
            fail("unsupported tax contract; real holding-period tax remains UNKNOWN")
    return tuple(events), rates
