"""Publication, idempotency, boundary and fail-closed integration tests."""

import hashlib
import json
from pathlib import Path
import socket
import struct

import pandas as pd
import pytest

from qbot.research.data.providers.local import _strict_bars
from qbot.research.errors import ProviderError
from qbot.research.data.tdx import TdxError, audit_day
from qbot.research.data.tdx_staging import (
    convert_day, frame_source_sha256, load_calendar, run_audit, staging_root,
)
from examples.research.audit_tdx_local import main


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "vipdoc"
    for market in ("sh", "sz", "bj"):
        (root / market / "lday").mkdir(parents=True)
    path = root / "sh" / "lday" / "sh600000.day"
    path.write_bytes(struct.pack("<IIIIIfII", 20200102, 1000, 1100, 900, 1050, 10500.125, 1000, 0xffffffff))
    return root, path


def item(source):
    root, path = source
    return audit_day(path, "sh", root, "2020-01-02", "2026-10-09")


def test_exact_roundtrip_hash_no_adjustments_and_original_unchanged(source, tmp_path):
    original = source[1].read_bytes()
    result = convert_day(item(source), tmp_path / "staging")
    frame = pd.read_parquet(result["parquet_file"])
    assert frame_source_sha256(frame) == hashlib.sha256(original).hexdigest()
    assert source[1].read_bytes() == original
    assert result["status"] == "CONVERTED_RAW_ONLY"
    assert not result["financial_research_eligible"]
    with pytest.raises(ProviderError, match="explicit canonical/raw/adjusted columns required"):
        _strict_bars(frame, is_index=False, benchmark_symbol="000905.SH")


def test_idempotency_retains_generation_bytes_and_mtime(source, tmp_path):
    result = convert_day(item(source), tmp_path / "staging")
    path = Path(result["parquet_file"])
    original, mtime = path.read_bytes(), path.stat().st_mtime_ns
    repeated = convert_day(item(source), tmp_path / "staging")
    assert repeated["status"] == "REUSED_VERIFIED"
    assert repeated["parquet_file"] == str(path)
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == mtime


def test_changed_source_creates_new_generation(source, tmp_path):
    first = convert_day(item(source), tmp_path / "staging")
    previous = Path(first["parquet_file"]).read_bytes()
    source[1].write_bytes(struct.pack("<IIIIIfII", 20200103, 1000, 1100, 900, 1050, 10500., 1000, 10))
    second = convert_day(item(source), tmp_path / "staging")
    assert first["parquet_file"] != second["parquet_file"]
    assert Path(first["parquet_file"]).read_bytes() == previous


@pytest.mark.parametrize("target", ["data.parquet", "receipt.json"])
def test_corrupt_existing_generation_refused_never_replaced(source, tmp_path, target):
    first = convert_day(item(source), tmp_path / "staging")
    path = Path(first["parquet_file"]).parent / target
    path.write_bytes(b"bad")
    with pytest.raises((TdxError, ValueError)):
        convert_day(item(source), tmp_path / "staging")
    assert path.read_bytes() == b"bad"


def test_atomic_publish_failure_has_no_published_generation(source, tmp_path, monkeypatch):
    import qbot.research.data.tdx_staging as module
    monkeypatch.setattr(module.os, "rename", lambda *args: (_ for _ in ()).throw(OSError("publish failed")))
    with pytest.raises(OSError, match="publish failed"):
        convert_day(item(source), tmp_path / "staging")
    artifacts = list((tmp_path / "staging").rglob("data.parquet"))
    assert len(artifacts) == 1
    assert artifacts[0].parent.name.startswith(".tmp-")


def test_validation_failure_never_publishes(source, tmp_path, monkeypatch):
    import qbot.research.data.tdx_staging as module
    monkeypatch.setattr(module, "frame_source_sha256", lambda frame: "bad")
    with pytest.raises(TdxError, match="ROUNDTRIP"):
        convert_day(item(source), tmp_path / "staging")
    assert all(p.parent.name.startswith(".tmp-") for p in (tmp_path / "staging").rglob("data.parquet"))


def test_nonempty_production_cache_root_refused(tmp_path):
    root = tmp_path / "raw"
    root.mkdir()
    original = root / "600000.SH.metadata.json"
    original.write_bytes(b"existing research history")
    with pytest.raises(TdxError, match="NONEMPTY_UNMARKED"):
        staging_root(root)
    assert original.read_bytes() == b"existing research history"


def test_output_cannot_overlap_source(source, tmp_path):
    with pytest.raises(TdxError, match="SOURCE_OUTPUT_OVERLAP"):
        run_audit(source[0], source[0] / "report")
    with pytest.raises(TdxError, match="SOURCE_OUTPUT_OVERLAP"):
        run_audit(source[0], tmp_path / "report", source[0] / "staging")


def test_full_offline_artifacts_logical_quarantine_and_exclusive_runs(source, tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network forbidden"))
    root, path = source
    bad = root / "sz" / "lday" / "sz000001.day"
    bad.write_bytes(b"truncated")
    report = tmp_path / "report"
    result = run_audit(root, report, tmp_path / "staging", run_id="test-run")
    assert result["status"] == "partial"
    directory = Path(result["report_dir"])
    expected = {"inventory.csv", "securities.csv", "coverage.csv", "anomalies.csv", "conversion_status.csv", "summary.md", "manifest.json", "verification_samples.csv", "quarantine_manifest.json"}
    assert expected <= {p.name for p in directory.iterdir()}
    saved = {p.name: p.read_bytes() for p in directory.iterdir()}
    assert result["metrics"]["discovered_day_files"] == 2
    assert result["metrics"]["valid_day_files"] == 1
    assert result["metrics"]["confirmed_a_shares"] == 0
    assert bad.read_bytes() == b"truncated"
    for name, receipt in result["artifacts"].items():
        assert hashlib.sha256(saved[name]).hexdigest() == receipt["sha256"]
    with pytest.raises(FileExistsError):
        run_audit(root, report, run_id="test-run")
    assert saved == {p.name: p.read_bytes() for p in directory.iterdir()}
    assert not (tmp_path / "processed" / "dataset.parquet").exists()


def test_missing_directory_records_failed_receipt(source, tmp_path):
    root, _ = source
    (root / "bj" / "lday").rmdir()
    with pytest.raises(TdxError, match="MISSING"):
        run_audit(root, tmp_path / "report", run_id="failed-run")
    manifest = json.loads((tmp_path / "report" / "failed-run" / "manifest.json").read_bytes())
    assert manifest["status"] == "failed"
    assert manifest["network_requests"] == 0
    assert not manifest["financial_research_eligible"]


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "..", "a\\b"])
def test_run_id_traversal_rejected(source, tmp_path, run_id):
    with pytest.raises(TdxError, match="INVALID_RUN_ID"):
        run_audit(source[0], tmp_path / "report", run_id=run_id)


@pytest.mark.parametrize("dates", [
    ["2020-01-02", "2020-01-02"], ["2020-01-03", "2020-01-02"],
    ["2020-01-04"], ["2020-01-02T12:00:00"],
])
def test_invalid_reference_calendar_refused(tmp_path, dates):
    path = tmp_path / "calendar.csv"
    pd.DataFrame({"date": dates}).to_csv(path, index=False)
    with pytest.raises(TdxError, match="INVALID_REFERENCE_CALENDAR"):
        load_calendar(path)


def test_cli_audit_only_and_convert(source, tmp_path, capsys):
    args = ["--vipdoc-root", str(source[0]), "--report-root", str(tmp_path / "report")]
    assert main(args + ["--run-id", "audit-only"]) == 0
    assert main(args + ["--convert", "--staging-root", str(tmp_path / "staging"), "--run-id", "converted"]) == 0
    assert "正式研究合格：False" in capsys.readouterr().out


def test_symbol_directory_symlink_rejected(source, tmp_path):
    staging = staging_root(tmp_path / "staging")
    (staging / "sh").mkdir()
    link = staging / "sh" / "600000.SH"
    external = tmp_path / "external"
    external.mkdir()
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("OS cannot create symbolic links")
    with pytest.raises(TdxError, match="LINK_OR_REPARSE"):
        convert_day(item(source), staging)
    assert not list(external.iterdir())


def test_duplicate_symbol_copies_not_silently_selected(source, tmp_path):
    nested = source[0] / "sh" / "lday" / "copy"
    nested.mkdir()
    (nested / source[1].name).write_bytes(source[1].read_bytes())
    result = run_audit(source[0], tmp_path / "reports", tmp_path / "staging")
    assert result["status"] == "partial"
    assert result["metrics"]["converted_files"] == 0
    anomalies = pd.read_csv(Path(result["report_dir"]) / "anomalies.csv")
    assert anomalies.code.eq("DUPLICATE_SOURCE_SYMBOL").sum() == 2
