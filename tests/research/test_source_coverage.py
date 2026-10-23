"""Artificial source records prove only coverage-audit behavior, never PIT."""

from datetime import date, datetime

import pytest

from qbot.research.errors import ProviderError
from qbot.research.universe.source_coverage import audit_notice_coverage


START, END = date(2021, 1, 1), date(2021, 12, 31)


def listing(notice_id=1, published="2021-06-01", title="人工调整公告"):
    return {"id": notice_id, "publishDate": published, "title": title}


def record(notice_id=1, content="<p>人工正文</p>", evidence=None, **kwargs):
    return ({**listing(notice_id, **kwargs), "content": content, "enclosureList": []},
            evidence or {"transport": "SYNTHETIC TEST ONLY", "point_in_time_universe_verified": False})


def audit(catalogue=None, notices=None, start=START, end=END):
    return audit_notice_coverage(catalogue if catalogue is not None else [listing(), listing(2)],
                                 notices if notices is not None else [record()], start, end)


def test_missing_body_is_unknown_not_zero_changes_or_zero_attachments():
    result = audit()
    assert result["missing_notice_ids"] == [2] and result["observed_bodies_in_scope"] == 1
    assert result["body_coverage_complete_for_this_catalogue_window"] is False
    missing = result["rows"][1]
    assert missing["body_mentions_csi500"] is None and missing["discovered_attachment_count"] is None
    assert missing["attachment_interpretation_status"] == "BODY_MISSING_ATTACHMENTS_UNKNOWN"


def test_complete_body_coverage_does_not_claim_pit_or_historical_availability():
    result = audit(notices=[record(), record(2)])
    assert result["body_coverage_complete_for_this_catalogue_window"] is True
    for field in ("point_in_time_universe_verified", "research_conclusions_permitted", "historical_constituents_generated",
                  "starting_membership_baseline_verified", "effective_dates_inferred", "historical_availability_of_current_bytes_verified"):
        assert result[field] is False


def test_missing_csi500_mention_does_not_ignore_actual_file_links():
    content = '<p>人工其他指数公告<a href="https://oss-ch.csindex.com.cn/notice/artificial.xlsx">附件</a></p>'
    result = audit(notices=[record(content=content)])
    assert result["rows"][0]["body_mentions_csi500"] is False
    assert result["rows"][0]["discovered_attachment_count"] == 1
    assert result["attachment_gaps"][0]["status"] == "CONTENT_NOT_REVIEWED_BY_THIS_AUDIT"
    assert result["attachment_gaps"][0]["publisher_url"].endswith("artificial.xlsx")


def test_no_discovered_file_is_not_a_membership_verdict():
    result = audit(notices=[record(content="中证500人工公告")])
    assert result["body_mentions_csi500_count"] == 1
    assert result["rows"][0]["attachment_interpretation_status"] == "NO_FILE_LINK_DISCOVERED_NOT_A_MEMBERSHIP_VERDICT"


def test_original_evidence_representation_preserved_without_relabelling():
    evidence = {"representation": "ARTIFICIAL DECODED TEXT NOT WIRE BYTES", "transport": "synthetic-browser",
                "observation_sha256": "a" * 64, "point_in_time_universe_verified": False}
    result = audit(notices=[record(evidence=evidence)])
    assert result["source_evidence"] == [{"notice_id": 1, "source_evidence": evidence}]
    assert "external_timestamp_proof" not in result["source_evidence"][0]["source_evidence"]


def test_scope_uses_publication_dates_without_promoting_to_effective_dates():
    catalogue = [listing(3, "2022-01-01"), listing(2, "2021-01-01"), listing(1, "2021-12-31")]
    result = audit(catalogue, [record(3, published="2022-01-01"), record(2, published="2021-01-01")])
    assert [r["notice_id"] for r in result["rows"]] == [2, 1]
    assert result["observed_bodies_outside_scope"] == 1 and result["missing_notice_ids"] == [1]


@pytest.mark.parametrize("start,end", [(END, START), ("2021-01-01", END), (START, "2021-12-31"),
                                       (datetime(2021, 1, 1), END)])
def test_invalid_window_fails(start, end):
    with pytest.raises(ProviderError):
        audit(start=start, end=end)


@pytest.mark.parametrize("catalogue", [[], None, [listing(), listing()], [listing(title="")],
                                      [listing(published="2021-02-30")], [listing(True)]])
def test_invalid_or_ambiguous_catalogue_fails(catalogue):
    with pytest.raises(ProviderError):
        audit_notice_coverage(catalogue, [], START, END)


@pytest.mark.parametrize("notices", [None, [()], [(None, {})], [record(), record()], [record(9)],
                                    [record(title="另一个人工标题")], [record(published="2021-06-02")],
                                    [(record()[0], {})], [record(evidence={"point_in_time_universe_verified": True})]])
def test_invalid_mismatched_duplicate_or_promoted_source_record_fails(notices):
    with pytest.raises(ProviderError):
        audit(notices=notices) if notices is not None else audit_notice_coverage([listing()], None, START, END)


def test_window_without_any_catalogue_records_is_not_100_percent_coverage():
    with pytest.raises(ProviderError, match="no catalogue"):
        audit([listing(1, "2022-01-01")], [])
