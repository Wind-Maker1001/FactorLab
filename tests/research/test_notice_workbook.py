"""Artificial extracted cells, not historical membership or source evidence."""

from copy import deepcopy

import pytest

from factorlab.errors import ProviderError
from factorlab.universe.notice_workbook import WORKBOOK_TABLE_CONTRACT, review_csi500_workbook_values


SIX = [["指数代码", "指数简称", "调出", None, "调入", None],
       [None, None, "证券代码", "证券简称", "证券代码", "证券简称"]]
TEN = [["指数代码", "指数简称", "调出", None, None, None, "调入", None, None, None],
       [None, None] + ["证券代码", "证券简称"] * 4]
PAIR = ["000905", "中证500", "600001", "旧样本", "000001", "新样本"]
OTHER = ["000300", "沪深300", "600002", "其他旧样本", "000002", "其他新样本"]
MULTI = ["000905", "中证500", "600001", "旧样本", "-", "-", "000001", "新样本", "-", "-"]


def notice(content="<p>沪深300等指数样本调整，名单见附件。</p>"):
    return {"id": 1, "publishDate": "2026-09-09", "content": content}


def sheet(rows=None, wide=False, name=None):
    return {"sheet": name or ("指数样本调整名单" if wide else "Sheet1"),
            "values": deepcopy((TEN if wide else SIX) + (rows if rows is not None else [MULTI if wide else PAIR]))}


def test_literal_csi500_row_found_even_when_body_has_no_keyword():
    result = review_csi500_workbook_values(notice(), [sheet([OTHER, PAIR])])
    assert result.body_mentions_csi500 is False
    assert result.review.status == "EFFECTIVE_DATE_REVIEW_REQUIRED"
    assert result.contract == WORKBOOK_TABLE_CONTRACT
    assert result.review.point_in_time_universe_verified is False
    assert result.extraction_completeness_proved_by_parser is False
    assert [(r["action"], r["symbol"], r["source_range"]) for r in result.review.changes] == [
        ("REMOVE", "600001.SH", "C4:D4"), ("ADD", "000001.SZ", "E4:F4")]
    assert result.worksheets == ({"sheet": "Sheet1", "header_range": "A1:F2", "data_rows": 2,
                                  "unique_index_codes": 2, "csi500_source_rows": (4,)},)


def test_two_slots_and_explicit_dashes_preserved_without_guessed_security():
    result = review_csi500_workbook_values(notice(), [sheet(wide=True)])
    assert len(result.review.changes) == 2
    assert [row["source_range"] for row in result.review.changes] == ["C3:D3", "G3:H3"]
    assert [(r["action"], r["source_range"]) for r in result.explicit_empty_target_slots] == [
        ("REMOVE", "E3:F3"), ("ADD", "I3:J3")]
    populated = deepcopy(MULTI)
    populated[4:6], populated[8:10] = ["600002", "旧样本2"], ["000002", "新样本2"]
    all_four = review_csi500_workbook_values(notice(), [sheet([populated], wide=True)])
    assert [r["action"] for r in all_four.review.changes] == ["REMOVE", "REMOVE", "ADD", "ADD"]
    assert len(all_four.explicit_empty_target_slots) == 0


def test_conditional_event_never_uses_publication_as_effective_date():
    result = review_csi500_workbook_values(notice("<p>自东兴证券、信达证券退市日起，对沪深300等指数调整。</p>"), [sheet()])
    assert result.review.conditional_effective_clause == "自东兴证券、信达证券退市日起"
    assert result.review.explicit_start_date is result.review.explicit_after_close_date is None
    assert result.review.status == "EFFECTIVE_DATE_REVIEW_REQUIRED"


@pytest.mark.parametrize("text,field", [("2026年9月11日收市后生效", "explicit_after_close_date"),
                                      ("自2026年9月14日起生效", "explicit_start_date")])
def test_explicit_date_semantics_retained_not_mapped_to_calendar(text, field):
    result = review_csi500_workbook_values(notice("<p>" + text + "</p>"), [sheet()])
    assert getattr(result.review, field) in ("2026-09-11", "2026-09-14")
    assert result.review.status == "PARSED_SOURCE_CANDIDATE_NOT_PIT"
    assert result.review.point_in_time_universe_verified is False


def test_other_index_and_csi500_subindex_are_not_target():
    subindex = ["H30012", "中证500子指数", "600003", "旧样本", "000003", "新样本"]
    result = review_csi500_workbook_values(notice(), [sheet([OTHER, subindex])])
    assert result.review.status == "NO_CSI500_ENTRIES_IN_SUPPORTED_TABLES" and not result.review.changes
    assert result.worksheets[0]["data_rows"] == 2


@pytest.mark.parametrize("column,value", [(0, 905), (0, "905"), (0, "000905 "), (0, "０００９０５"),
                                          (1, "其他指数"), (2, 600001), (2, "1"), (2, "-"),
                                          (3, "-"), (3, None), (4, "000001.SZ"), (5, "")])
def test_malformed_target_row_rejected_without_padding_or_filling(column, value):
    cells = deepcopy(PAIR)
    cells[column] = value
    with pytest.raises(ProviderError):
        review_csi500_workbook_values(notice(), [sheet([cells])])


@pytest.mark.parametrize("mutation", ["direction", "second-header", "extra-column", "missing-row-cell", "unknown-sheet",
                                     "blank-index", "numeric-other-index", "duplicate-sheet", "empty-target"])
def test_unknown_layout_or_other_row_cannot_be_negative_evidence(mutation):
    value = sheet([OTHER])
    if mutation == "direction":
        value["values"][0][2:6] = ["调入", None, "调出", None]
    elif mutation == "second-header":
        value["values"][1][2] = "股票"
    elif mutation == "extra-column":
        value["values"][2].append("注释")
    elif mutation == "missing-row-cell":
        value["values"][2][3] = None
    elif mutation == "unknown-sheet":
        value["sheet"] = "备选名单"
    elif mutation == "blank-index":
        value["values"][2][0] = ""
    elif mutation == "numeric-other-index":
        value["values"][2][0] = 300
    elif mutation == "empty-target":
        value["values"][2] = ["000905", "中证500", "-", "-", "-", "-"]
    sheets = [value, deepcopy(value)] if mutation == "duplicate-sheet" else [value]
    with pytest.raises(ProviderError):
        review_csi500_workbook_values(notice(), sheets)


@pytest.mark.parametrize("rows", [[PAIR, PAIR], [PAIR, ["000905", "中证500", "000001", "重复", "000002", "新"]]])
def test_duplicate_or_overlapping_target_symbols_fail(rows):
    with pytest.raises(ProviderError, match="duplicate or overlapping"):
        review_csi500_workbook_values(notice(), [sheet(rows)])


def test_explicit_count_checked_but_never_invented():
    assert review_csi500_workbook_values(notice(), [sheet()]).review.stated_change_count is None
    with pytest.raises(ProviderError, match="stated"):
        review_csi500_workbook_values(notice("<p>中证500指数更换2只样本。</p>"), [sheet()])


@pytest.mark.parametrize("sheets", [None, [], [None], [{"sheet": "Sheet1", "values": []}],
                                    [{"sheet": "Sheet1", "values": SIX}], [sheet()] * 21],
                         ids=["absent", "empty", "null-sheet", "no-values", "header-only", "too-many"])
def test_invalid_or_incomplete_extraction_fail(sheets):
    with pytest.raises(ProviderError):
        review_csi500_workbook_values(notice(), sheets)


def test_input_cells_and_notice_are_not_mutated_and_all_supported_sheets_read():
    source, n = [sheet([OTHER]), sheet(wide=True)], notice()
    snapshot = deepcopy((source, n))
    result = review_csi500_workbook_values(n, source)
    assert len(result.worksheets) == 2 and len(result.review.changes) == 2
    assert (source, n) == snapshot
