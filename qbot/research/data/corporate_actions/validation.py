"""Strict event facts and bitemporal selection; no inferred zero terms."""

from dataclasses import asdict
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Dict

from qbot.research.data.evidence import date, fail, json_bytes, require_hash, require_text, timestamp
from .contracts import CorporateAction, EventView, SourceSnapshot


EVENT_TYPES = frozenset({"CASH_DIVIDEND", "BONUS", "CAPITALIZATION", "RIGHTS",
                         "SPLIT", "CONSOLIDATION", "COMPOSITE", "CODE_CHANGE",
                         "MERGER", "DELISTING", "OTHER"})


def decimal(value: Any, label: str, *, positive: bool = False) -> Decimal:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        fail("explicit decimal %s required (no float coercion)" % label)
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        fail("invalid decimal %s" % label)
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        fail("invalid decimal %s" % label)
    return result


def normalize_per_share(value: Any, original_share_base: Any) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return decimal(value, "original term") / decimal(original_share_base, "share base", positive=True)


def validate_event_identity(event: CorporateAction) -> None:
    for name in ("security_id", "event_id", "source_id"):
        require_text(getattr(event, name), name)
    require_hash(event.source_sha256)
    if isinstance(event.revision, bool) or not isinstance(event.revision, int) or event.revision < 1:
        fail("event revision must be a positive integer")
    date(event.effective_date)
    if timestamp(event.available_at) < timestamp(event.announced_at):
        fail("availability precedes announcement")
    if event.event_type not in EVENT_TYPES or event.status not in {"PROPOSED", "IMPLEMENTED", "CANCELLED"}:
        fail("unknown event type/status")
    if event.basis not in {"PER_OLD_SHARE", "SEQUENTIAL_AFTER_PREVIOUS_EVENT"}:
        fail("unknown event share basis")
    if event.sequence is not None and (isinstance(event.sequence, bool)
                                       or not isinstance(event.sequence, int) or event.sequence < 1):
        fail("invalid event sequence")
    if not isinstance(event.synthetic_only, bool):
        fail("synthetic_only must be explicit")


def event_terms(event: CorporateAction) -> Dict[str, Decimal]:
    validate_event_identity(event)
    if event.status != "IMPLEMENTED":
        fail("only implemented events can adjust prices")
    if event.event_type in {"CODE_CHANGE", "MERGER", "DELISTING", "OTHER"}:
        fail("UNSUPPORTED corporate action requires independent identity/valuation contract")
    if event.currency != "CNY":
        fail("CNY company action terms required")
    if event.rule_id != "STANDARD_CNY_PER_OLD_SHARE_V1":
        fail("UNSUPPORTED event rule; standard formula cannot be inferred")
    require_text(event.original_terms, "original event terms")
    values = {k: decimal(getattr(event, k), k) for k in ("cash", "bonus", "capitalization", "rights")}
    allowed_terms = {
        "CASH_DIVIDEND": {"cash"}, "BONUS": {"bonus"}, "CAPITALIZATION": {"capitalization"},
        "RIGHTS": {"rights"}, "SPLIT": set(), "CONSOLIDATION": set(),
        "COMPOSITE": {"cash", "bonus", "capitalization", "rights"},
    }[event.event_type]
    if any(value and name not in allowed_terms for name, value in values.items()):
        fail("event type conflicts with supplied terms")
    if values["rights"] > 0:
        values["rights_price"] = decimal(event.rights_price, "rights_price", positive=True)
        require_text(event.eligibility, "rights eligibility")
        if event.subscription_start is None or event.subscription_end is None or event.tradable_date is None:
            fail("INCOMPLETE rights subscription/tradability terms")
        if date(event.subscription_start) > date(event.subscription_end):
            fail("inverted subscription dates")
    else:
        # Zero is proved by an explicit rights=0 term, not a missing field.
        values["rights_price"] = Decimal(0)
        if event.rights_price is not None:
            decimal(event.rights_price, "rights_price")
    if event.event_type in {"SPLIT", "CONSOLIDATION"}:
        values["multiplier"] = decimal(event.split_ratio, "split_ratio", positive=True)
        if values["bonus"] or values["capitalization"] or values["rights"]:
            fail("split with simultaneous share terms needs a separate composite rule")
        if event.tradable_date is None:
            fail("INCOMPLETE split tradability terms")
        if (event.event_type == "SPLIT" and values["multiplier"] <= 1
                or event.event_type == "CONSOLIDATION" and values["multiplier"] >= 1):
            fail("split/consolidation ratio direction differs")
    else:
        if event.split_ratio is not None:
            fail("unexpected split term")
        values["multiplier"] = Decimal(1) + values["bonus"] + values["capitalization"] + values["rights"]
    if values["cash"] and event.pay_date is None:
        fail("INCOMPLETE dividend payment date")
    if (values["bonus"] or values["capitalization"]) and (event.credit_date is None or event.tradable_date is None):
        fail("INCOMPLETE share credit/tradability dates")
    if event.record_date is None:
        fail("INCOMPLETE entitlement record date")
    if date(event.record_date) >= date(event.effective_date):
        fail("record date must precede effective date")
    for name in ("pay_date", "credit_date", "tradable_date", "subscription_start", "subscription_end"):
        value = getattr(event, name)
        if value is not None:
            date(value)
    values["close"] = decimal(event.reference_close, "reference_close", positive=True)
    if event.reference_price is not None:
        values["published_reference"] = decimal(event.reference_price, "reference_price", positive=True)
    return values


def validate_snapshot(snapshot: SourceSnapshot) -> None:
    require_text(snapshot.source_id, "source_id")
    require_text(snapshot.source_version, "source_version")
    acquired = timestamp(snapshot.acquired_at)
    if not isinstance(snapshot.synthetic_only, bool):
        fail("snapshot synthetic_only must be explicit")
    if snapshot.history_kind not in {"HISTORICALLY_RECONSTRUCTED", "CONTEMPORANEOUS"}:
        fail("unknown history kind")
    artifacts = {a.sha256 for a in snapshot.artifacts}
    for artifact in snapshot.artifacts:
        artifact.read()
    seen = set()
    revisions = {}
    for event in snapshot.events:
        validate_event_identity(event)
        key = (event.security_id, event.event_id, event.revision)
        if key in seen:
            fail("duplicate event revision")
        seen.add(key)
        revisions.setdefault(key[:2], []).append(event)
        if event.source_sha256 not in artifacts:
            fail("event source is not bound to snapshot artifacts")
        if timestamp(event.available_at) > acquired:
            fail("event availability exceeds acquisition")
        if event.synthetic_only and not snapshot.synthetic_only:
            fail("synthetic event cannot be relabelled as market evidence")
    for versions in revisions.values():
        ordered = sorted(versions, key=lambda e: e.revision)
        times = [timestamp(e.available_at) for e in ordered]
        if times != sorted(set(times)):
            fail("event revisions require strictly increasing availability")
    for coverage in snapshot.coverage:
        require_text(coverage.security_id, "coverage security_id")
        if date(coverage.start) > date(coverage.end):
            fail("inverted event coverage")
        if coverage.status not in {"UNKNOWN", "PARTIAL", "COMPLETE"}:
            fail("unknown event coverage status")
        if coverage.source_sha256 not in artifacts:
            fail("coverage lacks a bound source artifact")
        if timestamp(coverage.available_at) > acquired:
            fail("coverage availability exceeds acquisition")


def known_versions(snapshot: SourceSnapshot, end, knowledge):
    """A rescheduled/cancelled revision must not disappear behind a date filter."""
    known = [e for e in snapshot.events if timestamp(e.available_at) <= knowledge]
    relevant = {(e.security_id, e.event_id) for e in known if date(e.effective_date) <= end}
    return tuple(sorted((e for e in known if (e.security_id, e.event_id) in relevant),
                        key=lambda e: (e.effective_date, e.security_id, e.event_id, e.revision)))


def select_events(snapshot: SourceSnapshot, *, effective_through: str,
                  knowledge_as_of: str) -> EventView:
    """Preserve known revisions; the engine selects each row's actual vintage."""
    validate_snapshot(snapshot)
    end, knowledge = date(effective_through), timestamp(knowledge_as_of)
    events = known_versions(snapshot, end, knowledge)
    return EventView(snapshot, events, effective_through, knowledge_as_of)


def event_from_dict(payload: Dict[str, Any]) -> CorporateAction:
    if not isinstance(payload, dict) or set(payload) != set(CorporateAction.__dataclass_fields__):
        fail("event JSON requires exact fields")
    value = dict(payload)
    for name in ("cash", "bonus", "capitalization", "rights", "rights_price", "split_ratio", "reference_close", "reference_price"):
        if value[name] is not None:
            value[name] = decimal(value[name], name)
    event = CorporateAction(**value)
    validate_event_identity(event)
    return event
