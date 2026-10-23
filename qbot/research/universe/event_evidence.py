"""Offline event evidence checklist, separate from every formal PIT rule.

Hash binding and a completed checklist do not authenticate source statements.
This module cannot certify an event, replay members, or publish a universe.
"""

from datetime import date
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re

from qbot.research.errors import ProviderError


def _date(value):
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def verify_local_source(root, reference):
    """Bind one explicit relative file; no discovery, HTTP, or cache fallback."""
    relative = reference.get("local_relative_path")
    digest = reference.get("sha256")
    if (not isinstance(relative, str) or not relative or "\\" in relative or
            Path(relative).is_absolute() or PureWindowsPath(relative).drive or
            ".." in Path(relative).parts or not _sha(digest)):
        raise ProviderError("source reference requires a relative path and SHA256")
    root = Path(root).resolve()
    path = root / relative
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise ProviderError("source reference escapes the explicit archive root") from exc
    if any(item.is_symlink() for item in [path, *path.parents] if item != root):
        raise ProviderError("source reference cannot traverse a symlink")
    result = {"local_relative_path": relative, "expected_sha256": digest}
    if not path.is_file():
        return {**result, "status": "MISSING", "observed_sha256": None}
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ProviderError("source exceeds bounded evidence size")
    raw = path.read_bytes()
    if len(raw) > 64 * 1024 * 1024:
        raise ProviderError("source exceeds bounded evidence size")
    observed = hashlib.sha256(raw).hexdigest()
    return {**result, "bytes": len(raw), "observed_sha256": observed,
            "status": "MATCH" if observed == digest else "SHA_MISMATCH"}


def event_source_references(event):
    """Reuse the existing pit-events audit fields without a new source store."""
    references = []
    source = event.get("source_evidence", {})
    digest = source.get("raw_content_sha256") or source.get("container_sha256")
    if source.get("local_relative_path") and digest:
        references.append({"local_relative_path": source["local_relative_path"], "sha256": digest})
    for item in event.get("attachments", []):
        if item.get("local_relative_path") and item.get("raw_content_sha256"):
            references.append({"local_relative_path": item["local_relative_path"],
                               "sha256": item["raw_content_sha256"]})
        if item.get("receipt_relative_path") and item.get("receipt_sha256"):
            references.append({"local_relative_path": item["receipt_relative_path"],
                               "sha256": item["receipt_sha256"]})
    for item in event.get("independent_actual_membership_evidence", []):
        references.append({"local_relative_path": item.get("local_relative_path"),
                           "sha256": item.get("sha256")})
    for item in event.get("condition_trigger_evidence", []) + event.get("case_source_files", []):
        references.append({"local_relative_path": item.get("local_relative_path"),
                           "sha256": item.get("sha256")})
    return references


def check_event_evidence(event, verified_sources=()):
    """List missing requirements; even REVIEW_READY is not certification.

The independent evidence role/coverage is an auditable input assertion needing
human review. Listing, delisting, weekly update dates and calendars cannot fill
the actual membership field. Historical availability is checked separately.
"""
    if not isinstance(event, dict) or type(event.get("notice_id")) is not int or event["notice_id"] < 1:
        raise ProviderError("event requires a positive notice ID")
    verified = set(verified_sources)
    def bound(path, digest):
        return _sha(digest) and (path, digest) in verified
    gaps = []
    if not _date(event.get("announcement_publish_date")):
        gaps.append("ANNOUNCEMENT_PUBLIC_DATE_UNKNOWN")
    for field in ("planned_effective_after_close_date", "planned_explicit_start_date"):
        if event.get(field) is not None and not _date(event[field]):
            raise ProviderError("event dates must be explicit YYYY-MM-DD values")
    source = event.get("source_evidence", {})
    notice_hashes = {source.get(key) for key in
                     ("raw_content_sha256", "container_sha256", "response_text_utf8_sha256")}
    body_hash = source.get("raw_content_sha256") or source.get("container_sha256")
    if not bound(source.get("local_relative_path"), body_hash):
        gaps.append("NOTICE_BYTES_NOT_BOUND")
    attachments = event.get("attachments", [])
    if not attachments or any(not bound(item.get("local_relative_path"), item.get("raw_content_sha256")) or
                              not bound(item.get("receipt_relative_path"), item.get("receipt_sha256"))
                              for item in attachments):
        gaps.append("ATTACHMENT_BYTES_OR_RECEIPTS_NOT_BOUND")
    # Retrieval receipts are notice-derived too, even when copied or rehashed.
    # Neither actual membership nor condition confirmation may reuse them.
    notice_hashes.update(item.get(key) for item in attachments
                         for key in ("raw_content_sha256", "receipt_sha256"))
    additions, removals = event.get("candidate_add_count"), event.get("candidate_remove_count")
    if type(additions) is not int or type(removals) is not int or not 1 <= additions == removals <= 500:
        gaps.append("EXPLICIT_BALANCED_DELTA_MISSING")
    changes = event.get("candidate_changes", [])
    delta = []
    for item in changes:
        action, symbol = item.get("action"), item.get("symbol")
        if action not in ("ADD", "REMOVE") or not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}\.(SH|SZ)", symbol):
            continue
        delta.append((action, symbol))
    delta_complete = (bool(delta) and len(delta) == len(changes) == additions + removals and
                      len(set(delta)) == len(delta) and
                      sum(action == "ADD" for action, _ in delta) == additions and
                      not ({symbol for action, symbol in delta if action == "ADD"} &
                           {symbol for action, symbol in delta if action == "REMOVE"})) if (
                               type(additions) is int and type(removals) is int) else False
    if not delta_complete:
        gaps.append("COMPLETE_EXPLICIT_EVENT_DELTA_NOT_BOUND")
    delta_hash = hashlib.sha256(json.dumps(sorted(delta), separators=(",", ":")).encode("utf-8")).hexdigest()
    if type(event.get("unresolved_add_symbol_count")) is not int or event["unresolved_add_symbol_count"] != 0:
        gaps.append("HISTORICAL_ADD_SYMBOL_UNRESOLVED")
    claimed = event.get("actual_effective_member_date")
    if claimed is not None and not _date(claimed):
        raise ProviderError("actual date must be UNKNOWN/null or explicit YYYY-MM-DD")
    supported = []
    for item in event.get("independent_actual_membership_evidence", []):
        if (claimed is not None and item.get("kind") == "ACTUAL_INDEX_MEMBERSHIP" and
                item.get("index_code") == "000905" and item.get("notice_id") == event["notice_id"] and
                item.get("effective_member_date") == claimed and
                item.get("boundary") == "FIRST_INCLUDED_TRADING_SESSION" and
                item.get("coverage") == "FULL_EVENT_DELTA" and
                delta_complete and item.get("delta_sha256") == delta_hash and
                bound(item.get("local_relative_path"), item.get("sha256")) and
                item["sha256"] not in notice_hashes):
            supported.append(item["sha256"])
    if not supported:
        gaps.append("INDEPENDENT_ACTUAL_MEMBERSHIP_EVIDENCE_MISSING")
    if claimed is not None and not supported:
        gaps.append("UNSUPPORTED_ACTUAL_DATE_CLAIM")
    trigger_status = event.get("condition_trigger_status", "UNKNOWN")
    if event.get("conditional_effective_clause"):
        # A trigger confirmation is necessary, and never sufficient for actual
        # index implementation. No trigger date is copied to the actual date.
        trigger_bound = any(bound(item.get("local_relative_path"), item.get("sha256")) and
                            item["sha256"] not in notice_hashes
                            for item in event.get("condition_trigger_evidence", []))
        if trigger_status != "INDEPENDENTLY_CONFIRMED" or not _date(event.get("condition_trigger_date")) or not trigger_bound:
            gaps.append("CONDITION_TRIGGER_CONFIRMATION_MISSING")
            trigger_status = "UNKNOWN"
    if event.get("historical_identity_status") != "INDEPENDENTLY_REVIEWED":
        gaps.append("HISTORICAL_IDENTITY_LIFECYCLE_REVIEW_INCOMPLETE")
    if event.get("correction_review_status") != "INDEPENDENTLY_REVIEWED":
        gaps.append("CORRECTION_AND_TEMPORARY_EVENT_COVERAGE_UNKNOWN")
    if event.get("unresolved_conflicts", []):
        gaps.append("UNRESOLVED_SOURCE_CONFLICTS")
    historical = event.get("historical_availability_of_current_bytes_verified") is True
    return {"notice_id": event["notice_id"], "event_status": "CANDIDATE_ONLY" if gaps else "REVIEW_READY_NOT_CERTIFIED",
            "claimed_actual_effective_member_date": claimed,
            "actual_effective_member_date": claimed if supported else None,
            "actual_date_status": "EVIDENCE_BOUND_REQUIRES_HUMAN_REVIEW" if supported else "UNKNOWN",
            "announcement_publish_date": event.get("announcement_publish_date"),
            "announcement_publish_time": event.get("announcement_publish_time"),
            "planned_effective_after_close_date": event.get("planned_effective_after_close_date"),
            "planned_explicit_start_date": event.get("planned_explicit_start_date"),
            "conditional_effective_clause": event.get("conditional_effective_clause"),
            "condition_trigger_status": trigger_status,
            "condition_trigger_date": event.get("condition_trigger_date"),
            "candidate_add_count": additions, "candidate_remove_count": removals,
            "missing_requirements": gaps,
            "historical_availability_status": "INPUT_ASSERTION_REQUIRES_REVIEW" if historical else "UNKNOWN",
            "formal_admission": "UNCHANGED_BLOCKED", "point_in_time_universe_verified": False,
            "certified_event": False, "daily_pit_publication_allowed": False}


def unavailable_baseline_replay_result():
    """Report a skipped pilot, without inventing a baseline or daily counts."""
    return {"status": "NOT_ATTEMPTED_AUTHORITY_BASELINE_ABSENT",
            "baseline_date": "2021-09-13", "authoritative_baseline_member_count": None,
            "certified_events_applied": 0, "membership_rows_generated": 0,
            "daily_member_count_independently_checked": False,
            "daily_member_counts": None, "certified_daily_pit_published": False,
            "blockers": ["AUTHORITATIVE_COMPLETE_500_BASELINE_MISSING",
                         "INDEPENDENT_ACTUAL_EVENT_BOUNDARIES_MISSING",
                         "COMPLETE_TEMPORARY_CONDITIONAL_CORRECTION_CHAIN_UNPROVED"]}
