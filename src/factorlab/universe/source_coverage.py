"""Audit a bounded publisher catalogue against actual observed notice bodies.

Body coverage, a CSI500 mention and an empty attachment list are not membership
evidence. This pure audit neither fetches data nor generates constituents.
"""

from datetime import date
from typing import Any, Dict, List, Sequence, Tuple

from factorlab.errors import ProviderError
from factorlab.universe.notice_attachments import discover_notice_attachments
from factorlab.universe.notice_review import mentions_csi500
from factorlab.universe.official_sources import OfficialNoticeSource


def audit_notice_coverage(
    catalogue: Sequence[Dict[str, Any]],
    notices: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]],
    published_start: date,
    published_end: date,
) -> Dict[str, Any]:
    """Match validated bodies to catalogue IDs, titles and publication dates.

    The caller binds bytes using the existing HTTP-cache/browser readers. This
    audit retains their distinct evidence instead of inferring an authenticated
    origin or historical availability from supplied metadata.
    """
    if type(published_start) is not date or type(published_end) is not date or published_start > published_end:
        raise ProviderError("source coverage needs an ordered explicit publication-date window")
    if not isinstance(catalogue, (list, tuple)) or not 1 <= len(catalogue) <= 10000:
        raise ProviderError("source coverage requires a bounded nonempty catalogue")
    listings = {}
    for listing in catalogue:
        OfficialNoticeSource._entry(listing)
        if listing["id"] in listings:
            raise ProviderError("duplicate catalogue IDs cannot establish source coverage")
        listings[listing["id"]] = listing
    if not isinstance(notices, (list, tuple)) or len(notices) > 10000:
        raise ProviderError("source coverage needs bounded actual notice records")
    observed = {}
    for record in notices:
        if not isinstance(record, (list, tuple)) or len(record) != 2:
            raise ProviderError("source record must contain a notice and distinct source evidence")
        notice, evidence = record
        if not isinstance(notice, dict) or not isinstance(evidence, dict) or not evidence:
            raise ProviderError("source record has no notice/evidence")
        notice_id = notice.get("id")
        validated = OfficialNoticeSource.parse_notice_payload(
            {"success": True, "code": "200", "data": notice}, notice_id)
        if notice_id not in listings:
            raise ProviderError("observed notice is absent from the bound catalogue")
        listing = listings[notice_id]
        if validated["title"] != listing["title"] or validated["publishDate"] != listing["publishDate"]:
            raise ProviderError("catalogue/detail title or publication date mismatch")
        if notice_id in observed:
            raise ProviderError("duplicate observed IDs require explicit source-version selection")
        if evidence.get("point_in_time_universe_verified") is not False:
            raise ProviderError("source coverage must not accept a PIT promotion claim")
        observed[notice_id] = (validated, evidence)
    rows: List[Dict[str, Any]] = []
    attachment_gaps: List[Dict[str, Any]] = []
    evidence_records: List[Dict[str, Any]] = []
    scoped = sorted((row for row in catalogue if published_start.isoformat() <= row["publishDate"] <=
                     published_end.isoformat()), key=lambda row: (row["publishDate"], row["id"]))
    if not scoped:
        raise ProviderError("publication window contains no catalogue records")
    for listing in scoped:
        available = listing["id"] in observed
        row = {"notice_id": listing["id"], "publish_date": listing["publishDate"], "title": listing["title"],
               "body_status": "BODY_AVAILABLE" if available else "BODY_MISSING",
               "body_mentions_csi500": None, "discovered_attachment_count": None,
               "attachment_interpretation_status": "BODY_MISSING_ATTACHMENTS_UNKNOWN",
               "point_in_time_universe_verified": False}
        if available:
            notice, evidence = observed[listing["id"]]
            files = discover_notice_attachments(notice)
            row.update({"body_mentions_csi500": mentions_csi500(notice["title"] + notice["content"]),
                        "discovered_attachment_count": len(files),
                        "attachment_interpretation_status": "ATTACHMENT_CONTENT_NOT_REVIEWED_BY_THIS_AUDIT" if files else
                        "NO_FILE_LINK_DISCOVERED_NOT_A_MEMBERSHIP_VERDICT"})
            evidence_records.append({"notice_id": listing["id"], "source_evidence": evidence})
            for file in files:
                attachment_gaps.append({"notice_id": listing["id"], "publish_date": listing["publishDate"],
                                        "body_mentions_csi500": row["body_mentions_csi500"],
                                        "publisher_url": file["fileUrl"], "label": file["fileName"],
                                        "publisher_create_at": file.get("createAt"),
                                        "discovery_sources": file["discovery_sources"],
                                        "status": "CONTENT_NOT_REVIEWED_BY_THIS_AUDIT", "point_in_time_universe_verified": False})
        rows.append(row)
    missing = [row["notice_id"] for row in rows if row["body_status"] == "BODY_MISSING"]
    return {"diagnostic_only": True, "point_in_time_universe_verified": False,
            "research_conclusions_permitted": False, "historical_constituents_generated": False,
            "starting_membership_baseline_verified": False, "effective_dates_inferred": False,
            "historical_availability_of_current_bytes_verified": False,
            "catalogue_rows": len(catalogue), "scoped_rows": len(scoped),
            "observed_bodies_in_scope": len(scoped) - len(missing),
            "observed_bodies_outside_scope": len(observed) - (len(scoped) - len(missing)),
            "body_coverage_complete_for_this_catalogue_window": not missing,
            "missing_notice_ids": missing, "body_mentions_csi500_count": sum(row["body_mentions_csi500"] is True for row in rows),
            "discovered_attachments_not_reviewed_by_this_audit": len(attachment_gaps),
            "rows": rows, "attachment_gaps": attachment_gaps, "source_evidence": evidence_records}
