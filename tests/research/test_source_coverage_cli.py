"""File-level artificial coverage receipts; all real HTTP is forbidden."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import urllib.request

import pytest

from examples.research import audit_official_coverage as example
from qbot.research.config import PathsConfig, load_config
from qbot.research.universe.official_sources import CATALOGUE_URL, DETAIL_URL, OfficialNoticeSource
from tests.research.test_browser_notice_observations import container, write
from tests.research.test_official_sources import Response, detail


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    original = load_config("configs/research/csi500_smoke.yaml")
    config = replace(original, paths=PathsConfig(data_root=tmp_path / "data", results_root=tmp_path / "results"))
    object.__setattr__(config, "repo_root", original.repo_root)
    object.__setattr__(config, "config_path", original.config_path)
    monkeypatch.setattr(example, "load_config", lambda path: config)
    archive, browser = tmp_path / "archive", tmp_path / "browser"
    browser.mkdir()
    direct = detail(1)
    observation = container(2, enclosure=False)
    second = json.loads(observation["response_text"])["data"]
    listings = [{key: row[key] for key in ("id", "title", "publishDate")} for row in (direct["data"], second)]

    def opener(request, timeout):
        value = {"success": True, "code": "200", "total": 2, "currentPage": 1, "pageSize": 100,
                 "data": listings} if request.full_url == CATALOGUE_URL else direct
        return Response(request.full_url, value)

    client = OfficialNoticeSource(archive, opener=opener, sleeper=lambda seconds: None)
    client.catalogue()
    _, artifact = client.notice(1)
    write(browser, observation, "2.observation.json")

    def forbidden(*args, **kwargs):
        raise AssertionError("Coverage audit must not perform HTTP")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)
    return config, archive, browser, artifact


def run(archive, browser=None, *extras):
    args = ["--archive-root", str(archive)]
    if browser is not None:
        args += ["--browser-directories", str(browser)]
    return example.main(args + list(extras))


def manifest(config):
    return json.loads(sorted(config.paths.runs_dir.glob("*/manifest.json"))[-1].read_text(encoding="utf-8"))


def test_complete_scope_has_bound_bytes_but_no_pit_and_no_http(inputs):
    config, archive, browser, artifact = inputs
    assert run(archive, browser) == 0
    receipt = manifest(config)
    assert receipt["status"] == "success" and receipt["network_requests"] == 0
    assert receipt["verified_cache_reads"] == 2 and receipt["browser_requests_performed_by_this_run"] == 0
    assert receipt["observed_bodies_in_scope"] == 2 and receipt["missing_notice_ids"] == []
    assert receipt["point_in_time_universe_verified"] is False and receipt["historical_constituents_generated"] is False
    assert len(receipt["source_inputs"]) == 3
    for entry in receipt["source_inputs"]:
        snapshot = Path(entry["snapshot_path"]).read_bytes()
        assert hashlib.sha256(snapshot).hexdigest() == entry["snapshot_sha256"]
        assert snapshot == Path(entry["input_path"]).read_bytes()
    for entry in receipt["output_artifacts"].values():
        assert hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
    coverage = json.loads(Path(receipt["outputs"]["coverage"]).read_text(encoding="utf-8"))
    kinds = [e["source_evidence"] for e in coverage["source_evidence"]]
    assert kinds[0]["representation"].startswith("exact bytes read")
    assert kinds[1]["representation"].startswith("exact response.text()")
    assert not (config.paths.data_root / "universe").exists()


def test_missing_body_produces_partial_artifacts_not_false_success(inputs):
    config, archive, browser, artifact = inputs
    assert run(archive) == 1
    receipt = manifest(config)
    assert receipt["status"] == "partial" and receipt["missing_notice_ids"] == [2]
    assert receipt["network_requests"] == 0
    coverage = json.loads(Path(receipt["outputs"]["coverage"]).read_text(encoding="utf-8"))
    assert coverage["rows"][1]["discovered_attachment_count"] is None
    assert receipt["failures"]["missing_notice_bodies"] == [{"notice_id": 2}]


def test_run_inputs_remain_immutable_after_original_browser_changes(inputs):
    config, archive, browser, artifact = inputs
    assert run(archive, browser) == 0
    first = manifest(config)
    snapshot = [e for e in first["source_inputs"] if e["source_evidence"].get("transport_label_supplied_by_observation")][0]
    original = Path(snapshot["input_path"])
    original.write_text("changed after this run", encoding="utf-8")
    assert hashlib.sha256(Path(snapshot["snapshot_path"]).read_bytes()).hexdigest() == snapshot["snapshot_sha256"]
    assert run(archive, browser) == 1
    assert manifest(config)["status"] == "failed"


def test_repeated_runs_append_new_outputs_without_overwriting(inputs):
    config, archive, browser, artifact = inputs
    assert run(archive, browser) == 0
    first = manifest(config)
    first_bytes = Path(first["outputs"]["coverage"]).read_bytes()
    assert run(archive, browser) == 0
    second = manifest(config)
    assert first["run_id"] != second["run_id"]
    assert first["outputs"]["coverage"] != second["outputs"]["coverage"]
    assert Path(first["outputs"]["coverage"]).read_bytes() == first_bytes


@pytest.mark.parametrize("problem", ["bytes", "receipt", "discovery-json", "noncanonical-url", "duplicate-receipt"])
def test_bad_archive_fails_without_http_repair(inputs, problem):
    config, archive, browser, artifact = inputs
    receipt_path = next(path for path in archive.glob("*.receipt.json") if
                        json.loads(path.read_text(encoding="utf-8"))["request"]["url"].startswith(DETAIL_URL))
    if problem == "bytes":
        artifact.path.write_bytes(b"tampered")
    elif problem == "discovery-json":
        receipt_path.write_text("not JSON", encoding="utf-8")
    elif problem == "duplicate-receipt":
        (archive / "duplicate.receipt.json").write_bytes(receipt_path.read_bytes())
    else:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if problem == "receipt":
            receipt["point_in_time_universe_verified"] = True
        else:
            receipt["request"]["url"] = DETAIL_URL + "?id=01"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert run(archive, browser) == 1
    result = manifest(config)
    assert result["status"] == "failed" and result["network_requests"] == 0
    assert "coverage" not in result["outputs"]


@pytest.mark.parametrize("problem", ["duplicate-id", "unknown-container-field", "identity-mismatch", "empty-directory"])
def test_invalid_browser_inputs_fail_without_silent_selection(inputs, problem):
    config, archive, browser, artifact = inputs
    path = browser / "2.observation.json"
    if problem == "duplicate-id":
        write(browser, container(1), "1.observation.json")
    elif problem == "empty-directory":
        path.unlink()
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if problem == "unknown-container-field":
            payload["verified"] = True
        else:
            body = json.loads(payload["response_text"])
            body["data"]["title"] = "mismatching artificial title"
            payload["response_text"] = json.dumps(body)
        path.write_text(json.dumps(payload), encoding="utf-8")
    assert run(archive, browser) == 1
    assert manifest(config)["status"] == "failed" and manifest(config)["network_requests"] == 0


def test_duplicate_input_directory_not_read_twice(inputs):
    config, archive, browser, artifact = inputs
    assert run(archive, browser, str(browser)) == 1
    assert manifest(config)["status"] == "failed"


def test_absent_archive_is_not_created_or_fetched(inputs):
    config, archive, browser, artifact = inputs
    absent = archive.parent / "absent"
    assert run(absent, browser) == 1
    assert not absent.exists() and manifest(config)["network_requests"] == 0


def test_snapshot_io_failure_records_terminal_failure(inputs, monkeypatch):
    config, archive, browser, artifact = inputs
    original = Path.open

    def denied(self, mode="r", *args, **kwargs):
        if mode == "xb" and self.parent.name == "inputs":
            raise PermissionError("artificial snapshot write failure")
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    assert run(archive, browser) == 1
    result = manifest(config)
    assert result["status"] == "failed" and result["network_requests"] == 0
