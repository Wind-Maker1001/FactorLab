"""Offline workflow with artificial ZIP metadata and simulated extracted cells."""

from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import socket
from zipfile import ZipFile

import pytest

from examples.research import review_cached_workbook_notices as example
from factorlab.config import PathsConfig, load_config
from factorlab.errors import ProviderError
from factorlab.universe.official_sources import OfficialNoticeSource
from tests.research.test_notice_workbook import OTHER, sheet
from tests.research.test_official_sources import Response, detail


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def workbook(names=("Sheet1",), metadata=None):
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("xl/workbook.xml", metadata or ('<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                         '<sheets>' + ''.join('<sheet name="%s"/>' % name for name in names) + '</sheets></workbook>'))
    return stream.getvalue()


@pytest.fixture
def cli(tmp_path, monkeypatch):
    original = load_config("configs/research/csi500_smoke.yaml")
    config = replace(original, paths=PathsConfig(data_root=tmp_path / "data", results_root=tmp_path / "results"))
    object.__setattr__(config, "repo_root", original.repo_root)
    object.__setattr__(config, "config_path", original.config_path)
    monkeypatch.setattr(example, "load_config", lambda path: config)
    source, attachments, directory = [tmp_path / name for name in ("source", "attachments", "inspections")]
    for path in (source, attachments, directory):
        path.mkdir()
    inspector = tmp_path / "artificial-inspector.mjs"
    inspector.write_text("// Artificial extractor identity; not source evidence.\n", encoding="utf-8")
    def forbidden(*args, **kwargs):
        raise AssertionError("offline workbook review attempted network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    return config, source, attachments, directory, inspector


def seed(cli, number=1, rows=None, extra_sheets=(), extra_link=False):
    config, source, attachments, directory, inspector = cli
    body = detail(number)
    url = "https://oss-ch.csindex.com.cn/notice/artificial-%d.xlsx" % number
    body["data"]["enclosureList"][0]["fileUrl"] = url
    if extra_link:
        body["data"]["enclosureList"].append({"noticeId": number, "fileName": "other.pdf",
                                               "fileUrl": "https://oss-ch.csindex.com.cn/notice/other.pdf"})
    body_source = OfficialNoticeSource(source, opener=lambda req, timeout: Response(req.full_url, body),
                                      sleeper=lambda value: None).notice(number)[1]
    artifact = OfficialNoticeSource(attachments, opener=lambda req, timeout: Response(req.full_url, workbook(("Sheet1",) + extra_sheets)),
                                   sleeper=lambda value: None).attachment(url)
    inspection = {"diagnostic_only": True, "point_in_time_universe_verified": False, "input_path": str(artifact.path),
                  "input_sha256": digest(artifact.content), "inspector_sha256": digest(inspector.read_bytes()),
                  "sheets": [sheet(rows)]}
    path = directory / ("%d.inspection.json" % number)
    path.write_text(json.dumps(inspection, ensure_ascii=False), encoding="utf-8")
    return path, body_source, artifact


def run(cli, ids=(1,)):
    config, source, attachments, directory, inspector = cli
    return example.main(["--source-archive", str(source), "--attachment-root", str(attachments),
                         "--inspection-directory", str(directory), "--inspector-script", str(inspector),
                         "--notice-ids", *map(str, ids)])


def latest(cli):
    return json.loads(sorted(cli[0].paths.runs_dir.glob("*/manifest.json"))[-1].read_text(encoding="utf-8"))


def test_file_hashes_originals_and_offline_replay(cli):
    inspection_path, body, attachment = seed(cli)
    before = {p: p.read_bytes() for root in cli[1:4] for p in root.iterdir()}
    assert run(cli) == 0
    m = latest(cli)
    assert m["status"] == "success" and m["network_requests"] == 0 and m["verified_cache_reads"] == 2
    assert m["workbooks_reviewed"] == 1 and m["candidate_change_events"] == 2
    assert m["point_in_time_universe_verified"] is m["historical_availability_proved"] is False
    assert m["extraction_completeness_independently_proved"] is False
    for value in m["output_artifacts"].values():
        assert value["sha256"] == digest(Path(value["path"]).read_bytes())
    review = json.loads(Path(m["outputs"]["review"]).read_text(encoding="utf-8"))["workbook_reviews"][0]
    evidence = review["source_evidence"]
    assert Path(evidence["body"]["path"]).read_bytes() == body.content
    assert Path(evidence["workbook"]["path"]).read_bytes() == attachment.content
    assert Path(evidence["inspection"]["path"]).read_bytes() == inspection_path.read_bytes()
    old = {v["path"]: Path(v["path"]).read_bytes() for v in m["output_artifacts"].values()}
    assert run(cli) == 0 and latest(cli)["run_id"] != m["run_id"]
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert all(Path(p).read_bytes() == raw for p, raw in old.items())
    assert not cli[0].paths.universe_dir.exists() and not cli[0].paths.processed_dir.exists()


def test_supported_absence_is_explicit_not_body_keyword_filter(cli):
    seed(cli, rows=[OTHER])
    assert run(cli) == 0
    m = latest(cli)
    assert m["candidate_change_events"] == 0
    r = json.loads(Path(m["outputs"]["review"]).read_text(encoding="utf-8"))["workbook_reviews"][0]["workbook_review"]
    assert r["body_mentions_csi500"] is False and r["review"]["status"] == "NO_CSI500_ENTRIES_IN_SUPPORTED_TABLES"


@pytest.mark.parametrize("condition", ["inspection-missing", "source-hash", "extractor-hash", "source-path", "pit-flag",
                                      "not-diagnostic", "missing-sheet", "unknown-sheet", "bad-header", "corrupt-workbook"])
def test_invalid_identity_inventory_or_layout_not_negative_evidence(cli, condition):
    path, _, artifact = seed(cli, extra_sheets=("UNREVIEWED",) if condition == "missing-sheet" else ())
    value = json.loads(path.read_text(encoding="utf-8"))
    if condition == "inspection-missing":
        path.unlink()
    elif condition == "corrupt-workbook":
        artifact.path.write_bytes(b"PK corrupt")
    else:
        if condition == "source-hash": value["input_sha256"] = "0" * 64
        if condition == "extractor-hash": value["inspector_sha256"] = "0" * 64
        if condition == "source-path": value["input_path"] = str(path)
        if condition == "pit-flag": value["point_in_time_universe_verified"] = True
        if condition == "not-diagnostic": value["diagnostic_only"] = False
        if condition == "unknown-sheet": value["sheets"][0]["sheet"] = "UNKNOWN"
        if condition == "bad-header": value["sheets"][0]["values"][0][2] = "未知方向"
        path.write_text(json.dumps(value), encoding="utf-8")
    assert run(cli) == 1
    m = latest(cli)
    assert m["status"] == "partial" and m["network_requests"] == 0 and m["workbooks_reviewed"] == 0
    assert len(m["failures"]["workbook_source_review"]) == 1


def test_missing_attachment_does_not_request_http(cli):
    _, _, artifact = seed(cli)
    artifact.path.unlink()
    assert run(cli) == 1 and latest(cli)["network_requests"] == 0


def test_missing_cached_body_is_terminal_without_network(cli):
    assert run(cli) == 1
    m = latest(cli)
    assert m["status"] == "failed" and m["network_requests"] == 0 and not m["outputs"]


def test_one_failed_notice_retains_other_candidates(cli):
    seed(cli, 1)
    path, _, _ = seed(cli, 2)
    path.write_text("invalid", encoding="utf-8")
    assert run(cli, (1, 2)) == 1
    assert latest(cli)["status"] == "partial" and latest(cli)["workbooks_reviewed"] == 1
    assert latest(cli)["candidate_change_events"] == 2


def test_multiple_literal_attachments_require_explicit_independent_review(cli):
    seed(cli, extra_link=True)
    assert run(cli) == 1 and latest(cli)["workbooks_reviewed"] == 0


@pytest.mark.parametrize("ids", [(0,), (1, 1), tuple(range(1, 22))], ids=["nonpositive", "duplicate", "too-many"])
def test_invalid_notice_selection(cli, ids):
    assert run(cli, ids) == 1 and latest(cli)["status"] == "failed"


def test_output_collision_terminal_no_overwrite(cli, monkeypatch):
    seed(cli)
    real = example.RunManifest
    def manifest(*args, **kwargs):
        m = real(*args, **kwargs)
        (cli[0].paths.results_root / "diagnostics" / "workbook-notice-review" / m.run_id / "inputs").mkdir(parents=True)
        return m
    monkeypatch.setattr(example, "RunManifest", manifest)
    assert run(cli) == 1 and latest(cli)["status"] == "failed"


@pytest.mark.parametrize("metadata", ['<!DOCTYPE x><workbook/>', '<!ENTITY x "test"><workbook/>',
                                     '<workbook/>', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                                     '<sheets><sheet name="A"/><sheet name="A"/></sheets></workbook>'])
def test_unsupported_original_xml_metadata_rejected(metadata):
    with pytest.raises(ProviderError):
        example._source_sheet_names(workbook(metadata=metadata))
