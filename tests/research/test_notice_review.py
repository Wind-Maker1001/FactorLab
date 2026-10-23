"""Artificial HTML contracts only; these tests do not verify real membership."""

import pytest

from factorlab.errors import ProviderError
from factorlab.universe.notice_review import mentions_csi500, review_csi500_notice, visible_html_text


def table(rows=None, heading="中证500指数样本调整名单："):
    rows = rows or [("000001", "人工调出甲", "600001", "人工调入甲"),
                    ("688001", "人工调出乙", "000002", "人工调入乙")]
    return ("<p>" + heading + "</p><table><tbody>"
            '<tr><td colspan="2"><p>调出名单</p></td><td colspan="2"><p>调入名单</p></td></tr>'
            "<tr><td>证券代码</td><td>证券名称</td><td>证券代码</td><td>证券名称</td></tr>" +
            "".join("<tr>" + "".join("<td><p>%s</p></td>" % cell for cell in row) + "</tr>" for row in rows) +
            "</tbody></table>")


def notice(content=None, count=2, effective="2021年12月10日收市后", **changes):
    row = {"id": 1, "publishDate": "2021-11-26", "title": "人工公告，非真实市场证据",
           "content": ("<p>中证500指数更换%d只样本，自%s生效。</p>" % (count, effective)) +
                      (table() if content is None else content)}
    row.update(changes)
    return row


def test_exact_directions_codes_rows_and_after_close_date_are_only_candidates():
    result = review_csi500_notice(notice())
    assert result.notice_id == 1 and result.publish_date == "2021-11-26"
    assert result.explicit_after_close_date == "2021-12-10" and result.stated_change_count == 2
    assert result.status == "PARSED_SOURCE_CANDIDATE_NOT_PIT"
    assert [(row["action"], row["symbol"]) for row in result.changes] == [
        ("REMOVE", "000001.SZ"), ("ADD", "600001.SH"), ("REMOVE", "688001.SH"), ("ADD", "000002.SZ")]
    assert [row["source_table_row"] for row in result.changes] == [3, 3, 4, 4]
    assert result.point_in_time_universe_verified is False
    assert not hasattr(result, "get_members") and not hasattr(result, "effective_trading_date")


def test_split_inline_nodes_entities_and_script_style_do_not_change_discovery():
    value = '<style>fake</style><p>中证<font>500</font>&nbsp;指数</p><script>ignore me</script>'
    assert visible_html_text(value) == "中证500\xa0指数"
    assert mentions_csi500(value)
    assert not mentions_csi500("<script>中证500</script><style>中证500</style><p>沪深300</p>")
    assert mentions_csi500("中证500行业子指数")  # discovery is deliberately not a membership assertion


def test_split_heading_count_and_date_inline_nodes():
    value = notice()
    value["content"] = value["content"].replace("中证500", "中证<font>500</font>").replace(
        "更换2只", "更换<font>2</font>只").replace("12月10", "<font>12</font>月<font>10</font>")
    assert len(review_csi500_notice(value).changes) == 4


def test_backup_and_other_index_tables_are_excluded():
    result = review_csi500_notice(notice(table(heading="沪深300指数样本调整名单：") + table() +
                                       table(heading="中证500指数备选名单：")))
    assert len(result.changes) == 4


@pytest.mark.parametrize("content", ["<p>仅正文、另有附件</p>", table(heading="中证500指数备选名单："),
                                     table(heading="中证500行业指数样本调整名单："), "\n \t<table></table>"])
def test_missing_exact_table_requires_review_not_a_successful_empty_universe(content):
    result = review_csi500_notice(notice(content))
    assert result.status == "ATTACHMENT_OR_LAYOUT_REVIEW_REQUIRED" and result.changes == ()
    assert result.explicit_after_close_date == "2021-12-10"
    assert result.point_in_time_universe_verified is False


def test_no_effective_date_never_uses_publication_or_next_trading_day():
    value = notice(effective="相关证券退市日")
    result = review_csi500_notice(value)
    assert result.explicit_after_close_date is None and result.status == "EFFECTIVE_DATE_REVIEW_REQUIRED"
    assert result.publish_date == "2021-11-26" and len(result.changes) == 4


@pytest.mark.parametrize("ending", ["收盘后", "收市后"])
def test_two_explicit_after_close_spellings(ending):
    assert review_csi500_notice(notice(effective="2021年12月10日" + ending)).explicit_after_close_date == "2021-12-10"


@pytest.mark.parametrize("extra", ["<p>另有2021年12月13日收市后生效</p>",
                                 "<p>中证500指数更换3只样本</p>"])
def test_conflicting_dates_or_counts_require_independent_review(extra):
    with pytest.raises(ProviderError):
        review_csi500_notice(notice(table() + extra))


def test_repeated_same_date_and_count_do_not_create_ambiguity():
    assert len(review_csi500_notice(notice(table() + "<p>2021年12月10日收盘后，中证500指数更换2只</p>")).changes) == 4


@pytest.mark.parametrize("effective", ["2021年11月25日收市后", "2021年2月30日收盘后"])
def test_invalid_or_retroactive_date_rejected(effective):
    with pytest.raises(ProviderError):
        review_csi500_notice(notice(effective=effective))


@pytest.mark.parametrize("field,value", [("id", True), ("id", 0), ("id", "1"), ("publishDate", None),
                                        ("publishDate", "2021-11-2"), ("publishDate", "2021-02-30"),
                                        ("content", None), ("content", [])])
def test_invalid_identity_date_or_content_rejected(field, value):
    row = notice()
    row[field] = value
    with pytest.raises(ProviderError):
        review_csi500_notice(row)


@pytest.mark.parametrize("value", [None, [], "html" * (1024 * 1024)], ids=["null", "list", "oversized"])
def test_invalid_notice_or_oversized_content(value):
    with pytest.raises(ProviderError):
        review_csi500_notice(value if not isinstance(value, str) else notice(content=value))


@pytest.mark.parametrize("count", [0, 1, 3, 501])
def test_explicit_count_must_be_plausible_and_match_both_directions(count):
    with pytest.raises(ProviderError):
        review_csi500_notice(notice(count=count))


def test_no_explicit_count_does_not_assume_table_length():
    row = notice()
    row["content"] = row["content"].replace("中证500指数更换2只", "中证500指数调整")
    with pytest.raises(ProviderError, match="explicit stated"):
        review_csi500_notice(row)


@pytest.mark.parametrize("old,new", [("调出名单", "调入名单"), ("证券代码", "股票名称"),
                                     ('colspan="2"', 'colspan="1"'), ("000001", "1"),
                                     ("000001", "000001.SZ"), ("人工调入甲", ""),
                                     ("000001", "600001"), ("688001", "000001")])
def test_wrong_header_direction_identifier_missing_duplicate_or_overlap(old, new):
    with pytest.raises(ProviderError):
        review_csi500_notice(notice(table().replace(old, new)))


def test_multiple_exact_tables_are_not_merged():
    with pytest.raises(ProviderError, match="multiple CSI500"):
        review_csi500_notice(notice(table() + table()))


@pytest.mark.parametrize("old,new", [("<tbody>", "<tbody><table></table>"),
                                     ('colspan="2"', 'rowspan="2"'),
                                     ('colspan="2"', 'colspan="7"'),
                                     ("</table>", ""), ("</tr>", ""), ("</td>", "")])
def test_unsupported_or_unclosed_table_never_silently_recovers(old, new):
    with pytest.raises(ProviderError):
        review_csi500_notice(notice(table().replace(old, new)))


def test_plain_decoded_heading_not_reparsed_as_markup():
    content = '<p>不相关&lt;文字&gt;</p>\n' + table()
    assert len(review_csi500_notice(notice(content)).changes) == 4


def test_explicit_start_date_not_reinterpreted_as_after_close_or_publication():
    row = notice(effective="2021年12月13日起")
    result = review_csi500_notice(row)
    assert result.explicit_start_date == "2021-12-13"
    assert result.explicit_after_close_date is None and result.conditional_effective_clause is None
    assert result.publish_date == "2021-11-26" and result.point_in_time_universe_verified is False


def test_conditional_listing_day_retains_source_clause_without_assigning_a_date():
    row = notice(effective="人工公司上市日起")
    result = review_csi500_notice(row)
    assert result.conditional_effective_clause == "自人工公司上市日起"
    assert result.explicit_start_date is None and result.explicit_after_close_date is None
    assert result.status == "EFFECTIVE_DATE_REVIEW_REQUIRED"


def test_conditional_delisting_day_is_not_publication_date():
    result = review_csi500_notice(notice(effective="相关证券退市之日起"))
    assert result.conditional_effective_clause == "自相关证券退市之日起"
    assert result.explicit_start_date is None and result.explicit_after_close_date is None


def test_two_date_semantics_are_retained_but_not_resolved_without_calendar():
    row = notice(table() + "<p>自2021年12月13日起进行调整。</p>")
    result = review_csi500_notice(row)
    assert result.explicit_after_close_date == "2021-12-10" and result.explicit_start_date == "2021-12-13"
    assert result.status == "EFFECTIVE_DATE_REVIEW_REQUIRED"


def test_conditional_event_not_erased_by_another_explicit_source_date():
    row = notice(table() + "<p>自人工公司上市日起进行调整。</p>")
    result = review_csi500_notice(row)
    assert result.explicit_after_close_date == "2021-12-10" and result.conditional_effective_clause
    assert result.status == "EFFECTIVE_DATE_REVIEW_REQUIRED"


@pytest.mark.parametrize("date_text", ["2021年2月30日", "2021年11月25日"])
def test_invalid_or_retroactive_start_date_requires_review(date_text):
    with pytest.raises(ProviderError):
        review_csi500_notice(notice(effective=date_text + "起"))


def test_multiple_start_dates_not_silently_collapsed():
    row = notice(effective="2021年12月13日起")
    row["content"] += "<p>自2021年12月14日起进行其他调整。</p>"
    with pytest.raises(ProviderError, match="multiple explicit start"):
        review_csi500_notice(row)
