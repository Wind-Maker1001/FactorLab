"""Offline supervisor-boundary tests; no real SDK/network or PIT claims."""

from copy import deepcopy
from datetime import date
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import zipfile

import pytest

from examples.research import baostock_source_probe as probe
from qbot.research.config import (
    DataConfig, PathsConfig, ProjectConfig, ResearchConfig, UniverseConfig,
    discover_repository_root,
)
from qbot.research.errors import ConfigurationError, DataQualityError
from qbot.research.reproducibility import validate_provenance


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    config = ResearchConfig(project=ProjectConfig("synthetic-source-probe-test-only"),
                            data=DataConfig(date(2024, 1, 2), date(2024, 1, 12)),
                            universe=UniverseConfig(), factors=("REV1",),
                            paths=PathsConfig(data_root=tmp_path / "data", results_root=tmp_path / "results"))
    object.__setattr__(config, "repo_root", discover_repository_root())
    config.validate()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("baostock-0.9.4.dist-info/METADATA", "Name: baostock\nVersion: 0.9.4\n")
    wheel = tmp_path / "synthetic-offline-wheel.whl"
    wheel.write_bytes(buffer.getvalue())
    # Only the publisher boundary is synthetic, not the real config/manifest.
    monkeypatch.setattr(probe, "PUBLISHED_WHEEL_SHA256", hashlib.sha256(buffer.getvalue()).hexdigest())
    return config, wheel


def _response(wheel):
    rows = [["2023-11-24", "sh.%06d" % (600000 + index), "fixture"] for index in range(500)]
    return {
        "schema_version": 1, "diagnostic_only": True, "point_in_time_universe_verified": False,
        "sdk_version": probe.SDK_VERSION, "sdk_client_version": probe.SDK_CLIENT_VERSION,
        "sdk_import_path": str(wheel) + "/baostock/__init__.py",
        "server": {"host": probe.PUBLIC_HOST, "port": probe.PUBLIC_PORT},
        "login": {"error_code": "0", "error_message": "success"}, "child_exception": None,
        "queries": [{"requested_date": value, "server_date": value,
                     "fields": ["updateDate", "code", "code_name"], "rows": deepcopy(rows),
                     "error_code": "0", "error_message": "success"} for value in probe.DEFAULT_DATES],
    }


def _mock_child(monkeypatch, result=None, error=None):
    calls = []
    def run(*args, **kwargs):
        calls.append((args, kwargs))
        if error:
            raise error
        return result
    # Replace only this example's reference, not subprocess.run globally:
    # production Git/provenance collection must remain genuine.
    monkeypatch.setattr(probe, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired))
    return calls


def _stored(config):
    manifests = list(config.paths.runs_dir.glob("*/manifest.json"))
    assert len(manifests) == 1
    return json.loads(manifests[0].read_text(encoding="utf-8")), manifests[0]


def test_supervisor_records_genuine_manifest_but_never_certifies_pit(inputs, monkeypatch):
    config, wheel = inputs
    calls = _mock_child(monkeypatch, SimpleNamespace(returncode=0, stdout=json.dumps(_response(wheel)), stderr="SDK log discarded"))
    result = probe.run_probe(config, sdk_wheel=wheel)
    stored, path = _stored(config)
    validate_provenance(stored)
    assert stored == result["manifest"]
    assert stored["status"] == "success"
    assert stored["diagnostic_only"] is True
    assert stored["research_conclusions_permitted"] is False
    assert stored["point_in_time_universe_verified"] is False
    assert stored["effective_membership_dates_verified"] is False
    assert stored["query_contract_verified"] is True
    assert stored["installed_baostock_version"] == importlib.metadata.version("baostock")
    assert stored["sdk_wheel"]["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert stored["sdk_wheel"]["version"] == "0.9.4"
    assert stored["source_urls"] == list(probe.SOURCE_URLS)
    diagnostic = result["diagnostic"]
    assert diagnostic["point_in_time_universe_verified"] is False
    for query in diagnostic["response"]["queries"]:
        assert query["row_count"] == query["unique_member_count"] == 500
        assert query["update_date_distribution"] == {"2023-11-24": 500}
        assert query["query_contract_verified"] is True
        assert query["point_in_time_universe_verified"] is False
        codes = sorted(row[1] for row in query["rows"])
        assert query["members_sha256"] == hashlib.sha256(("\n".join(codes) + "\n").encode()).hexdigest()
    assert diagnostic["response"]["effective_membership_dates_verified"] is False
    data_path = Path(result["diagnostic_path"])
    assert data_path.parent == path.parent
    assert hashlib.sha256(data_path.read_bytes()).hexdigest() == stored["probe"]["diagnostic_sha256"]
    command, options = calls[0]
    assert "--child" in command[0]
    assert options["timeout"] == 20.0
    assert options["capture_output"] is True
    assert options["check"] is False
    assert options["cwd"] == str(config.repo_root)
    assert not config.paths.data_root.exists()
    assert not config.paths.universe_dir.exists()
    assert not config.paths.processed_dir.exists()
    assert not config.paths.forward_dir.exists()


def test_whole_child_timeout_records_sanitized_failure_not_partial_stdout(inputs, monkeypatch):
    config, wheel = inputs
    secret = "never-persist-raw-credential-or-debug-output"
    calls = _mock_child(monkeypatch, error=subprocess.TimeoutExpired(["child"], 20, output=secret, stderr=secret))
    with pytest.raises(DataQualityError, match="killed and reaped"):
        probe.run_probe(config, sdk_wheel=wheel)
    stored, path = _stored(config)
    assert stored["status"] == "failed"
    diagnostic = json.loads((path.parent / "diagnostic.json").read_text(encoding="utf-8"))
    assert diagnostic["child_timeout"] == {"timeout_seconds": 20.0, "child_killed_and_reaped": True}
    assert diagnostic["point_in_time_universe_verified"] is False
    assert diagnostic["query_contract_verified"] is False
    assert secret not in json.dumps(diagnostic)
    assert secret not in json.dumps(stored)
    assert len(calls) == 1


def test_wrong_wheel_hash_stops_before_child_and_is_recorded(inputs, monkeypatch):
    config, wheel = inputs
    wheel.write_bytes(b"wrong SDK bytes")
    calls = _mock_child(monkeypatch)
    with pytest.raises(DataQualityError, match="publisher SHA"):
        probe.run_probe(config, sdk_wheel=wheel)
    stored, _ = _stored(config)
    assert stored["status"] == "failed"
    assert stored["query_contract_verified"] is False
    assert calls == []


def test_wrong_metadata_cannot_pass_even_with_matching_fixture_hash(inputs, monkeypatch):
    config, wheel = inputs
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("baostock-0.9.4.dist-info/METADATA", "Name: baostock\nVersion: 0.8.9\n")
    wheel.write_bytes(buffer.getvalue())
    monkeypatch.setattr(probe, "PUBLISHED_WHEEL_SHA256", hashlib.sha256(buffer.getvalue()).hexdigest())
    calls = _mock_child(monkeypatch)
    with pytest.raises(DataQualityError, match="metadata"):
        probe.run_probe(config, sdk_wheel=wheel)
    assert calls == []


@pytest.mark.parametrize("operation", ["wrong_server_date", "duplicate", "499_rows", "query_error", "missing_query", "login_error"])
def test_invalid_historical_query_cannot_satisfy_contract(inputs, monkeypatch, operation):
    config, wheel = inputs
    response = _response(wheel)
    if operation == "wrong_server_date":
        response["queries"][0]["server_date"] = "2026-10-09"
    elif operation == "duplicate":
        response["queries"][0]["rows"][-1] = response["queries"][0]["rows"][0]
    elif operation == "499_rows":
        response["queries"][0]["rows"].pop()
    elif operation == "query_error":
        response["queries"][0]["error_code"] = "-1"
    elif operation == "missing_query":
        response["queries"].pop()
    else:
        response["login"]["error_code"] = "-1"
        response["queries"] = []
    _mock_child(monkeypatch, SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr=""))
    with pytest.raises(DataQualityError, match="not verified"):
        probe.run_probe(config, sdk_wheel=wheel)
    stored, path = _stored(config)
    assert stored["status"] == "failed"
    diagnostic = json.loads((path.parent / "diagnostic.json").read_text(encoding="utf-8"))
    assert diagnostic["query_contract_verified"] is False
    assert diagnostic["point_in_time_universe_verified"] is False


@pytest.mark.parametrize("operation", ["true_pit", "installed_import", "wrong_server", "extra_field", "mixed_sdk_prints"])
def test_untrusted_child_claim_or_mixed_console_is_rejected(inputs, monkeypatch, operation):
    config, wheel = inputs
    response = _response(wheel)
    if operation == "true_pit":
        response["point_in_time_universe_verified"] = True
    elif operation == "installed_import":
        response["sdk_import_path"] = "site-packages/baostock/__init__.py"
    elif operation == "wrong_server":
        response["server"]["host"] = "vip-api.baostock.com"
    elif operation == "extra_field":
        response["extra"] = "do not persist"
    output = json.dumps(response)
    if operation == "mixed_sdk_prints":
        output = "login success!\n" + output
    _mock_child(monkeypatch, SimpleNamespace(returncode=0, stdout=output, stderr="raw secret not persisted"))
    with pytest.raises(DataQualityError):
        probe.run_probe(config, sdk_wheel=wheel)
    stored, path = _stored(config)
    diagnostic = json.loads((path.parent / "diagnostic.json").read_text(encoding="utf-8"))
    assert diagnostic["point_in_time_universe_verified"] is False
    assert "response" not in diagnostic
    assert "raw secret" not in json.dumps(stored)


def test_initial_manifest_publication_failure_prevents_network(inputs, monkeypatch):
    config, wheel = inputs
    calls = _mock_child(monkeypatch)
    def fail_manifest(*args, **kwargs):
        raise OSError("manifest storage unavailable")
    monkeypatch.setattr(probe, "RunManifest", fail_manifest)
    with pytest.raises(OSError, match="manifest storage"):
        probe.run_probe(config, sdk_wheel=wheel)
    assert calls == []


def test_safeguard_manifest_failure_prevents_network(inputs, monkeypatch):
    config, wheel = inputs
    calls = _mock_child(monkeypatch)
    def fail_output(*args, **kwargs):
        raise OSError("manifest safeguard publication unavailable")
    monkeypatch.setattr(probe.RunManifest, "add_output", fail_output)
    with pytest.raises(OSError, match="safeguard"):
        probe.run_probe(config, sdk_wheel=wheel)
    assert calls == []
    assert not list(config.paths.runs_dir.glob("*/diagnostic.json"))


def test_final_manifest_failure_cannot_return_success(inputs, monkeypatch):
    config, wheel = inputs
    _mock_child(monkeypatch, SimpleNamespace(returncode=0, stdout=json.dumps(_response(wheel)), stderr=""))
    def fail_finish(*args, **kwargs):
        raise OSError("final manifest unavailable")
    monkeypatch.setattr(probe.RunManifest, "finish", fail_finish)
    with pytest.raises(OSError, match="final manifest"):
        probe.run_probe(config, sdk_wheel=wheel)
    stored, _ = _stored(config)
    assert stored["status"] == "running"
    assert stored["point_in_time_universe_verified"] is False


def test_missing_installed_distribution_is_recorded_as_none(inputs, monkeypatch):
    config, wheel = inputs
    _mock_child(monkeypatch, SimpleNamespace(returncode=0, stdout=json.dumps(_response(wheel)), stderr=""))
    real_version = importlib.metadata.version
    def absent_baostock(name):
        if name == "baostock":
            raise importlib.metadata.PackageNotFoundError(name)
        return real_version(name)
    monkeypatch.setattr(importlib.metadata, "version", absent_baostock)
    result = probe.run_probe(config, sdk_wheel=wheel)
    assert result["manifest"]["status"] == "success"
    assert result["manifest"]["installed_baostock_version"] is None
    assert result["manifest"]["package_versions"]["baostock"] is None
    assert result["manifest"]["point_in_time_universe_verified"] is False


@pytest.mark.parametrize("timeout", [True, 0, -1, 61, float("nan"), float("inf"), "20"])
def test_invalid_deadline_rejected(inputs, monkeypatch, timeout):
    config, wheel = inputs
    calls = _mock_child(monkeypatch)
    with pytest.raises(ConfigurationError):
        probe.run_probe(config, sdk_wheel=wheel, timeout_seconds=timeout)
    assert calls == []


@pytest.mark.parametrize("dates", [["2024-01-02"], ["2024-01-02"] * 2, ["2024-01-02", "2024-1-3"], ["2024-01-02", "2099-01-01"]])
def test_invalid_dates_rejected(inputs, monkeypatch, dates):
    config, wheel = inputs
    calls = _mock_child(monkeypatch)
    with pytest.raises(ConfigurationError):
        probe.run_probe(config, sdk_wheel=wheel, dates=dates)
    assert calls == []
