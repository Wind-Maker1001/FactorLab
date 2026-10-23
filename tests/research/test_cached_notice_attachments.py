"""Artificial cached HTTP notices; never authentic historical constituent data."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import urllib.error

import pytest

from examples.research import download_cached_notice_attachments as example
from factorlab.config import PathsConfig, load_config
from factorlab.universe.official_sources import OfficialNoticeSource
from tests.research.test_official_sources import detail, Response


@pytest.fixture
def cli(tmp_path, monkeypatch):
    original = load_config("configs/research/csi500_smoke.yaml")
    config = replace(original, paths=PathsConfig(data_root=tmp_path / "data", results_root=tmp_path / "results"))
    object.__setattr__(config, "repo_root", original.repo_root)
    object.__setattr__(config, "config_path", original.config_path)
    monkeypatch.setattr(example, "load_config", lambda path: config)
    source, archive = tmp_path / "source", tmp_path / "attachments"
    source.mkdir()
    calls, replies, factories = [], [], []
    def opener(request, timeout):
        calls.append(request.full_url)
        value = replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return Response(request.full_url, value)
    def factory(root, **kwargs):
        factories.append((Path(root), dict(kwargs)))
        return OfficialNoticeSource(root, opener=opener, sleeper=lambda value: None, **kwargs)
    monkeypatch.setattr(example, "OfficialNoticeSource", factory)
    return config, source, archive, calls, replies, factories


def seed(source, number=1):
    payload = detail(number)
    payload["data"]["enclosureList"][0]["fileUrl"] = "https://oss-ch.csindex.com.cn/notice/artificial-%d.xlsx" % number
    client = OfficialNoticeSource(source, opener=lambda req, timeout: Response(req.full_url, payload),
                                  sleeper=lambda value: None)
    return client.notice(number)[1]


def run(source, archive, ids=(1,), extra=()):
    return example.main(["--source-archive", str(source), "--attachment-root", str(archive),
                         "--notice-ids", *map(str, ids), *extra])


def latest(config):
    return json.loads(sorted(config.paths.runs_dir.glob("*/manifest.json"))[-1].read_text(encoding="utf-8"))


def test_cached_body_only_literal_download_independent_roots_and_hash_bindings(cli):
    config, source, archive, calls, replies, factories = cli
    original = seed(source)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    replies.append(b"PK ARTIFICIAL NOT A REAL WORKBOOK")
    assert run(source, archive) == 0
    m = latest(config)
    assert m["status"] == "success" and m["archived_not_interpreted"] == 1
    assert m["body_network_requests"] == 0 and m["attachment_network_requests"] == 1
    assert m["verified_body_cache_reads"] == 1 and m["verified_attachment_cache_reads"] == 0
    assert all("queryAnnouncement" not in url for url in calls)
    assert factories[0][1]["cache_only"] is True
    assert m["point_in_time_universe_verified"] is m["historical_constituents_generated"] is False
    for value in m["output_artifacts"].values():
        assert value["sha256"] == hashlib.sha256(Path(value["path"]).read_bytes()).hexdigest()
    review = json.loads(Path(m["outputs"]["attachments"]).read_text(encoding="utf-8"))
    row = review["notices"][0]
    assert Path(row["body_snapshot"]).read_bytes() == original.content
    assert row["attachments"][0]["status"] == "ARCHIVED_NOT_INTERPRETED"
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    assert not config.paths.universe_dir.exists() and not config.paths.processed_dir.exists()


def test_cache_only_replay_preserves_previous_run(cli):
    config, source, archive, calls, replies, factories = cli
    seed(source)
    replies.append(b"PK ARTIFICIAL")
    assert run(source, archive) == 0
    old = latest(config)
    old_outputs = {v["path"]: Path(v["path"]).read_bytes() for v in old["output_artifacts"].values()}
    assert run(source, archive, extra=("--cache-only",)) == 0
    new = latest(config)
    assert new["run_id"] != old["run_id"] and len(calls) == 1
    assert new["body_network_requests"] == new["attachment_network_requests"] == 0
    assert new["verified_attachment_cache_reads"] == 1
    assert all(Path(path).read_bytes() == content for path, content in old_outputs.items())


@pytest.mark.parametrize("condition", ["missing", "corrupt", "missing-second"])
def test_all_bodies_validated_before_any_attachment_request(cli, condition):
    config, source, archive, calls, replies, factories = cli
    ids = (1,)
    if condition != "missing":
        artifact = seed(source)
        if condition == "corrupt":
            artifact.path.write_bytes(b"bad")
        else:
            ids = (1, 2)
    assert run(source, archive, ids) == 1
    m = latest(config)
    assert m["status"] == "failed" and m["completed_at"] and calls == []
    assert m["body_network_requests"] == m["attachment_network_requests"] == 0
    assert not archive.exists()


@pytest.mark.parametrize("ids", [(0,), (1, 1), tuple(range(1, 22))], ids=["nonpositive", "duplicate", "too-many"])
def test_invalid_explicit_selection(cli, ids):
    config, source, archive, calls, replies, factories = cli
    assert run(source, archive, ids) == 1
    assert latest(config)["status"] == "failed" and not calls and not factories


def test_same_input_and_output_archive_rejected(cli):
    config, source, archive, calls, replies, factories = cli
    seed(source)
    assert run(source, source) == 1
    assert latest(config)["status"] == "failed" and not calls


def test_partial_attachment_failure_continues_other_notice(cli):
    config, source, archive, calls, replies, factories = cli
    seed(source, 1)
    seed(source, 2)
    replies.extend([urllib.error.URLError("artificial failure"), b"PK ARTIFICIAL"])
    assert run(source, archive, (1, 2)) == 1
    m = latest(config)
    assert m["status"] == "partial" and m["archived_not_interpreted"] == 1
    assert m["body_network_requests"] == 0 and m["attachment_network_requests"] == len(calls) == 2


def test_access_denial_stops_further_attachment_requests(cli):
    config, source, archive, calls, replies, factories = cli
    seed(source, 1)
    seed(source, 2)
    replies.append(urllib.error.HTTPError("u", 403, "denied", {}, None))
    assert run(source, archive, (1, 2)) == 1
    m = latest(config)
    assert m["status"] == "partial" and m["attachment_network_requests"] == len(calls) == 1
    assert [x["skipped_after_site_denial"] for x in m["failures"]["cached_notice_attachments"]] == [False, True]


def test_attachment_cache_missing_never_falls_back_in_cache_only_mode(cli):
    config, source, archive, calls, replies, factories = cli
    seed(source)
    assert run(source, archive, extra=("--cache-only",)) == 1
    m = latest(config)
    assert m["status"] == "partial" and m["archived_not_interpreted"] == 0
    assert m["body_network_requests"] == m["attachment_network_requests"] == 0 and not calls


def test_output_collision_leaves_terminal_failure_without_network(cli, monkeypatch):
    config, source, archive, calls, replies, factories = cli
    seed(source)
    real = example.RunManifest
    def manifest(*args, **kwargs):
        m = real(*args, **kwargs)
        (config.paths.results_root / "diagnostics" / "cached-notice-attachments" / m.run_id / "inputs").mkdir(parents=True)
        return m
    monkeypatch.setattr(example, "RunManifest", manifest)
    assert run(source, archive) == 1
    assert latest(config)["status"] == "failed" and not calls
