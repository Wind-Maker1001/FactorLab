"""Offline file/manifest pipeline with artificial PDF bytes and page strings."""

from dataclasses import replace
import csv
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

from examples.research import review_pdf_notices as example
from qbot.research.config import PathsConfig, load_config
from qbot.research.errors import ProviderError
from qbot.research.universe.official_sources import OfficialNoticeSource
from tests.research.test_browser_notice_observations import container, write, latest_manifest, Response
from tests.research.test_notice_pdf import HEAD, ROW1, ROW2, STOP


FAKE_PDF = b"%PDF- ARTIFICIAL TEST BYTES, NOT HISTORICAL MARKET EVIDENCE"


def observation(directory, number=1, links=1):
    payload = container(number, content="<p>中证500指数更换2只样本，2021年12月10日收市后生效。</p>")
    decoded = json.loads(payload["response_text"])
    decoded["data"]["enclosureList"] = [
        {"noticeId": number, "fileName": "ARTIFICIAL.pdf",
         "fileUrl": "https://oss-ch.csindex.com.cn/notice/artificial-%d-%d.pdf" % (number, i)}
        for i in range(links)]
    payload["response_text"] = json.dumps(decoded, ensure_ascii=False)
    return write(directory, payload, "%d.observation.json" % number), decoded["data"]["enclosureList"]


@pytest.fixture
def cli(tmp_path, monkeypatch):
    original = load_config("configs/research/csi500_smoke.yaml")
    config = replace(original, paths=PathsConfig(data_root=tmp_path / "data", results_root=tmp_path / "results"))
    object.__setattr__(config, "repo_root", original.repo_root)
    object.__setattr__(config, "config_path", original.config_path)
    monkeypatch.setattr(example, "load_config", lambda path: config)
    directory, archive = tmp_path / "observations", tmp_path / "archive"
    directory.mkdir()
    archive.mkdir()
    def forbidden(*args, **kwargs):
        raise AssertionError("offline PDF review must never connect to the network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(example, "_extract_pages", lambda raw: [HEAD + ROW1, ROW2 + STOP])
    return config, directory, archive


def cache(archive, url, content=FAKE_PDF):
    source = OfficialNoticeSource(archive, opener=lambda request, timeout: Response(request.full_url, content),
                                  sleeper=lambda value: None)
    return source.attachment(url)


def execute(directory, archive):
    return example.main(["--input-directory", str(directory), "--archive-root", str(archive)])


def test_real_cache_validation_exclusive_snapshots_csv_and_all_hash_bindings(cli):
    config, directory, archive = cli
    original, links = observation(directory)
    artifact = cache(archive, links[0]["fileUrl"])
    before = {p.name: p.read_bytes() for p in archive.iterdir()}
    assert execute(directory, archive) == 0
    run = latest_manifest(config)
    assert run["status"] == "success" and run["pdfs_reviewed"] == 1 and run["candidate_change_events"] == 4
    assert run["network_requests"] == 0 and run["verified_cache_reads"] == 1
    assert run["point_in_time_universe_verified"] is run["historical_constituents_generated"] is False
    assert run["visual_review_performed_by_this_run"] is run["historical_availability_proved"] is False
    assert run["manual_effective_date_review_required"] == []
    inputs = run["input_evidence"][0]
    assert Path(inputs["observation_snapshot"]).read_bytes() == original.read_bytes()
    assert Path(inputs["pdf_snapshot"]).read_bytes() == artifact.content
    assert inputs["pdf_sha256"] == hashlib.sha256(artifact.content).hexdigest()
    assert Path(inputs["receipt_snapshot"]).read_bytes() == Path(inputs["receipt_input_path"]).read_bytes()
    assert inputs["receipt_sha256"] == hashlib.sha256(Path(inputs["receipt_snapshot"]).read_bytes()).hexdigest()
    for row in run["output_artifacts"].values():
        assert row["sha256"] == hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest()
    with Path(run["outputs"]["candidate_changes"]).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 4 and rows[0]["symbol"] == "000001.SZ"
    assert rows[2]["source_page"] == "2" and rows[0]["point_in_time_universe_verified"] == "False"
    assert {p.name: p.read_bytes() for p in archive.iterdir()} == before
    assert not config.paths.universe_dir.exists() and not config.paths.processed_dir.exists()


def test_offline_missing_attachment_is_partial_not_network_fallback(cli):
    config, directory, archive = cli
    observation(directory)
    assert execute(directory, archive) == 1
    run = latest_manifest(config)
    assert run["status"] == "partial" and run["candidate_change_events"] == 0
    assert run["network_requests"] == run["verified_cache_reads"] == 0
    assert "absent from the verified cache" in run["failures"]["pdf_source_review"][0]["error"]


@pytest.mark.parametrize("kind", ["bytes", "receipt"])
def test_corrupt_cache_is_partial_and_never_repairs_source(cli, kind):
    config, directory, archive = cli
    _, links = observation(directory)
    artifact = cache(archive, links[0]["fileUrl"])
    if kind == "bytes":
        artifact.path.write_bytes(FAKE_PDF + b"tampered")
    else:
        receipt = next(archive.glob("*.receipt.json"))
        value = json.loads(receipt.read_text(encoding="utf-8"))
        value["http_status"] = 403
        receipt.write_text(json.dumps(value), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in archive.iterdir()}
    assert execute(directory, archive) == 1
    run = latest_manifest(config)
    assert run["status"] == "partial" and run["network_requests"] == run["verified_cache_reads"] == 0
    assert {p.name: p.read_bytes() for p in archive.iterdir()} == before


@pytest.mark.parametrize("links", [0, 2])
def test_zero_or_multiple_pdf_versions_require_selection(cli, links):
    config, directory, archive = cli
    observation(directory, links=links)
    assert execute(directory, archive) == 1
    run = latest_manifest(config)
    assert run["status"] == "partial" and run["verified_cache_reads"] == 0
    assert "exactly one" in run["failures"]["pdf_source_review"][0]["error"]


def test_failed_notice_does_not_discard_other_valid_candidates(cli):
    config, directory, archive = cli
    observation(directory, 1)
    _, links = observation(directory, 2)
    cache(archive, links[0]["fileUrl"])
    assert execute(directory, archive) == 1
    run = latest_manifest(config)
    assert run["status"] == "partial" and run["pdfs_reviewed"] == 1 and run["candidate_change_events"] == 4
    assert run["verified_cache_reads"] == 1 and run["network_requests"] == 0


def test_replay_creates_new_version_and_preserves_previous_evidence(cli):
    config, directory, archive = cli
    _, links = observation(directory)
    cache(archive, links[0]["fileUrl"])
    assert execute(directory, archive) == 0
    old = latest_manifest(config)
    old_bytes = {row["path"]: Path(row["path"]).read_bytes() for row in old["output_artifacts"].values()}
    assert execute(directory, archive) == 0
    new = latest_manifest(config)
    assert old["run_id"] != new["run_id"] and new["network_requests"] == 0
    assert all(Path(path).read_bytes() == content for path, content in old_bytes.items())


@pytest.mark.parametrize("kind", ["empty", "bad-container", "duplicate", "missing-archive"])
def test_invalid_inputs_terminal_manifest_before_cache_reads(cli, kind):
    config, directory, archive = cli
    if kind != "empty":
        path, _ = observation(directory)
        if kind == "bad-container":
            path.write_bytes(b"invalid")
        elif kind == "duplicate":
            (directory / "duplicate.observation.json").write_bytes(path.read_bytes())
        elif kind == "missing-archive":
            archive = archive / "absent"
    assert execute(directory, archive) == 1
    run = latest_manifest(config)
    assert run["status"] == "failed" and run["completed_at"]
    assert run["network_requests"] == run["verified_cache_reads"] == 0


def test_pdf_layout_failure_preserves_raw_and_text_inputs_without_empty_success(cli, monkeypatch):
    config, directory, archive = cli
    _, links = observation(directory)
    cache(archive, links[0]["fileUrl"])
    monkeypatch.setattr(example, "_extract_pages", lambda content: [HEAD + ROW1 + STOP])
    assert execute(directory, archive) == 1
    run = latest_manifest(config)
    assert run["status"] == "partial" and run["candidate_change_events"] == 0
    assert "stated change count" in run["failures"]["pdf_source_review"][0]["error"]
    assert Path(run["input_evidence"][0]["extracted_pages_snapshot"]).is_file()


def test_optional_reader_extraction_contract_uses_full_pages_without_reordering(monkeypatch):
    seen = []
    class Reader:
        is_encrypted = False
        pages = [SimpleNamespace(extract_text=lambda: "page1"), SimpleNamespace(extract_text=lambda: None)]
        def __init__(self, stream, strict):
            seen.append((stream.read(), strict))
    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=Reader))
    assert example._extract_pages(FAKE_PDF) == ["page1", ""]
    assert seen == [(FAKE_PDF, True)]


def test_missing_optional_reader_has_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(ProviderError, match="requirements-source-review"):
        example._extract_pages(FAKE_PDF)


@pytest.mark.parametrize("raw", [b"", b"PK NOT A PDF", b"%PDF-" + b" " * (20 * 1024 * 1024)],
                         ids=["empty", "wrong-signature", "oversized"])
def test_non_pdf_or_oversized_bytes_rejected_before_optional_import(raw, monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(ProviderError, match="signature"):
        example._extract_pages(raw)


@pytest.mark.parametrize("encrypted,count", [(True, 1), (False, 0), (False, 101)])
def test_encrypted_or_unbounded_reader_rejected(encrypted, count, monkeypatch):
    reader = SimpleNamespace(is_encrypted=encrypted, pages=[None] * count)
    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=lambda *a, **kw: reader))
    with pytest.raises(ProviderError, match="unencrypted"):
        example._extract_pages(FAKE_PDF)
