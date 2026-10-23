"""Artificial PDF text layouts; no fixture asserts real historical membership."""

import pytest

from qbot.research.errors import ProviderError
from qbot.research.universe.notice_pdf import PDF_TABLE_CONTRACT, review_csi500_pdf_pages


HEAD = "中证 500 指数样本调整名单：\n调出名单 调入名单\n证券代码 证券名称 证券代码 证券名称\n"
ROW1 = "000001 人工调出 A 600001 人工调入甲\n"
ROW2 = "688001 *ST 人工乙 000002 人工调入-U\n"
STOP = "中证 1000 指数样本调整名单：\n"


def notice(**kwargs):
    result = {"id": 1, "publishDate": "2023-05-26", "title": "人工公告，非市场证据",
              "content": "<p>中证500指数更换2只样本，2023年6月9日收市后生效。</p>"}
    result.update(kwargs)
    return result


def test_cross_page_pairs_direction_names_and_audit_coordinates():
    pages = ["无关前言\n" + HEAD + ROW1, ROW2 + "\n" + STOP + ROW1,
             "中证500指数备选名单：\n" + ROW1 + ROW2]
    result = review_csi500_pdf_pages(notice(), pages)
    assert result.contract == PDF_TABLE_CONTRACT
    assert result.heading_page == 1 and result.heading_line == 2
    assert result.termination_page == 2 and result.termination_line == 3
    assert result.termination_heading == STOP.strip()
    assert result.pair_counts_by_page == ((1, 1), (2, 1))
    assert result.visual_review_performed_by_parser is False
    reviewed = result.review
    assert reviewed.explicit_after_close_date == "2023-06-09"
    assert reviewed.status == "PARSED_SOURCE_CANDIDATE_NOT_PIT"
    assert reviewed.point_in_time_universe_verified is False
    assert not hasattr(result, "get_members") and not hasattr(reviewed, "effective_trading_date")
    assert [(row["action"], row["symbol"], row["name"], row["source_page"], row["source_line"])
            for row in reviewed.changes] == [
        ("REMOVE", "000001.SZ", "人工调出 A", 1, 5), ("ADD", "600001.SH", "人工调入甲", 1, 5),
        ("REMOVE", "688001.SH", "*ST 人工乙", 2, 1), ("ADD", "000002.SZ", "人工调入-U", 2, 1)]


@pytest.mark.parametrize("stop", [STOP, "中证500指数备选名单：\n", "备选名单\n"])
def test_explicit_next_index_or_reserve_heading_stops_before_even_valid_paired_rows(stop):
    result = review_csi500_pdf_pages(notice(), [HEAD + ROW1 + ROW2 + stop + ROW1 + ROW2])
    assert len(result.review.changes) == 4 and result.termination_heading == stop.strip()


def test_end_of_document_is_recorded_not_an_inferred_date():
    result = review_csi500_pdf_pages(notice(), [HEAD + ROW1 + ROW2])
    assert result.termination_heading == "END_OF_DOCUMENT"


def test_repeated_two_line_header_on_continuation_page():
    continued = "调出名单 调入名单\n股票代码 股票名称 股票代码 股票名称\n"
    assert len(review_csi500_pdf_pages(notice(), [HEAD + ROW1, continued + ROW2 + STOP]).review.changes) == 4


@pytest.mark.parametrize("pages", [[], "text", None, [None], [1], [HEAD] * 101,
                                 ["中" * (1024 * 1024)]], ids=["empty", "scalar", "null", "null-page",
                                 "number-page", "too-many-pages", "too-many-bytes"])
def test_invalid_or_oversized_page_container(pages):
    with pytest.raises(ProviderError):
        review_csi500_pdf_pages(notice(), pages)


@pytest.mark.parametrize("head", ["中证500行业指数样本调整名单：\n", "中证500指数备选名单：\n",
                                  "中证500指数样本调整名单（续）：\n", "中证500指数\n"])
def test_nearby_headings_never_start_membership_review(head):
    with pytest.raises(ProviderError, match="exactly one"):
        review_csi500_pdf_pages(notice(), [head + ROW1 + ROW2])


def test_two_csi500_tables_are_ambiguous_even_when_one_would_match_count():
    with pytest.raises(ProviderError, match="exactly one"):
        review_csi500_pdf_pages(notice(), [HEAD + ROW1 + ROW2 + STOP, HEAD + ROW1 + ROW2])


@pytest.mark.parametrize("replacement", ["调入名单 调出名单", "调出名单", "调出 调入",
                                         "调出名单 调入名单 备选名单"])
def test_direction_never_assumed(replacement):
    with pytest.raises(ProviderError, match="direction/header"):
        review_csi500_pdf_pages(notice(), [HEAD.replace("调出名单 调入名单", replacement) + ROW1 + ROW2])


@pytest.mark.parametrize("replacement", ["证券名称 证券代码 证券名称 证券代码", "证券代码 证券名称",
                                         "证券代码 证券名称 股票代码 股票名称"])
def test_security_header_never_assumed(replacement):
    with pytest.raises(ProviderError, match="column header"):
        review_csi500_pdf_pages(notice(), [HEAD.replace("证券代码 证券名称 证券代码 证券名称", replacement) + ROW1 + ROW2])


@pytest.mark.parametrize("row", ["00001 人工甲 600001 人工乙\n", "- 人工甲 600001 人工乙\n",
                                "０００００１ 人工甲 600001 人工乙\n", "000001 600001 人工乙\n",
                                "000001 人工甲 - 人工乙\n", "000001 - 600001 人工乙\n",
                                "000001 人工甲 600001 人工乙 000003 人工丙\n",
                                "000001 人工甲 600001 000003\n", "脚注、未知提取内容\n"])
def test_malformed_codes_or_names_and_unknown_active_lines_fail(row):
    with pytest.raises(ProviderError):
        review_csi500_pdf_pages(notice(), [HEAD + row + ROW2])


@pytest.mark.parametrize("row", [ROW1, ROW2.replace("000002", "000001"), ROW2.replace("000002", "600001")])
def test_duplicate_or_overlapping_changes_rejected(row):
    with pytest.raises(ProviderError, match="duplicate or overlapping"):
        review_csi500_pdf_pages(notice(), [HEAD + ROW1, row + STOP])


@pytest.mark.parametrize("tail", ["", ROW2 + ROW1])
def test_missing_or_excess_pairs_cannot_be_hidden_by_early_stop(tail):
    with pytest.raises(ProviderError, match="stated change count"):
        review_csi500_pdf_pages(notice(), [HEAD + ROW1 + tail + STOP])


def test_blank_scanned_continuation_page_requires_review():
    with pytest.raises(ProviderError, match="empty extracted page"):
        review_csi500_pdf_pages(notice(), [HEAD + ROW1, "\n", ROW2 + STOP])


@pytest.mark.parametrize("pages", [[HEAD], [HEAD + STOP], [HEAD + ROW1, "调出名单 调入名单\n"],
                                  [HEAD + ROW1, "调入名单 调出名单\n" + ROW2]])
def test_truncated_or_unverified_continuation_headers_rejected(pages):
    with pytest.raises(ProviderError):
        review_csi500_pdf_pages(notice(), pages)


def test_no_explicit_count_does_not_estimate_it_from_pdf():
    with pytest.raises(ProviderError, match="explicit stated"):
        review_csi500_pdf_pages(notice(content="<p>中证500指数调整</p>"), [HEAD + ROW1 + ROW2])


@pytest.mark.parametrize("content", ["<p>中证500指数更换2只样本。</p>",
                                    "<p>中证500指数更换2只样本，自相关证券上市日起生效。</p>"])
def test_unknown_or_conditional_effective_date_not_inferred(content):
    result = review_csi500_pdf_pages(notice(content=content), [HEAD + ROW1 + ROW2]).review
    assert result.status == "EFFECTIVE_DATE_REVIEW_REQUIRED"
    assert result.explicit_after_close_date is None and result.explicit_start_date is None


def test_inline_html_disagreement_requires_review_not_pdf_override():
    from tests.research.test_notice_review import table
    html = "<p>中证500指数更换2只样本，2023年6月9日收市后生效。</p>" + table()
    with pytest.raises(ProviderError, match="identities disagree"):
        review_csi500_pdf_pages(notice(content=html), [HEAD + ROW1 + ROW2])
