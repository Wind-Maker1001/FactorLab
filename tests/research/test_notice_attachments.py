"""Synthetic HTML/metadata discovery, never historical constituent validation."""

import pytest

from qbot.research.errors import ProviderError
from qbot.research.universe.notice_attachments import body_file_links, discover_notice_attachments


BASE = "https://www.csindex.com.cn/#/about/newsDetail?id=1"
OLD = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20210610/1623319091660435.xlsx"


def notice(content, enclosures=None):
    return {"id": 1, "content": content, "enclosureList": enclosures or []}


def test_empty_api_enclosures_do_not_hide_real_body_file_links():
    files = discover_notice_attachments(notice('<p>人工附件：<a href="%s"><span>调整名单</span></a></p>' % OLD))
    assert len(files) == 1 and files[0]["fileUrl"] == OLD and files[0]["createAt"] is None
    assert files[0]["fileName"] == "调整名单"
    assert files[0]["discovery_sources"][0]["kind"] == "body_href"
    assert files[0]["discovery_sources"][0]["source_link_ordinal"] == 1


def test_invisible_label_uses_actual_title_attribute_without_dropping_the_link():
    row = body_file_links('<a title="原文标题" href="%s"><span></span></a>' % OLD, BASE)[0]
    assert row["label"] == "原文标题" and row["label_from_title"] is True and row["visible_label_present"] is False


def test_file_without_label_retained_without_invented_visible_text():
    row = body_file_links('<a href="%s"></a>' % OLD, BASE)[0]
    assert row["label"] == "" and row["label_from_title"] is False and row["visible_label_present"] is False


def test_metadata_and_same_encoded_body_request_alias_merge_with_both_origins():
    raw = "https://oss-ch.csindex.com.cn/notice/调整 名单.xlsx"
    encoded = "https://oss-ch.csindex.com.cn/notice/%E8%B0%83%E6%95%B4%20%E5%90%8D%E5%8D%95.xlsx"
    entry = {"fileUrl": raw, "fileName": "字段名", "createAt": "2021-06-10 09:00:00"}
    files = discover_notice_attachments(notice('<a href="%s">正文名</a>' % encoded, [entry]))
    assert len(files) == 1 and files[0]["fileUrl"] == raw
    assert files[0]["fileName"] == "字段名" and files[0]["createAt"] == entry["createAt"]
    assert [row["kind"] for row in files[0]["discovery_sources"]] == ["enclosureList", "body_href"]
    assert files[0]["discovery_sources"][1]["href"] == encoded


def test_distinct_dated_revisions_are_not_merged_or_selected_by_date():
    earlier = OLD.replace("20210610/1623319091660435", "20210601/1622509386248149")
    files = discover_notice_attachments(notice('<a href="%s">相同名单名</a><a href="%s">相同名单名</a>' % (OLD, earlier)))
    assert [row["fileUrl"] for row in files] == [OLD, earlier]
    assert all(row["createAt"] is None for row in files)  # URL directory is not a publisher timestamp


def test_encoded_and_literal_slash_requests_not_assumed_to_be_same_resource():
    a = "https://oss-ch.csindex.com.cn/notice/file.xlsx"
    b = "https://oss-ch.csindex.com.cn/notice%2Ffile.xlsx"
    assert len(discover_notice_attachments(notice('<a href="%s">a</a><a href="%s">b</a>' % (a, b)))) == 2


def test_same_href_repeat_keeps_all_source_positions_but_one_file():
    files = discover_notice_attachments(notice('<a href="%s">a</a><a href="%s">b</a>' % (OLD, OLD)))
    assert len(files) == 1 and [row["source_link_ordinal"] for row in files[0]["discovery_sources"]] == [1, 2]


def test_non_file_web_links_not_classified_and_original_anchor_ordinal_kept():
    links = body_file_links('<a href="http://www.csindex.com.cn">官网</a><a href="%s">名单</a>' % OLD, BASE)
    assert len(links) == 1 and links[0]["source_link_ordinal"] == 2


def test_relative_resolution_uses_actual_spa_page_base_not_api_directory():
    row = body_file_links('<a href="/notice/artificial.xlsx">文件</a>', BASE)[0]
    assert row["href"] == "/notice/artificial.xlsx" and row["resolved_url"] == "https://www.csindex.com.cn/notice/artificial.xlsx"


def test_discovery_is_not_a_download_authorization_for_external_links():
    row = body_file_links('<a href="https://unverified.invalid/file.xlsx">文件</a>', BASE)[0]
    assert row["resolved_url"].startswith("https://unverified.invalid")
    assert "point_in_time_universe_verified" not in row


def test_script_style_data_not_a_file_href_and_html_entities_decoded_once():
    html = '<script><a href="fake.xlsx">fake</a></script><style>fake</style><a href="%s?a=1&amp;b=2">人&amp;工</a>' % OLD
    rows = body_file_links(html, BASE)
    assert len(rows) == 1 and rows[0]["href"].endswith("?a=1&b=2") and rows[0]["label"] == "人&工"


@pytest.mark.parametrize("extension", ["XLSX", "xls", "pdf", "zip", "csv"])
def test_literal_file_extensions_discovered_without_inventing_paths(extension):
    url = "https://oss-ch.csindex.com.cn/notice/fixture." + extension
    assert body_file_links('<a href="%s">fixture</a>' % url, BASE)[0]["resolved_url"] == url


@pytest.mark.parametrize("html", ['<a href="a.xlsx"><a href="b.xlsx">nested</a></a>',
                                 '<a href="a.xlsx" href="b.xlsx">ambiguous</a>', '<a href="a.xlsx">unclosed'])
def test_ambiguous_or_unclosed_anchor_fails_instead_of_guessing(html):
    with pytest.raises(ProviderError):
        body_file_links(html, BASE)


@pytest.mark.parametrize("html", [None, [], "x" * (2 * 1024 * 1024 + 1)], ids=["null", "list", "oversized"])
def test_invalid_or_oversized_html_rejected(html):
    with pytest.raises(ProviderError):
        body_file_links(html, BASE)


@pytest.mark.parametrize("value", [None, [], {"id": True, "enclosureList": []},
                                 {"id": 1, "enclosureList": None},
                                 {"id": 1, "enclosureList": [None], "content": ""}])
def test_invalid_identity_or_enclosures_rejected(value):
    with pytest.raises(ProviderError):
        discover_notice_attachments(value)
