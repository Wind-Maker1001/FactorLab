"""Artificial containers and network boundaries; no real PIT claim."""

from dataclasses import replace
import hashlib
import io
import json
import socket
from pathlib import Path
import urllib.error

import pytest

from examples.research import collect_exchange_article, collect_official_notices, review_browser_notices as example
from qbot.research.config import PathsConfig, load_config
from qbot.research.errors import ProviderError
from qbot.research.universe.browser_observations import REPRESENTATION, TRANSPORT, read_browser_notice
from qbot.research.universe.official_sources import DETAIL_URL, OfficialNoticeSource
from tests.research.test_notice_review import notice as synthetic_notice


def container(notice_id=1, content=None, enclosure=True):
    row = synthetic_notice() if content is None else synthetic_notice(content=content)
    row["id"] = notice_id
    row["enclosureList"] = [{"noticeId": notice_id, "fileName": "ARTIFICIAL.xlsx",
                             "fileUrl": "https://oss-ch.csindex.com.cn/notice/artificial-%d.xlsx" % notice_id}] if enclosure else []
    return {"schema_version": 1, "source_url": DETAIL_URL + "?id=%d" % notice_id,
            "http_status": 200, "transport": TRANSPORT, "observed_at": "2022-01-10 09:00:00 UTC",
            "response_text": json.dumps({"success": True, "code": "200", "data": row}, ensure_ascii=False),
            "representation": REPRESENTATION, "point_in_time_universe_verified": False}


def write(directory, payload, name="1.observation.json"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def test_exact_container_and_decoded_text_hashes_and_non_authenticated_capture(tmp_path):
    payload = container()
    path = write(tmp_path, payload)
    item = read_browser_notice(path)
    assert item.container_bytes == path.read_bytes()
    assert item.container_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert item.response_text_sha256 == hashlib.sha256(payload["response_text"].encode("utf-8")).hexdigest()
    assert item.container_sha256 != item.response_text_sha256
    assert item.observed_at == payload["observed_at"] and item.observed_at_utc == "2022-01-10T09:00:00+00:00"
    assert item.notice["id"] == 1
    evidence = item.evidence()
    assert evidence["external_timestamp_proof"] is False and evidence["external_origin_authentication_proof"] is False
    assert evidence["point_in_time_universe_verified"] is False
    assert evidence["transport_label_supplied_by_observation"] == TRANSPORT
    assert not hasattr(item, "get_members")


def test_semantically_equal_json_has_different_byte_binding(tmp_path):
    first = container()
    second = {**first, "response_text": " \n" + first["response_text"]}
    a = read_browser_notice(write(tmp_path, first, "a.observation.json"))
    b = read_browser_notice(write(tmp_path, second, "b.observation.json"))
    assert a.notice == b.notice
    assert a.container_sha256 != b.container_sha256 and a.response_text_sha256 != b.response_text_sha256


@pytest.mark.parametrize("field,value", [("schema_version", True), ("schema_version", 2), ("http_status", True),
                                        ("http_status", 403), ("transport", "urllib"),
                                        ("representation", "original compressed wire bytes"),
                                        ("point_in_time_universe_verified", True),
                                        ("point_in_time_universe_verified", 0), ("response_text", None)])
def test_bad_schema_or_unearned_provenance_rejected(tmp_path, field, value):
    payload = container()
    payload[field] = value
    with pytest.raises(ProviderError):
        read_browser_notice(write(tmp_path, payload))


@pytest.mark.parametrize("operation", ["extra", "missing"])
def test_container_unknown_or_missing_fields_rejected(tmp_path, operation):
    payload = container()
    if operation == "extra":
        payload["verified"] = True
    else:
        del payload["observed_at"]
    with pytest.raises(ProviderError, match="schema"):
        read_browser_notice(write(tmp_path, payload))


@pytest.mark.parametrize("url", [DETAIL_URL + "?id=01", DETAIL_URL + "?id=1&date=2021",
                                DETAIL_URL + "?id=1#ignored", DETAIL_URL.replace("www.", "evil.") + "?id=1",
                                DETAIL_URL.replace("https://", "http://") + "?id=1", "file:///notice/1", None])
def test_observed_url_must_be_exact_public_identity(tmp_path, url):
    with pytest.raises(ProviderError, match="exact public"):
        read_browser_notice(write(tmp_path, {**container(), "source_url": url}))


@pytest.mark.parametrize("timestamp", [None, "2022-01-10", "2022-01-10T09:00:00",
                                      "2022-01-10T09:00:00+08:00", "2022-02-30 09:00:00 UTC",
                                      "2999-01-10T09:00:00+00:00"])
def test_invalid_naive_non_utc_or_future_timestamp_rejected(tmp_path, timestamp):
    with pytest.raises(ProviderError):
        read_browser_notice(write(tmp_path, {**container(), "observed_at": timestamp}))


def test_explicit_iso_utc_capture_preserves_original_representation(tmp_path):
    value = "2022-01-10T09:00:00+00:00"
    assert read_browser_notice(write(tmp_path, {**container(), "observed_at": value})).observed_at == value


def test_standard_browser_utc_z_timestamp_supported_on_python39(tmp_path):
    value = "2022-01-10T09:00:00.987Z"
    item = read_browser_notice(write(tmp_path, {**container(), "observed_at": value}))
    assert item.observed_at == value and item.observed_at_utc == "2022-01-10T09:00:00.987000+00:00"


@pytest.mark.parametrize("field,value", [("id", 2), ("publishDate", "2022-02-10"), ("content", None),
                                        ("title", ""), ("enclosureList", None),
                                        ("enclosureList", [{"noticeId": 2, "fileName": "x", "fileUrl": "https://oss-ch.csindex.com.cn/notice/x.xlsx"}])])
def test_response_identity_publication_content_and_enclosures_rejected(tmp_path, field, value):
    payload = container()
    decoded = json.loads(payload["response_text"])
    decoded["data"][field] = value
    payload["response_text"] = json.dumps(decoded)
    with pytest.raises(ProviderError):
        read_browser_notice(write(tmp_path, payload))


@pytest.mark.parametrize("response", ["not json", "[]", '{"success":true,"code":200,"data":{}}',
                                     '{"success":false,"code":"200","data":{}}'])
def test_invalid_response_contract_rejected(tmp_path, response):
    with pytest.raises(ProviderError):
        read_browser_notice(write(tmp_path, {**container(), "response_text": response}))


@pytest.mark.parametrize("encoding", ["utf-16", "invalid"])
def test_container_requires_utf8_not_json_auto_detected_encoding(tmp_path, encoding):
    path = tmp_path / "1.observation.json"
    path.write_bytes(json.dumps(container()).encode("utf-16") if encoding == "utf-16" else b"\xff")
    with pytest.raises(ProviderError, match="UTF-8"):
        read_browser_notice(path)


def test_duplicate_container_keys_rejected_before_identity_selection(tmp_path):
    path = tmp_path / "1.observation.json"
    path.write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
    with pytest.raises(ProviderError, match="duplicate key"):
        read_browser_notice(path)


def test_duplicate_response_keys_rejected(tmp_path):
    payload = {**container(), "response_text": '{"success":true,"success":false}'}
    with pytest.raises(ProviderError, match="duplicate key"):
        read_browser_notice(write(tmp_path, payload))


def test_missing_or_symlink_container_is_not_consumed(tmp_path, monkeypatch):
    with pytest.raises(ProviderError, match="regular"):
        read_browser_notice(tmp_path / "absent")
    path = write(tmp_path, container())
    monkeypatch.setattr(Path, "is_symlink", lambda self: True)
    with pytest.raises(ProviderError, match="non-symlink"):
        read_browser_notice(path)


@pytest.mark.parametrize("kind", ["container", "response"])
def test_size_bounds(tmp_path, kind):
    path = tmp_path / "1.observation.json"
    if kind == "container":
        path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    else:
        write(tmp_path, {**container(), "response_text": " " * (2 * 1024 * 1024 + 1)})
    with pytest.raises(ProviderError, match="oversized|bounded"):
        read_browser_notice(path)


@pytest.fixture
def cli(tmp_path, monkeypatch):
    original = load_config("configs/research/csi500_smoke.yaml")
    config = replace(original, paths=PathsConfig(data_root=tmp_path / "data", results_root=tmp_path / "results"))
    object.__setattr__(config, "repo_root", original.repo_root)
    object.__setattr__(config, "config_path", original.config_path)
    monkeypatch.setattr(example, "load_config", lambda path: config)
    monkeypatch.setattr(collect_exchange_article, "load_config", lambda path: config)
    monkeypatch.setattr(collect_official_notices, "load_config", lambda path: config)
    directory = tmp_path / "observations"
    directory.mkdir()
    return config, directory


def latest_manifest(config):
    paths = sorted(config.paths.runs_dir.glob("*/manifest.json"))
    return json.loads(paths[-1].read_text(encoding="utf-8"))


def test_offline_cli_uses_real_review_bindings_and_never_instantiates_http(cli, monkeypatch):
    config, directory = cli
    original = write(directory, container())
    write(directory, container(2, content="<p>仅附件，非内嵌名单</p>"), "2.observation.json")
    def forbidden(*args, **kwargs):
        raise AssertionError("default review must not create an HTTP client")
    monkeypatch.setattr(example, "OfficialNoticeSource", forbidden)
    assert example.main(["--input-directory", str(directory)]) == 0
    run = latest_manifest(config)
    assert run["status"] == "success" and run["candidate_change_events"] == 4
    assert run["observation_count"] == 2 and run["inline_candidate_tables"] == 1
    assert run["manual_review_required_notices"] == [2]
    assert run["network_requests"] == run["browser_requests_performed_by_this_run"] == 0
    assert run["point_in_time_universe_verified"] is run["historical_constituents_generated"] is False
    snapshot = Path(run["input_evidence"][0]["snapshot_path"])
    assert snapshot.read_bytes() == original.read_bytes()
    assert run["input_evidence"][0]["observation_sha256"] == hashlib.sha256(snapshot.read_bytes()).hexdigest()
    for artifact in run["output_artifacts"].values():
        assert artifact["sha256"] == hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest()
    summary = json.loads(Path(run["outputs"]["review"]).read_text(encoding="utf-8"))
    assert all(row["status"] == "DISCOVERED_NOT_DOWNLOADED" for row in summary["attachments"])
    assert not config.paths.universe_dir.exists() and not config.paths.processed_dir.exists()


def test_rerun_gets_new_run_and_preserves_old_snapshots(cli):
    config, directory = cli
    path = write(directory, container())
    assert example.main(["--input-directory", str(directory)]) == 0
    before = latest_manifest(config)
    old_snapshot = Path(before["input_evidence"][0]["snapshot_path"])
    old_bytes = old_snapshot.read_bytes()
    value = container()
    value["response_text"] += "\n"
    write(directory, value)
    assert example.main(["--input-directory", str(directory)]) == 0
    after = latest_manifest(config)
    assert before["run_id"] != after["run_id"]
    assert old_snapshot.read_bytes() == old_bytes and old_snapshot.read_bytes() != path.read_bytes()


@pytest.mark.parametrize("condition", ["empty", "malformed", "duplicate", "invalid-second"])
def test_bad_inputs_have_terminal_manifest_and_no_http(cli, monkeypatch, condition):
    config, directory = cli
    if condition != "empty":
        write(directory, container())
        if condition == "duplicate":
            write(directory, container(), "duplicate.observation.json")
        elif condition == "invalid-second":
            write(directory, {**container(2), "transport": "invented"}, "2.observation.json")
        else:
            (directory / "1.observation.json").write_text("not json", encoding="utf-8")
    def forbidden(*args, **kwargs):
        raise AssertionError("all source observations must be validated before HTTP")
    monkeypatch.setattr(example, "OfficialNoticeSource", forbidden)
    assert example.main(["--input-directory", str(directory), "--download-attachments"]) == 1
    run = latest_manifest(config)
    assert run["status"] == "failed" and run["completed_at"]
    assert run["network_requests"] == 0
    assert not config.paths.universe_dir.exists()


def test_bad_inline_table_is_partial_not_silently_valid_zero(cli):
    config, directory = cli
    payload = container()
    payload["response_text"] = payload["response_text"].replace("更换2只", "更换3只")
    write(directory, payload)
    assert example.main(["--input-directory", str(directory)]) == 1
    run = latest_manifest(config)
    assert run["status"] == "partial" and run["candidate_change_events"] == 0
    summary = json.loads(Path(run["outputs"]["review"]).read_text(encoding="utf-8"))
    assert summary["notices"][0]["review"]["status"] == "REVIEW_FAILED"


class Response(io.BytesIO):
    status = 200
    headers = {}
    def __init__(self, url, content):
        super().__init__(content)
        self.url = url
    def geturl(self):
        return self.url


def http_boundary(monkeypatch, target, replies):
    calls = []
    def opener(request, timeout):
        calls.append(request.full_url)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Response(request.full_url, reply)
    def factory(root, **kwargs):
        return OfficialNoticeSource(root, opener=opener, sleeper=lambda value: None, **kwargs)
    monkeypatch.setattr(target, "OfficialNoticeSource", factory)
    return calls


def test_attachment_cli_archives_with_separate_http_receipt_then_cache_only(cli, monkeypatch):
    config, directory = cli
    write(directory, container())
    archive = config.paths.data_root / "source-archive"
    calls = http_boundary(monkeypatch, example, [b"PK ARTIFICIAL NOT A REAL WORKBOOK"])
    args = ["--input-directory", str(directory), "--download-attachments", "--archive-root", str(archive)]
    assert example.main(args) == 0
    run = latest_manifest(config)
    assert run["network_requests"] == 1 and run["verified_cache_reads"] == 0
    assert run["browser_requests_performed_by_this_run"] == 0
    assert example.main(args + ["--cache-only"]) == 0
    run = latest_manifest(config)
    assert len(calls) == 1 and run["network_requests"] == 0 and run["verified_cache_reads"] == 1
    record = json.loads(Path(run["outputs"]["review"]).read_text(encoding="utf-8"))["attachments"][0]
    assert record["status"] == "ARCHIVED_NOT_INTERPRETED"
    assert record["source_evidence"]["request"]["method"] == "GET"
    assert "transport_label_supplied_by_observation" not in record["source_evidence"]


def test_attachment_denial_stops_batch_and_preserves_failure_and_skip(cli, monkeypatch):
    config, directory = cli
    write(directory, container())
    write(directory, container(2), "2.observation.json")
    calls = http_boundary(monkeypatch, example, [urllib.error.HTTPError("u", 403, "denied", {}, None)])
    assert example.main(["--input-directory", str(directory), "--download-attachments",
                         "--archive-root", str(config.paths.data_root / "archive")]) == 1
    run = latest_manifest(config)
    assert len(calls) == run["network_requests"] == 1 and run["status"] == "partial"
    assert [row["skipped_after_site_denial"] for row in run["failures"]["source_review"]] == [False, True]


def test_python39_socket_timeout_is_partial_and_next_attachment_is_processed(cli, monkeypatch):
    config, directory = cli
    write(directory, container())
    write(directory, container(2), "2.observation.json")
    calls = http_boundary(monkeypatch, example, [socket.timeout("actual read timeout"), b"PK ARTIFICIAL"])
    assert example.main(["--input-directory", str(directory), "--download-attachments",
                         "--archive-root", str(config.paths.data_root / "archive")]) == 1
    run = latest_manifest(config)
    assert run["status"] == "partial" and len(calls) == run["network_requests"] == 2
    assert run["attachments_archived_not_interpreted"] == 1
    assert run["failures"]["source_review"][0]["notice_id"] == 1


def test_generic_oserror_stays_fatal_with_terminal_counts(cli, monkeypatch):
    config, directory = cli
    write(directory, container())
    calls = http_boundary(monkeypatch, example, [OSError("not a diagnosed network error")])
    assert example.main(["--input-directory", str(directory), "--download-attachments",
                         "--archive-root", str(config.paths.data_root / "archive")]) == 1
    run = latest_manifest(config)
    assert run["status"] == "failed" and run["network_requests"] == len(calls) == 1


def test_missing_attachment_cache_has_no_network_fallback(cli, monkeypatch):
    config, directory = cli
    write(directory, container())
    calls = http_boundary(monkeypatch, example, [])
    assert example.main(["--input-directory", str(directory), "--download-attachments", "--cache-only",
                         "--archive-root", str(config.paths.data_root / "archive")]) == 1
    assert calls == [] and latest_manifest(config)["network_requests"] == 0


def test_cache_only_flag_without_attachment_mode_rejected(cli):
    _, directory = cli
    with pytest.raises(SystemExit):
        example.main(["--input-directory", str(directory), "--cache-only"])


def test_exchange_duplicate_template_title_not_joined_and_file_links_not_downloaded(cli, monkeypatch):
    config, _ = cli
    html = b'<!doctype html><html><title>ARTIFICIAL ARTICLE</title><title>NAV TITLE</title><a href="a.pdf">file</a></html>'
    calls = http_boundary(monkeypatch, collect_exchange_article, [html])
    assert collect_exchange_article.main(["--url", "https://www.sse.com.cn/market/sseindex/diclosure/c/c_20210528_5476672.shtml",
                                         "--archive-root", str(config.paths.data_root / "exchange")]) == 0
    run = latest_manifest(config)
    assert run["title"] == "ARTIFICIAL ARTICLE" and len(calls) == 1
    assert len(run["linked_files_not_downloaded"]) == 1
    assert run["point_in_time_universe_verified"] is False


def test_exchange_failure_keeps_actual_request_count(cli, monkeypatch):
    config, _ = cli
    calls = http_boundary(monkeypatch, collect_exchange_article, [socket.timeout("read timeout")])
    assert collect_exchange_article.main(["--url", "https://www.sse.com.cn/market/sseindex/diclosure/c/c_20210528_5476672.shtml",
                                         "--archive-root", str(config.paths.data_root / "exchange")]) == 1
    run = latest_manifest(config)
    assert run["status"] == "failed" and run["network_requests"] == len(calls) == 1


def test_catalogue_attachment_selection_handles_split_visible_csi500_text(cli, monkeypatch):
    config, _ = cli
    payload = container(content="<p>中证<font>500</font>相关调整</p>")
    detail = json.loads(payload["response_text"])
    listing = {key: detail["data"][key] for key in ("id", "title", "publishDate")}
    catalogue = {"success": True, "code": "200", "total": 1, "currentPage": 1, "pageSize": 100, "data": [listing]}
    calls = http_boundary(monkeypatch, collect_official_notices, [json.dumps(catalogue).encode("utf-8"),
                                                              payload["response_text"].encode("utf-8"), b"PK ARTIFICIAL"])
    assert collect_official_notices.main(["--archive-root", str(config.paths.data_root / "catalogue")]) == 0
    run = latest_manifest(config)
    assert len(calls) == run["network_requests"] == 3 and run["archived_attachments"] == 1
    assert run["csi500_mentioned_notices"] == 1 and run["point_in_time_universe_verified"] is False


def test_catalogue_fatal_error_keeps_actual_network_count(cli, monkeypatch):
    config, _ = cli
    calls = http_boundary(monkeypatch, collect_official_notices, [OSError("not a transient network error")])
    assert collect_official_notices.main(["--archive-root", str(config.paths.data_root / "catalogue")]) == 1
    run = latest_manifest(config)
    assert run["status"] == "failed" and run["network_requests"] == len(calls) == 1


def test_browser_review_empty_enclosure_list_discovers_and_archives_literal_body_link(cli, monkeypatch):
    config, directory = cli
    url = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20210610/1623319091660435.xlsx"
    write(directory, container(content='<p>自2021年12月13日起调整，附件<a href="%s">原名单</a></p>' % url, enclosure=False))
    calls = http_boundary(monkeypatch, example, [b"PK ARTIFICIAL"])
    assert example.main(["--input-directory", str(directory), "--download-attachments",
                         "--archive-root", str(config.paths.data_root / "archive")]) == 0
    run = latest_manifest(config)
    assert calls == [url] and run["discovered_attachments"] == run["attachments_archived_not_interpreted"] == 1
    summary = json.loads(Path(run["outputs"]["review"]).read_text(encoding="utf-8"))
    record = summary["attachments"][0]
    assert record["publisher_url"] == url and record["discovery_sources"][0]["kind"] == "body_href"
    assert record["source_evidence"]["request"]["contract"].startswith("csindex-legacy-static-notice")
    assert summary["notices"][0]["review"]["explicit_start_date"] == "2021-12-13"


def test_duplicate_metadata_and_body_link_only_one_attachment_request(cli, monkeypatch):
    config, directory = cli
    url = "https://oss-ch.csindex.com.cn/notice/artificial-1.xlsx"
    write(directory, container(content='<a href="%s">same</a>' % url))
    calls = http_boundary(monkeypatch, example, [b"PK ARTIFICIAL"])
    assert example.main(["--input-directory", str(directory), "--download-attachments",
                         "--archive-root", str(config.paths.data_root / "archive")]) == 0
    run = latest_manifest(config)
    record = json.loads(Path(run["outputs"]["review"]).read_text(encoding="utf-8"))["attachments"][0]
    assert len(calls) == run["discovered_attachments"] == 1 and len(record["discovery_sources"]) == 2


def test_unapproved_body_link_recorded_failed_without_any_http(cli, monkeypatch):
    config, directory = cli
    write(directory, container(content='<a href="https://unapproved.invalid/private.xlsx">file</a>', enclosure=False))
    calls = http_boundary(monkeypatch, example, [])
    assert example.main(["--input-directory", str(directory), "--download-attachments",
                         "--archive-root", str(config.paths.data_root / "archive")]) == 1
    run = latest_manifest(config)
    assert calls == [] and run["status"] == "partial" and run["network_requests"] == 0


def test_two_body_revisions_both_preserved_neither_implicitly_selected(cli, monkeypatch):
    config, directory = cli
    a = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20210610/1623319091660435.xlsx"
    b = a.replace("20210610/1623319091660435", "20210601/1622509386248149")
    write(directory, container(content='<a href="%s">名单</a><a href="%s">名单</a>' % (a, b), enclosure=False))
    calls = http_boundary(monkeypatch, example, [b"PK ARTIFICIAL A", b"PK ARTIFICIAL B"])
    assert example.main(["--input-directory", str(directory), "--download-attachments",
                         "--archive-root", str(config.paths.data_root / "archive")]) == 0
    run = latest_manifest(config)
    assert calls == [a, b] and run["discovered_attachments"] == 2 and run["point_in_time_universe_verified"] is False


def test_catalogue_collector_also_finds_body_file_when_metadata_list_empty(cli, monkeypatch):
    config, _ = cli
    url = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20210610/1623319091660435.xlsx"
    payload = container(content='<p>中证500<a href="%s">名单</a></p>' % url, enclosure=False)
    detail = json.loads(payload["response_text"])
    listing = {key: detail["data"][key] for key in ("id", "title", "publishDate")}
    catalogue = {"success": True, "code": "200", "total": 1, "currentPage": 1, "pageSize": 100, "data": [listing]}
    calls = http_boundary(monkeypatch, collect_official_notices, [json.dumps(catalogue).encode("utf-8"),
                                                              payload["response_text"].encode("utf-8"), b"PK ARTIFICIAL"])
    assert collect_official_notices.main(["--archive-root", str(config.paths.data_root / "catalogue")]) == 0
    run = latest_manifest(config)
    assert calls[-1] == url and run["archived_attachments"] == 1
