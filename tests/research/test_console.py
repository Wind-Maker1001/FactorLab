"""Small public summaries and synthetic records; no strategy execution."""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import threading

import pytest

from factorlab.console.data import Catalog, ReadOnlyTracker, public, status
from factorlab.console.server import ConsoleServer
from factorlab.experiments import ExperimentTracker

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/console/public-research.json"


@pytest.fixture
def catalog():
    return Catalog(CONFIG)


@pytest.fixture
def synthetic(tmp_path):
    root = tmp_path / "tracker"
    tracker = ExperimentTracker(root)
    definition = dict(model="ridge", parameters={"alpha": 1}, factors=[],
                      dataset={"sha256": "b" * 64}, label={"name": "EXCESS_RET5"},
                      windows={"train": None, "validation": None, "test": ["2020-07-01", "2020-08-31"]},
                      walk_forward={"purge_days": 5}, costs={"bps": 10})
    tracker.create_experiment("e1", definition)
    options = dict(code={"commit": "c" * 40, "tree": "d" * 40},
                   environment={"python": "3.13", "API_TOKEN": "do-not-publish"},
                   command=["python", "research.py"], status="SUCCEEDED", qualification="SYNTHETIC",
                   prediction_metrics={"ic": None, "zero": 0},
                   artifacts={"private": {"path": str(tmp_path / "private.txt")}},
                   validation={"sample_hash": "e" * 64, "evaluation": {"minimum_cross_section": 4}},
                   timestamp="2026-11-10T12:00:00+00:00")
    tracker.append_run("e1", "r1", **options)
    options["validation"] = {**options["validation"], "research_status": "PARTIAL"}
    tracker.append_run("e1", "r2", **options)
    other = deepcopy(definition)
    other["costs"] = {"bps": 20}
    tracker.create_experiment("e2", other)
    tracker.append_run("e2", "r3", **options)
    # The console must discover JSON even when the disposable on-disk index is absent.
    tracker.index.unlink()
    config = tmp_path / "console.json"
    config.write_text(json.dumps({"projects": [], "trackers": [{"id": "synthetic", "root": "tracker", "project": "test", "strategy": "demo"}]}))
    return Catalog(config), root


@contextmanager
def running(catalog):
    with ConsoleServer(catalog, 0) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            worker.join()


def request(server, path, method="GET", headers=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    connection.request(method, path, headers=headers or {})
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_public_views_and_numeric_values(catalog):
    overview = catalog.overview()
    states = {p["id"]: p["status"] for p in overview["projects"]}
    assert states == {"p6-r01": "RESEARCH_COMPLETED", "p6-d01": "PARTIAL", "p6-e01": "BLOCKED"}
    overall = catalog.table("p6-r01:overall.csv", {})
    assert overall["total"] == 6
    bollinger = next(row for row in overall["rows"] if row["strategy"] == "bollinger")
    assert float(bollinger["mean_return"]) == pytest.approx(0.0017579057)
    assert bollinger["accounts"] == "54"
    annual = catalog.table("p6-r01:annual.csv", {"strategy": "cta", "year": "2018"})
    assert annual["total"] == 1
    assert float(annual["rows"][0]["mean_return"]) == pytest.approx(-0.0113514722)
    accounts = catalog.table("p6-r01:account-metrics.csv", {"symbol": "601857.SH", "year": "2018", "strategy": "rsi"})
    assert accounts["total"] == 1
    assert float(accounts["rows"][0]["cumulative_return"]) == pytest.approx(0.18981167)
    page = catalog.table("p6-r01:account-metrics.csv", {}, 20, 5)
    assert page["total"] == 324 and len(page["rows"]) == 5
    assert len(catalog.table("p6-r01:leave_one_security_out.csv", {"strategy": "cta"})["rows"]) == 22


def test_quality_sha_and_no_admission(catalog):
    d = catalog.evidence("p6-d01")
    assert d["quality"]["ready_rows"] == 22955
    assert d["quality"]["rejected_rows"] == 445
    assert len(d["quality"]["securities"]) == 24
    assert sum(len(s["intervals"]) for s in d["quality"]["securities"]) == 43
    assert d["manifest_sha256"] == hashlib.sha256(catalog.content("p6-d01:manifest.json")).hexdigest()
    assert d["historical_pit"] is False and d["formal_admission"] is False


def test_null_is_not_zero(catalog):
    d = catalog.evidence("p6-e01")
    assert d["total_return"] is None and d["total_return_status"] == "TOTAL_RETURN_UNAVAILABLE"
    rows = d["strategies"]
    assert len(rows) == 6
    assert rows["bollinger"]["bounded_gross_entitlement"] == "0.00000"
    assert rows["bollinger"]["total_return_net"] is None
    assert rows["dual_ma"]["events"][0]["eligible_shares"] == 0
    assert rows["dual_ma"]["events"][0]["actual_received"] is None
    assert len(catalog.table("p6-e01:EVENT_EVIDENCE.csv", {})["rows"]) == 2


def test_readonly_tracker_reuses_queries_compare_and_reproduction(synthetic):
    catalog, root = synthetic
    before = {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    rows = catalog.experiments({})
    assert rows["experiment_count"] == 2 and rows["total"] == 3
    assert catalog.experiments({"project": "test", "model": "ridge", "date": "2026-11-10", "strategy": "demo", "status": "PARTIAL"})["total"] == 2
    assert catalog.experiments({"model": "other"})["total"] == 0
    assert catalog.experiments({"model": "", "execution_status": "", "project": "", "strategy": "", "status": ""})["total"] == 3
    first = next(row for row in rows["rows"] if row["id"] == "r1")
    assert first["status"] == "UNKNOWN" and first["execution_status"] == "SUCCEEDED"
    detail = catalog.detail("synthetic", "r1")
    assert detail["run"]["prediction_metrics"] == {"ic": None, "zero": 0}
    assert detail["run"]["environment"] == {"python": "3.13"}
    assert detail["run"]["command"] == "[LOCAL_COMMAND_REDACTED]"
    assert catalog.experiments({"project": "different"})["experiment_count"] == 0
    assert detail["run"]["artifacts"]["private"]["path"] == "[LOCAL_PATH]"
    assert detail["artifact_verification"].startswith("NOT_RUN")
    assert catalog.compare("synthetic", ["r1", "r2"])["comparable"]
    with pytest.raises(ValueError, match="Incomparable"):
        catalog.compare("synthetic", ["r1", "r3"])
    after = {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    assert after == before and not (root / "index.sqlite3").exists()


@pytest.mark.parametrize("raw,expected", [("SUCCEEDED", "UNKNOWN"), ("anything_COMPLETED", "UNKNOWN"), ("NOT_RUN", "NOT_RUN"), ("SOFTWARE_ACCEPTED", "SOFTWARE_ACCEPTED"), (None, "UNKNOWN")])
def test_status_is_explicit(raw, expected):
    assert status(raw)["status"] == expected


def test_reports_allowlist_and_missing(catalog, tmp_path):
    content, mime = catalog.report("p6-r01:REPORT.md")
    assert "text/plain" in mime and "P6_R01" in content.decode()
    with pytest.raises(KeyError):
        catalog.content("../../.env")
    with pytest.raises(ValueError):
        catalog.report("p6-r01:real-summary.json")
    settings = {"projects": [{"id": "empty", "title": "Empty", "root": ".", "files": ["REPORT.md"]}]}
    config = tmp_path / "config.json"
    config.write_text(json.dumps(settings))
    project = Catalog(config).overview()["projects"][0]
    assert project["status"] == "UNKNOWN" and project["missing"] == ["REPORT.md"]


def test_symlink_escape_and_replacement(tmp_path):
    root = tmp_path / "public"
    root.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("do not read")
    report = root / "REPORT.md"
    report.write_text("public")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"projects": [{"id": "test", "title": "test", "root": "public", "files": ["REPORT.md"]}]}))
    catalog = Catalog(config)
    report.unlink()
    try:
        report.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires Windows Developer Mode or privilege")
    with pytest.raises(ValueError, match="outside"):
        catalog.report("test:REPORT.md")
    with pytest.raises(ValueError, match="outside"):
        Catalog(config)


def test_tracker_symlink_escape(tmp_path):
    root = tmp_path / "tracker"
    (root / "runs").mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    try:
        (root / "runs/r1.json").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires Windows Developer Mode or privilege")
    with pytest.raises(ValueError, match="outside"):
        ReadOnlyTracker(root)


def test_http_readonly_security_and_assets(catalog):
    with running(catalog) as server:
        assert server.server_address[0] == "127.0.0.1"
        code, headers, body = request(server, "/")
        assert code == 200 and 'lang="zh-CN"' in body.decode()
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        for name in ("/app.js", "/style.css", "/api/overview", "/api/evidence?project=p6-r01", "/report?id=p6-r01%3AREPORT.md"):
            assert request(server, name)[0] == 200
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"):
            assert request(server, "/api/overview", method)[0] == 405
        for path in ("/../.env", "/%2e%2e/.git/config", "/report?id=../../.env", "/report?id=C%3A%5CWindows%5Cwin.ini"):
            code, _, body = request(server, path)
            assert code == 404 and str(ROOT).encode() not in body
        for query in ("limit=101", "offset=-1", "limit=2&limit=3", "path=/private", "strategy=cta&secret=1"):
            assert request(server, "/api/table?id=p6-r01%3Aoverall.csv&" + query)[0] == 400
        assert request(server, "/api/overview", headers={"Host": "evil.example"})[0] == 403
        assert request(server, "/api/overview", headers={"Origin": "https://evil.example"})[0] == 403
        assert request(server, "/api/overview", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        data = json.loads(request(server, "/api/table?id=p6-r01%3Aannual.csv&strategy=cta&year=2018")[2])
        assert len(data["rows"]) == 1


def test_http_experiment_detail_compare(synthetic):
    catalog, _ = synthetic
    with running(catalog) as server:
        assert request(server, "/api/experiment?store=synthetic&id=r1")[0] == 200
        assert request(server, "/api/compare?store=synthetic&ids=r1,r2")[0] == 200
        code, _, body = request(server, "/api/compare?store=synthetic&ids=r1,r3")
        assert code == 422 and "不可比较" in body.decode()
        assert request(server, "/api/experiment?store=synthetic&id=../outside")[0] == 400


def test_public_redacts_paths_and_nested_credentials():
    assert public({"nested": {"password": "hidden"}, "path": "C:\\private\\file", "zero": 0, "null": None}) == {
        "nested": {"password": "[REDACTED]"}, "path": "[LOCAL_PATH]", "zero": 0, "null": None}
