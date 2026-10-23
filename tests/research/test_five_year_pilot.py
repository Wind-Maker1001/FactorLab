"""SYNTHETIC_TEST_ONLY: offline five-year ingestion and evidence regressions."""

import json
from pathlib import Path
import subprocess
import sys
import time

import pandas as pd
import pytest
import requests

from qbot.research.data.providers.akshare import AKShareProvider
from qbot.research.errors import CacheError
from qbot.research.exploratory import five_year as pilot
from qbot.research.exploratory.contract import manifest_version, sha256_file


DATES = ["2021-03-08", "2021-03-09", "2021-09-13", "2021-09-14",
         "2021-09-15", "2021-09-16", "2021-09-17", "2021-09-20"]


class SyntheticSDK:
    __version__ = "SYNTHETIC_TEST_ONLY"

    def stock_zh_a_hist(self, **kwargs):
        result = requests.get("https://example.invalid/pilot-stock", params=kwargs).json()
        return pd.DataFrame(result["rows"])

    def stock_zh_index_hist_csindex(self, **kwargs):
        result = requests.get("https://example.invalid/pilot-index", params=kwargs).json()
        return pd.DataFrame(result["rows"])


@pytest.fixture
def synthetic_transport(monkeypatch):
    calls, mode = [], {"value": "default"}

    def fake_request(session, method, url, **options):
        kwargs = options.get("params", {})
        calls.append((url, dict(kwargs)))
        code, adjustment = kwargs["symbol"], kwargs.get("adjust", "")
        if code == "600112" and mode["value"] != "all-valid":
            raise requests.Timeout("SYNTHETIC_TEST_ONLY historical-exit endpoint unavailable")
        dates = list(DATES)
        if code == "000002":
            dates.remove("2021-09-14")
        if code == "601059":
            dates = dates[-2:]
        if code == "000961" and mode["value"] != "all-valid":
            dates = []
        if code == "600000" and mode["value"] == "duplicate":
            dates.append(dates[-1])
        if code == "600000" and mode["value"] == "date-mismatch" and adjustment == "hfq":
            dates = dates[:-1]
        if code == "600000" and mode["value"] == "weekend":
            dates.append("2021-09-18")
        multiplier = 2 if adjustment == "hfq" else 1
        rows = [{"日期": day, "开盘": 10 * multiplier, "最高": 12 * multiplier,
                 "最低": 9 * multiplier, "收盘": 11 * multiplier,
                 "成交量": 10, "成交额": 11000, "换手率": 2}
                for day in dates]
        if code == "000905":
            for row in rows:
                row["成交额"] = 0.0011  # CSI endpoint units: CNY 100m.
        if code == "600000" and mode["value"] == "bad-units":
            for row in rows:
                row["成交额"] *= 100
        response = requests.Response()
        response.status_code = mode.get("deny_status", 200) if code == mode.get("deny_code") else 200
        response.url = requests.Request(method, url, params=kwargs).prepare().url
        response._content = json.dumps({"synthetic_only": True, "rows": rows}).encode("utf-8")
        response.headers["Content-Type"] = "application/json"
        return response

    def no_throttle(**kwargs):
        kwargs["request_interval"] = 0
        return AKShareProvider(**kwargs)

    monkeypatch.setattr(requests.sessions.Session, "request", fake_request)
    monkeypatch.setattr(pilot, "AKShareProvider", no_throttle)
    return calls, mode


def acquire(root):
    return pilot.acquire_pilot(root, research_end="2021-09-20", akshare_module=SyntheticSDK(),
                               synthetic_only=True, progress=lambda value: None)


def test_frozen_sample_cannot_replace_unavailable_names(tmp_path):
    frozen = pilot.freeze_pilot(tmp_path, synthetic_only=True)
    content = (tmp_path / "sample.json").read_bytes()
    assert frozen["symbols"] == list(pilot.DEFAULT_SYMBOLS)
    assert {value["identity_status"] for value in frozen["securities"]} == {"UNVERIFIED"}
    assert pilot.freeze_pilot(tmp_path, synthetic_only=True) == frozen
    with pytest.raises(ValueError, match="pre-frozen"):
        pilot.freeze_pilot(tmp_path, pilot.DEFAULT_SYMBOLS[:-1], synthetic_only=True)
    with pytest.raises(ValueError, match="pre-frozen"):
        pilot.freeze_pilot(tmp_path, synthetic_only=False)
    assert (tmp_path / "sample.json").read_bytes() == content


def test_partial_pilot_preserves_missing_failed_and_short_history(tmp_path, synthetic_transport):
    result = acquire(tmp_path)
    manifest = pilot.verify_pilot(result["manifest_path"], allow_synthetic=True)
    assert result["statuses"] == {"software": "SOFTWARE_SUCCESS", "data": "EXPLORATORY_DATA_AVAILABLE",
                                  "pit": "PIT_UNVERIFIED", "hfq": "HFQ_UNVERIFIED",
                                  "formal": "FORMAL_ADMISSION_BLOCKED"}
    root = Path(result["manifest_path"]).parent
    bars = pd.read_parquet(root / "bars.parquet")
    assert len(bars) == len(DATES) * len(pilot.DEFAULT_SYMBOLS)
    assert set(bars["symbol"]) == set(pilot.DEFAULT_SYMBOLS)
    missing = bars.loc[bars["symbol"].eq("000002.SZ") & bars["date"].eq("2021-09-14")].iloc[0]
    assert pd.isna(missing.raw_close) and pd.isna(missing.adj_close)
    assert missing.observation_status == "MISSING_REASON_UNKNOWN"
    assert missing.historical_security_state == "UNVERIFIED"
    row = bars.loc[bars["symbol"].eq("600000.SH")].iloc[0]
    assert row.open == row.raw_open == 10
    assert row.adj_open == 20 and row.volume == 1000 and row.amount == 11000
    assert row.turnover == pytest.approx(0.02)
    cover = {value["symbol"]: value for value in result["coverage"]}
    assert cover["601059.SH"]["warmup_observed_sessions"] == 0
    assert cover["601059.SH"]["paired_observed_research_sessions"] == 2
    assert cover["600112.SH"]["missing_research_sessions"] == 6
    assert cover["000961.SZ"]["missing_research_sessions"] == 6
    attempts = json.loads((root / "attempts.json").read_text())
    assert any(value["symbol"] == "600112.SH" and value["status"] == "DOWNLOAD_OR_VALIDATION_FAILED" for value in attempts)
    assert any(value["symbol"] == "000961.SZ" and value["status"] == "EMPTY_VENDOR_RESPONSE" for value in attempts)
    assert manifest["warmup"]["calendar_sufficient"] is False
    assert manifest["terminal_labels"]["unmatured_calendar_dates"] == DATES[-6:]
    assert manifest["verification_design"]["factor_search_performed"] is False
    assert manifest["verification_design"]["existing_2025_2026_independent_test"] is False
    assert manifest["verification_design"]["added_earlier_dates_are_forward_test"] is False
    assert all("evidence/" in item["path"] for name, item in manifest["files"].items() if ".bin" in name)
    with pytest.raises(ValueError, match="SYNTHETIC_TEST_ONLY"):
        pilot.verify_pilot(result["manifest_path"])


def test_repeated_execution_retains_receipts_and_resumes_cache(tmp_path, synthetic_transport):
    calls, mode = synthetic_transport
    mode["value"] = "all-valid"
    first = acquire(tmp_path)
    original = {path.name: path.read_bytes() for path in Path(first["manifest_path"]).parent.iterdir() if path.is_file()}
    count = len(calls)
    second = acquire(tmp_path)
    assert len(calls) == count
    assert first["manifest_path"] != second["manifest_path"]
    assert first["receipt_path"] != second["receipt_path"]
    assert {path.name: path.read_bytes() for path in Path(first["manifest_path"]).parent.iterdir() if path.is_file()} == original
    assert second["metrics"]["native_http_responses_this_run"] == 0
    pilot.verify_pilot(first["manifest_path"], allow_synthetic=True)
    pilot.verify_pilot(second["manifest_path"], allow_synthetic=True)


@pytest.mark.parametrize("mode", ["duplicate", "date-mismatch"])
def test_vendor_date_failures_are_receipted_without_replacement(tmp_path, synthetic_transport, mode):
    synthetic_transport[1]["value"] = mode
    result = acquire(tmp_path)
    attempts = json.loads((Path(result["manifest_path"]).parent / "attempts.json").read_text())
    item = next(value for value in attempts if value["symbol"] == "600000.SH")
    assert item["status"] == "DOWNLOAD_OR_VALIDATION_FAILED"
    assert item["retained_in_frozen_sample"] is True
    assert len(result["coverage"]) == 10
    assert next(value for value in result["coverage"] if value["symbol"] == "600000.SH")["missing_research_sessions"] == 6


@pytest.mark.parametrize("mode", ["bad-units", "weekend"])
def test_unit_or_calendar_failure_does_not_grant_analysis_ready_data(tmp_path, synthetic_transport, mode):
    synthetic_transport[1]["value"] = mode
    result = acquire(tmp_path)
    assert result["statuses"]["software"] == "SOFTWARE_FAILED"
    assert result["statuses"]["data"] == "EXPLORATORY_DATA_UNAVAILABLE"
    assert result["statuses"]["formal"] == "FORMAL_ADMISSION_BLOCKED"
    pilot.verify_pilot(result["manifest_path"], allow_synthetic=True)


def test_cache_source_identity_mismatch_is_auditable_not_relabelled(tmp_path, synthetic_transport):
    first = acquire(tmp_path)
    path = tmp_path / "cache" / "600000.SH.metadata.json"
    metadata = json.loads(path.read_text())
    metadata["source_version"] = "SYNTHETIC_TEST_ONLY-different-source-contract"
    path.write_text(json.dumps(metadata))
    second = acquire(tmp_path)
    attempts = json.loads((Path(second["manifest_path"]).parent / "attempts.json").read_text())
    item = next(value for value in attempts if value["symbol"] == "600000.SH")
    assert item["status"] == "DOWNLOAD_OR_VALIDATION_FAILED"
    assert "source identity mismatch" in item["reason"]
    assert json.loads(path.read_text())["source_version"] == metadata["source_version"]
    pilot.verify_pilot(first["manifest_path"], allow_synthetic=True)


def test_receipt_and_native_bytes_tampering_are_rejected(tmp_path, synthetic_transport):
    result = acquire(tmp_path)
    root = Path(result["manifest_path"]).parent
    native = next((root / "evidence" / "sources").glob("*.bin"))
    native.write_bytes(b"SYNTHETIC_TEST_ONLY corrupted bytes")
    with pytest.raises(ValueError, match="artifact hash"):
        pilot.verify_pilot(result["manifest_path"], allow_synthetic=True)


def test_qualification_upgrade_rejected_even_with_recomputed_local_hashes(tmp_path, synthetic_transport):
    result = acquire(tmp_path)
    path = Path(result["manifest_path"])
    manifest = json.loads(path.read_text())
    manifest["statuses"]["hfq"] = "CERTIFIED"
    manifest["data_version"] = manifest_version(manifest)
    path.write_text(json.dumps(manifest))
    receipt_path = Path(result["receipt_path"])
    receipt = json.loads(receipt_path.read_text())
    receipt.update(manifest_sha256=sha256_file(path), data_version=manifest["data_version"], statuses=manifest["statuses"])
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="upgrade financial qualification"):
        pilot.verify_pilot(path, allow_synthetic=True)


def test_cli_freeze_and_invalid_range_do_not_access_network(tmp_path, monkeypatch, capsys):
    def no_network(*args, **kwargs):
        raise AssertionError("network not authorized in freeze or invalid configuration")

    monkeypatch.setattr(requests.sessions.Session, "request", no_network)
    assert pilot.main(["freeze", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["pit"] is False
    with pytest.raises(ValueError, match="date range"):
        pilot.acquire_pilot(tmp_path, research_start="2021-09-20", research_end="2021-09-13")
    with pytest.raises(ValueError, match="zero or one retry"):
        pilot.acquire_pilot(tmp_path, retry_count=5)


def rebind_local_hashes(result, mutate_manifest=None):
    """An attacker can recompute local hashes; semantic evidence must still bind."""
    path = Path(result["manifest_path"])
    root = path.parent
    manifest = json.loads(path.read_text())
    for name in manifest["files"]:
        manifest["files"][name] = pilot._item(root, root / name)
    manifest["sample_sha256"] = sha256_file(root / "sample.json")
    if mutate_manifest is not None:
        mutate_manifest(manifest)
    manifest["data_version"] = manifest_version(manifest)
    path.write_text(json.dumps(manifest))
    receipt_path = Path(result["receipt_path"])
    receipt = json.loads(receipt_path.read_text())
    receipt.update(manifest_sha256=sha256_file(path), data_version=manifest["data_version"],
                   artifact_hashes={name: item["sha256"] for name, item in manifest["files"].items()},
                   statuses=manifest["statuses"])
    receipt_path.write_text(json.dumps(receipt))


@pytest.fixture
def semantic_snapshot(tmp_path, synthetic_transport):
    return acquire(tmp_path)


def test_v2_cache_metadata_is_exact_original_snapshot_bytes(semantic_snapshot):
    result = semantic_snapshot
    root = Path(result["manifest_path"]).parent
    original = root.parent.parent / "cache" / "600000.SH.metadata.json"
    copied = root / "evidence/cache/600000.SH.metadata.json"
    assert original.read_bytes() == copied.read_bytes()
    manifest = pilot.verify_pilot(result["manifest_path"], allow_synthetic=True)
    assert manifest["schema"] == "FIVE_YEAR_PILOT_MANIFEST_V3"
    assert "authorized_use" not in manifest["source_contract"]
    assert "no commercial/redistribution grant established" in manifest["source_contract"]["authorization_evidence"]


@pytest.mark.parametrize("case", ["coverage", "calendar", "bar-price", "metadata-bytes", "freeze-time", "security-state"])
def test_recomputed_local_hashes_do_not_replace_structural_evidence(semantic_snapshot, case):
    result = semantic_snapshot
    root = Path(result["manifest_path"]).parent
    if case == "coverage":
        path = root / "coverage.json"
        value = json.loads(path.read_text())
        value[0]["missing_research_sessions"] += 1
        path.write_text(json.dumps(value))
    elif case == "calendar":
        path = root / "calendar.csv"
        pd.read_csv(path).iloc[:-1].to_csv(path, index=False)
    elif case == "bar-price":
        path = root / "bars.parquet"
        frame = pd.read_parquet(path)
        selected = frame["symbol"].eq("600000.SH")
        frame.loc[selected, "close"] = frame.loc[selected, "raw_close"] = 11.1
        frame.loc[selected, "adj_close"] = 22.2
        frame.to_parquet(path, index=False)
    elif case == "metadata-bytes":
        path = root / "evidence/cache/600000.SH.metadata.json"
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        path = root / "sample.json"
        value = json.loads(path.read_text())
        if case == "freeze-time":
            value["frozen_at"] = "2099-01-01T00:00:00+00:00"
        else:
            value["securities"][0]["historical_membership"] = "CERTIFIED"
        path.write_text(json.dumps(value))
    rebind_local_hashes(result)
    with pytest.raises(ValueError):
        pilot.verify_pilot(result["manifest_path"], allow_synthetic=True)


@pytest.mark.parametrize("case", ["request-role", "response-hash", "body-price", "http-status"])
def test_recomputed_hashes_cannot_change_native_request_or_response_binding(semantic_snapshot, case):
    result = semantic_snapshot
    root = Path(result["manifest_path"]).parent
    request = next((root / "evidence/sources").glob("*-stock_zh_a_hist.request.json"))
    entry = json.loads(request.read_text())
    if case == "request-role":
        # Directory enumeration order differs across filesystems: always
        # mutate the actual role, including when the first entry is HFQ.
        previous_role = entry["interface_arguments"]["adjust"]
        assert previous_role in {"", "hfq"}
        entry["interface_arguments"]["adjust"] = "hfq" if previous_role == "" else ""
        assert entry["interface_arguments"]["adjust"] != previous_role
    elif case == "response-hash":
        entry["response_sha256"] = "0" * 64
    elif case == "http-status":
        entry["status_code"] = 500
    else:
        response = root / "evidence" / entry["response_file"]
        payload = json.loads(response.read_text())
        payload["rows"][0]["收盘"] += 0.1
        response.write_text(json.dumps(payload))
        entry["response_sha256"] = sha256_file(response)
    request.write_text(json.dumps(entry))
    rebind_local_hashes(result)
    with pytest.raises(ValueError):
        pilot.verify_pilot(result["manifest_path"], allow_synthetic=True)


@pytest.mark.parametrize("case", ["terminal-labels", "independent-test", "remove-required-artifact"])
def test_recomputed_manifest_cannot_upgrade_validation_design(semantic_snapshot, case):
    def mutate(value):
        if case == "terminal-labels":
            value["terminal_labels"]["prices_after_research_end_fetched"] = True
        elif case == "independent-test":
            value["verification_design"]["existing_2025_2026_independent_test"] = True
        else:
            del value["files"]["bars.parquet"]

    rebind_local_hashes(semantic_snapshot, mutate)
    with pytest.raises(ValueError):
        pilot.verify_pilot(semantic_snapshot["manifest_path"], allow_synthetic=True)


def test_stdlib_memory_reports_bytes_or_explicit_unavailability():
    memory = pilot._stdlib_memory()
    for key in ("process_rss_bytes", "process_peak_rss_bytes"):
        assert memory[key] is None or isinstance(memory[key], int) and memory[key] > 0
    assert memory["memory_method"]


def test_intraday_calendar_cannot_be_silently_normalized(semantic_snapshot):
    root = Path(semantic_snapshot["manifest_path"]).parent
    path = root / "calendar.csv"
    frame = pd.read_csv(path)
    frame["date"] = frame["date"] + "T10:00:00"
    frame.to_csv(path, index=False)
    rebind_local_hashes(semantic_snapshot)
    with pytest.raises(ValueError, match="midnight"):
        pilot.verify_pilot(semantic_snapshot["manifest_path"], allow_synthetic=True)


def test_initial_evidence_corruption_creates_a_blocked_failure_receipt(tmp_path, synthetic_transport):
    pilot.freeze_pilot(tmp_path, synthetic_only=True)
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "0001-broken.bin").write_bytes(b"SYNTHETIC_TEST_ONLY corrupted source")
    (sources / "0001-broken.request.json").write_text(json.dumps({
        "response_file": "sources/0001-broken.bin", "response_sha256": "0" * 64,
        "url": "https://example.invalid/broken"}))
    result = acquire(tmp_path)
    assert result["statuses"]["software"] == "SOFTWARE_FAILED"
    assert result["statuses"]["data"] == "EXPLORATORY_DATA_UNAVAILABLE"
    manifest = pilot.verify_pilot(result["manifest_path"], allow_synthetic=True)
    assert "source hash mismatch" in manifest["fatal_error"]
    attempts = json.loads((Path(result["manifest_path"]).parent / "attempts.json").read_text())
    assert len(attempts) == 10 and all(value["retained_in_frozen_sample"] for value in attempts)


def test_legacy_v1_receipts_remain_replayable_without_rewriting_evidence(semantic_snapshot):
    result = semantic_snapshot
    root = Path(result["manifest_path"]).parent
    path = root / "bars.parquet"
    frame = pd.read_parquet(path)
    frame["calculation_version"] = "five-year-minimal-ingestion-v1"
    frame.to_parquet(path, index=False)
    for path in (root / "evidence/cache").glob("*.metadata.json"):
        path.write_text(json.dumps(json.loads(path.read_text()), indent=4))

    def legacy(value):
        value["schema"] = "FIVE_YEAR_PILOT_MANIFEST_V1"
        value["version"] = "five-year-minimal-ingestion-v1"
        value.pop("benchmark_provenance")

    rebind_local_hashes(result, legacy)
    receipt_path = Path(result["receipt_path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["schema"] = "FIVE_YEAR_PILOT_RECEIPT_V1"
    receipt_path.write_text(json.dumps(receipt))
    original = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    verified = pilot.verify_pilot(result["manifest_path"], allow_synthetic=True, with_verification=True)
    assert verified["manifest"]["schema"] == "FIVE_YEAR_PILOT_MANIFEST_V1"
    assert verified["verification"]["calendar_origin"] == "CACHE_ONLY_NOT_NATIVE_VERIFIED"
    assert {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()} == original


@pytest.mark.parametrize("http_status", [401, 403, 429])
def test_explicit_supplier_denial_stops_http_even_with_provider_retry_one(tmp_path, synthetic_transport, http_status):
    calls, mode = synthetic_transport
    mode.update(deny_code="600000", deny_status=http_status)
    result = pilot.acquire_pilot(tmp_path, research_end="2021-09-20", akshare_module=SyntheticSDK(),
                                  synthetic_only=True, retry_count=1, progress=lambda value: None)
    assert len(calls) == 1  # No immediate retry, other security URL, benchmark URL or proxy.
    manifest = pilot.verify_pilot(result["manifest_path"], allow_synthetic=True)
    assert manifest["supplier_access_limit"]["status_code"] == http_status
    root = Path(result["manifest_path"]).parent
    attempts = json.loads((root / "attempts.json").read_text())
    securities = [value for value in attempts if value["symbol"] != "000905.SH"]
    assert [value["symbol"] for value in securities] == list(pilot.DEFAULT_SYMBOLS)
    assert securities[0]["status"] == "DOWNLOAD_OR_VALIDATION_FAILED"
    assert all(value["status"] == "NOT_ATTEMPTED_ACCESS_LIMIT" for value in securities[1:])
    assert all(value["native_http_responses"] == 0 for value in securities[1:])
    assert manifest["metrics"]["not_attempted_access_limit"] == 9
    receipts = list((root / "evidence/sources").glob("*.request.json"))
    assert len(receipts) == 1
    entry = json.loads(receipts[0].read_text())
    assert entry["status_code"] == http_status
    assert sha256_file(root / "evidence" / entry["response_file"]) == entry["response_sha256"]


def test_later_supplier_denial_keeps_prior_data_and_reads_local_benchmark_cache(tmp_path, synthetic_transport):
    calls, mode = synthetic_transport
    benchmark_cache = tmp_path / "prior-authorized-benchmark"
    benchmark_evidence = tmp_path / "prior-benchmark-native-evidence"
    benchmark_evidence.mkdir()
    recorder = pilot.RecordingAKShare(SyntheticSDK(), benchmark_evidence)
    provider = AKShareProvider(akshare_module=recorder, request_interval=0, retry_count=0)
    pilot.ParquetBarCache(benchmark_cache).update_from_provider(
        provider, "000905.SH", "2021-03-08", "2021-09-20", is_index=True)
    calls.clear()
    mode.update(deny_code="300750", deny_status=429)
    result = pilot.acquire_pilot(tmp_path / "pilot", research_end="2021-09-20", akshare_module=SyntheticSDK(),
                                  synthetic_only=True, retry_count=1, benchmark_cache_root=benchmark_cache,
                                  benchmark_evidence_root=benchmark_evidence,
                                  progress=lambda value: None)
    assert len(calls) == 9  # Four paired successes, then one preserved denial.
    assert result["statuses"]["data"] == "EXPLORATORY_DATA_AVAILABLE"
    assert result["metrics"]["successful_research_securities"] == 4
    assert len(result["coverage"]) == 10
    assert result["metrics"]["not_attempted_access_limit"] == 5
    root = Path(result["manifest_path"]).parent
    assert len(pd.read_parquet(root / "bars.parquet")) == 80
    pilot.verify_pilot(result["manifest_path"], allow_synthetic=True)


def test_concurrent_entry_fails_before_recorder_http_or_evidence_mutation(semantic_snapshot, synthetic_transport, tmp_path, monkeypatch):
    """A real second process holds the whole acquire entry, across Windows/POSIX."""
    first = semantic_snapshot
    root = Path(first["manifest_path"]).parents[2]
    calls = synthetic_transport[0]
    calls_before = len(calls)
    sources_before = {path.relative_to(root).as_posix(): path.read_bytes()
                      for path in (root / "sources").rglob("*") if path.is_file()}
    runs_before = {path.name for path in (root / "runs").iterdir()}
    first_bytes = {path.relative_to(Path(first["manifest_path"]).parent).as_posix(): path.read_bytes()
                   for path in Path(first["manifest_path"]).parent.rglob("*") if path.is_file()}
    ready = tmp_path / "pilot-lock-ready"
    script = """
from pathlib import Path
import sys
from qbot.research.exploratory import five_year as pilot
def hold(*args, **kwargs):
    Path(sys.argv[2]).write_text('whole pilot entry locked', encoding='utf-8')
    sys.stdin.readline()
    return {}
pilot._acquire_pilot_locked = hold
pilot.acquire_pilot(Path(sys.argv[1]))
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(root), str(ready)],
                               cwd=str(Path(__file__).resolve().parents[2]), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "child did not acquire the whole pilot root lock"

        def forbidden_recorder(*args, **kwargs):
            raise AssertionError("competing acquire constructed a recorder before obtaining the root lock")

        with monkeypatch.context() as patch:
            patch.setattr(pilot, "_AccessLimitedRecorder", forbidden_recorder)
            with pytest.raises(CacheError, match="writer lock timeout"):
                acquire(root)
        assert len(calls) == calls_before
        assert {path.name for path in (root / "runs").iterdir()} == runs_before
        assert {path.relative_to(root).as_posix(): path.read_bytes()
                for path in (root / "sources").rglob("*") if path.is_file()} == sources_before
    finally:
        try:
            stdout, stderr = process.communicate(input="release\n", timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
    assert process.returncode == 0, stdout + stderr
    assert pilot.PILOT_LOCK_SYMBOL not in pilot.DEFAULT_SYMBOLS + ("000905.SH",)
    assert not (root / "cache" / (pilot.PILOT_LOCK_SYMBOL + ".metadata.json")).exists()
    second = acquire(root)  # OS ownership released; the next entry resumes safely.
    assert second["manifest_path"] != first["manifest_path"]
    assert {path.relative_to(root).as_posix(): path.read_bytes()
            for path in (root / "sources").rglob("*") if path.is_file()} == sources_before
    assert {path.relative_to(Path(first["manifest_path"]).parent).as_posix(): path.read_bytes()
            for path in Path(first["manifest_path"]).parent.rglob("*") if path.is_file()} == first_bytes
    pilot.verify_pilot(first["manifest_path"], allow_synthetic=True)
    pilot.verify_pilot(second["manifest_path"], allow_synthetic=True)


class NativeLayoutSDK(SyntheticSDK):
    """Local mock SDK with the production Eastmoney/CSI response layouts."""

    def stock_zh_a_hist(self, **kwargs):
        code = kwargs["symbol"]
        params = {"secid": ("1." if code.startswith(("5", "6", "9")) else "0.") + code,
                  "fqt": "2" if kwargs["adjust"] == "hfq" else "0", "klt": "101",
                  "beg": kwargs["start_date"], "end": kwargs["end_date"]}
        payload = requests.get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                               params=params, timeout=kwargs["timeout"]).json()
        if payload["data"] is None:
            return pd.DataFrame()
        return pd.DataFrame([row.split(",") for row in payload["data"]["klines"]],
                            columns=("日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "振幅", "涨跌幅", "涨跌额", "换手率"))

    def stock_zh_index_hist_csindex(self, **kwargs):
        payload = requests.get("https://www.csindex.com.cn/csindex-home/perf/index-perf",
                               params={"indexCode": kwargs["symbol"], "startDate": kwargs["start_date"], "endDate": kwargs["end_date"]}).json()
        frame = pd.DataFrame(payload["data"], columns=pilot.CSI_NATIVE_FIELDS)
        frame.columns = pilot.CSI_VENDOR_COLUMNS
        return frame


@pytest.fixture
def native_transport(synthetic_transport, monkeypatch):
    """No network: reproduce actual supplier JSON layouts with local fake values."""
    calls, mode = [], {"stock_null": False}

    def mocked_native_request(session, method, url, **options):
        params = options["params"]
        calls.append((url, dict(params)))
        if url == "https://www.csindex.com.cn/csindex-home/perf/index-perf":
            rows = [dict(zip(pilot.CSI_NATIVE_FIELDS, (day.replace("-", ""), "000905", "fixture", "fixture", "fixture", "fixture",
                                                      10., 12., 9., 11., 0., 0., 10., 0.0011, 500., 10.))) for day in mode.get("index_dates", DATES)]
            payload = {"code": "200", "msg": "Success", "data": rows, "success": True}
        else:
            assert url == "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            if mode["stock_null"]:
                payload = {"rc": 0, "data": None}
            else:
                multiplier = 2 if params["fqt"] == "2" else 1
                rows = [",".join(str(value) for value in (day, 10 * multiplier, 11 * multiplier, 12 * multiplier, 9 * multiplier,
                                                           10, 11000, 0, 0, 0, 2)) for day in DATES]
                payload = {"rc": 0, "data": {"code": params["secid"][2:], "market": int(params["secid"][0]), "klines": rows}}
        response = requests.Response()
        response.status_code = 200
        response.url = requests.Request(method, url, params=params).prepare().url
        response._content = json.dumps(payload).encode("utf-8")
        response.headers["Content-Type"] = "application/json"
        return response

    monkeypatch.setattr(requests.sessions.Session, "request", mocked_native_request)
    return calls, mode


@pytest.fixture
def native_null_snapshot(tmp_path, native_transport):
    """Exercise the real data:null parser; all network/body bytes are mocked."""
    native_transport[1]["stock_null"] = True
    result = pilot.acquire_pilot(tmp_path / "native-null-pilot", research_end="2021-09-20",
                                  akshare_module=NativeLayoutSDK(), progress=lambda value: None)
    assert len(native_transport[0]) == 21
    return result


def test_native_http_200_data_null_remains_empty_unavailable_with_valid_receipt(native_null_snapshot):
    result = native_null_snapshot
    assert result["statuses"]["software"] == "SOFTWARE_SUCCESS"
    assert result["statuses"]["data"] == "EXPLORATORY_DATA_UNAVAILABLE"
    assert result["statuses"]["formal"] == "FORMAL_ADMISSION_BLOCKED"
    root = Path(result["manifest_path"]).parent
    attempts = json.loads((root / "attempts.json").read_text())
    assert all(item["status"] == "EMPTY_VENDOR_RESPONSE" for item in attempts if item["symbol"] != "000905.SH")
    bars = pd.read_parquet(root / "bars.parquet")
    assert len(bars) == 80 and bars["raw_close"].isna().all() and bars["adj_close"].isna().all()
    assert bars["observation_status"].eq("MISSING_REASON_UNKNOWN").all()
    assert all(item["paired_observed_research_sessions"] == 0 for item in result["coverage"])
    pilot.verify_pilot(result["manifest_path"])


def test_native_data_null_cannot_prove_a_nonempty_cache_after_local_hash_rebinding(native_null_snapshot):
    result = native_null_snapshot
    root = Path(result["manifest_path"]).parent
    row = {"date": pd.Timestamp("2021-09-13"), "symbol": "600000.SH",
           "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0,
           "volume": 1000.0, "amount": 11000.0, "turnover": 0.02, "adj_factor": 2.0}
    for field in ("open", "high", "low", "close"):
        row["raw_" + field], row["adj_" + field] = row[field], row[field] * 2
    cached_path = root / "evidence/cache/600000.SH.parquet"
    pilot.canonicalize_bars(pd.DataFrame([row])).to_parquet(cached_path, index=False)
    metadata_path = root / "evidence/cache/600000.SH.metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata.update(content_sha256=sha256_file(cached_path), row_count=1,
                    data_start="2021-09-13", data_end="2021-09-13")
    metadata_path.write_text(json.dumps(metadata))
    attempts_path = root / "attempts.json"
    attempts = json.loads(attempts_path.read_text())
    attempts[0].update(cache_data_sha256=sha256_file(cached_path),
                       cache_metadata_sha256=sha256_file(metadata_path))
    attempts_path.write_text(json.dumps(attempts))
    (root / "attempt-01.json").write_text(json.dumps(attempts[0]))
    rebind_local_hashes(result)
    with pytest.raises(ValueError, match="cached dates are absent from native source response"):
        pilot.verify_pilot(result["manifest_path"])


@pytest.fixture
def native_csi_snapshot(tmp_path, native_transport):
    return pilot.acquire_pilot(tmp_path / "native-csi-layout", research_end="2021-09-20",
                               akshare_module=NativeLayoutSDK(), progress=lambda value: None)


def test_native_csi_layout_binds_dates_prices_units_and_calendar(native_csi_snapshot):
    result = native_csi_snapshot
    report = pilot.verify_pilot(result["manifest_path"], with_verification=True)
    manifest = report["manifest"]
    assert report["verification"]["calendar_origin"] == "NATIVE_RESPONSE_RECONSTRUCTED"
    assert report["verification"]["independent_exchange_calendar_completeness"] == "UNVERIFIED"
    assert manifest["benchmark_provenance"]["status"] == "NATIVE_RESPONSE_RECONSTRUCTED"
    assert len(manifest["benchmark_provenance"]["native_response_sha256"]) == 1
    benchmark = pd.read_parquet(Path(result["manifest_path"]).parent / "benchmark.parquet")
    assert benchmark["date"].dt.strftime("%Y-%m-%d").tolist() == DATES
    assert benchmark["volume"].eq(10).all() and benchmark["amount"].eq(110000).all()
    assert benchmark["turnover"].isna().all() and benchmark["adj_factor"].eq(1).all()
    for field in ("open", "high", "low", "close"):
        assert benchmark[field].equals(benchmark["raw_" + field])
        assert benchmark[field].equals(benchmark["adj_" + field])


def _replace_benchmark_products(result, benchmark):
    root = Path(result["manifest_path"]).parent
    benchmark.to_parquet(root / "benchmark.parquet", index=False)
    cached = root / "evidence/cache/000905.SH.parquet"
    benchmark.to_parquet(cached, index=False)
    metadata_path = root / "evidence/cache/000905.SH.metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata.update(content_sha256=sha256_file(cached), row_count=len(benchmark),
                    data_start=benchmark["date"].min().strftime("%Y-%m-%d"), data_end=benchmark["date"].max().strftime("%Y-%m-%d"))
    metadata_path.write_text(json.dumps(metadata))
    attempts_path = root / "attempts.json"
    attempts = json.loads(attempts_path.read_text())
    index = next(item for item in attempts if item["symbol"] == "000905.SH")
    index.update(cache_metadata_sha256=sha256_file(metadata_path), observed_rows=len(benchmark))
    attempts_path.write_text(json.dumps(attempts))


@pytest.mark.parametrize("case", ["price", "extra-session"])
def test_rehashed_benchmark_cache_and_calendar_must_match_fixed_native_response(native_csi_snapshot, case):
    result = native_csi_snapshot
    root = Path(result["manifest_path"]).parent
    benchmark = pd.read_parquet(root / "benchmark.parquet")
    mutate = None
    if case == "price":
        for field in ("close", "raw_close", "adj_close"):
            benchmark[field] = 11.1
    else:
        extra = benchmark.iloc[[0]].copy()
        extra["date"] = pd.Timestamp("2021-03-10")
        benchmark = pd.concat([benchmark, extra]).sort_values(["date", "symbol"]).reset_index(drop=True)
        sessions = pd.DatetimeIndex(benchmark["date"])
        pd.DataFrame({"date": sessions.strftime("%Y-%m-%d")}).to_csv(root / "calendar.csv", index=False)
        old = pd.read_parquet(root / "bars.parquet")
        frame = pilot.reindex_bars_to_calendar(old[list(pilot.CANONICAL_BAR_COLUMNS)], sessions, pilot.DEFAULT_SYMBOLS)
        frame["observation_status"] = frame["raw_close"].map(lambda value: "MISSING_REASON_UNKNOWN" if pd.isna(value) else "OBSERVED_VENDOR_BAR")
        frame["historical_security_state"], frame["hfq_qualification"] = "UNVERIFIED", "HFQ_UNVERIFIED"
        frame["source_version"], frame["calculation_version"] = old["source_version"].iloc[0], pilot.VERSION
        frame.to_parquet(root / "bars.parquet", index=False)
        coverage, annual = pilot._coverage(frame, sessions, pilot.DEFAULT_SYMBOLS, pd.Timestamp("2021-09-13"), pd.Timestamp("2021-09-20"))
        (root / "coverage.json").write_text(json.dumps(coverage))
        pd.DataFrame(annual).to_csv(root / "annual_coverage.csv", index=False)

        def mutate(value):
            value["metrics"]["panel_rows"] = len(frame)
            value["warmup"]["available_calendar_sessions"] = 3

    _replace_benchmark_products(result, benchmark)
    rebind_local_hashes(result, mutate)
    with pytest.raises(ValueError, match="structured pilot snapshot mismatch"):
        pilot.verify_pilot(result["manifest_path"])


@pytest.mark.parametrize("case", ["url", "http-status", "period", "index-code", "body-price"])
def test_benchmark_native_url_status_layout_and_body_cannot_be_rebound_to_cache(native_csi_snapshot, case):
    result = native_csi_snapshot
    root = Path(result["manifest_path"]).parent
    index_path = root / "benchmark_sources.json"
    index = json.loads(index_path.read_text())
    receipt_path, response_path = root / index[0]["receipt"]["path"], root / index[0]["response"]["path"]
    entry = json.loads(receipt_path.read_text())
    if case == "url":
        entry["url"] = entry["url"].replace("www.csindex.com.cn", "example.invalid")
    elif case == "http-status":
        entry["status_code"] = 403
    elif case == "period":
        entry["interface_arguments"]["period"] = "weekly"
    else:
        payload = json.loads(response_path.read_text())
        if case == "index-code":
            payload["data"][0]["indexCode"] = "000300"
        else:
            payload["data"][0]["close"] = 11.1
        response_path.write_text(json.dumps(payload))
        entry["response_sha256"] = sha256_file(response_path)
    receipt_path.write_text(json.dumps(entry))
    index[0] = {"receipt": pilot._item(root, receipt_path), "response": pilot._item(root, response_path)}
    index_path.write_text(json.dumps(index))

    def mutate(value):
        value["benchmark_provenance"]["native_response_sha256"] = [sha256_file(response_path)]

    rebind_local_hashes(result, mutate)
    with pytest.raises(ValueError):
        pilot.verify_pilot(result["manifest_path"])


def test_benchmark_cache_without_native_evidence_cannot_supply_calendar(tmp_path, native_transport):
    cache = tmp_path / "unproved-cache"
    provider = AKShareProvider(akshare_module=NativeLayoutSDK(), request_interval=0, retry_count=0)
    pilot.ParquetBarCache(cache).update_from_provider(provider, "000905.SH", "2021-03-08", "2021-09-20", is_index=True)
    result = pilot.acquire_pilot(tmp_path / "pilot", research_end="2021-09-20", benchmark_cache_root=cache,
                                  akshare_module=NativeLayoutSDK(), progress=lambda value: None)
    report = pilot.verify_pilot(result["manifest_path"], with_verification=True)
    assert result["statuses"]["software"] == "SOFTWARE_SUCCESS"
    assert result["statuses"]["data"] == "EXPLORATORY_DATA_UNAVAILABLE"
    assert report["verification"]["calendar_origin"] == "UNAVAILABLE"
    assert report["manifest"]["calendar_status"] == "CALENDAR_UNAVAILABLE"
    assert report["manifest"]["benchmark_provenance"]["status"] == "UNAVAILABLE"
    root = Path(result["manifest_path"]).parent
    assert pd.read_csv(root / "calendar.csv").empty
    assert json.loads((root / "benchmark_sources.json").read_text()) == []


def test_v3_calendar_scope_cannot_claim_independent_completeness(native_csi_snapshot):
    def mutate(value):
        value["benchmark_provenance"]["calendar_scope"] = "INDEPENDENT_COMPLETE_CALENDAR_CERTIFIED"

    rebind_local_hashes(native_csi_snapshot, mutate)
    with pytest.raises(ValueError, match="provenance/units/status"):
        pilot.verify_pilot(native_csi_snapshot["manifest_path"])


def test_legacy_schema_cannot_self_assert_native_benchmark_qualification(native_csi_snapshot):
    result = native_csi_snapshot
    root = Path(result["manifest_path"]).parent
    bars = pd.read_parquet(root / "bars.parquet")
    bars["calculation_version"] = "five-year-minimal-ingestion-v2"
    bars.to_parquet(root / "bars.parquet", index=False)

    def mutate(value):
        value["schema"] = "FIVE_YEAR_PILOT_MANIFEST_V2"
        value["version"] = "five-year-minimal-ingestion-v2"

    rebind_local_hashes(result, mutate)
    receipt_path = Path(result["receipt_path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["schema"] = "FIVE_YEAR_PILOT_RECEIPT_V2"
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="legacy benchmark provenance is cache-only"):
        pilot.verify_pilot(result["manifest_path"], with_verification=True)


def test_warmup_only_native_benchmark_failure_clears_calendar_after_assignment(tmp_path, native_transport):
    """Valid CSI bytes can reconstruct a calendar that has no research sessions."""
    native_transport[1]["index_dates"] = DATES[:2]
    result = pilot.acquire_pilot(tmp_path / "warmup-only", research_end="2021-09-20",
                                  akshare_module=NativeLayoutSDK(), progress=lambda value: None)
    root = Path(result["manifest_path"]).parent
    report = pilot.verify_pilot(result["manifest_path"], with_verification=True)
    manifest = report["manifest"]
    attempts = json.loads((root / "attempts.json").read_text())
    benchmark_attempt = next(item for item in attempts if item["symbol"] == "000905.SH")
    # This reason occurs after TradingCalendar assignment, not source decoding.
    assert benchmark_attempt["status"] == "BENCHMARK_FAILED"
    assert "benchmark has no actual research sessions" in benchmark_attempt["reason"]
    assert manifest["calendar_status"] == "CALENDAR_UNAVAILABLE"
    assert manifest["benchmark_provenance"]["status"] == "UNAVAILABLE"
    assert report["verification"]["calendar_origin"] == "UNAVAILABLE"
    assert result["statuses"]["software"] == "SOFTWARE_SUCCESS"
    assert result["statuses"]["data"] == "EXPLORATORY_DATA_UNAVAILABLE"
    assert result["statuses"]["formal"] == "FORMAL_ADMISSION_BLOCKED"
    assert pd.read_parquet(root / "benchmark.parquet").empty
    assert pd.read_csv(root / "calendar.csv").empty
    assert result["coverage"] == []
    # The valid original pre-window response stays available as evidence.
    records = pilot._read_archived_benchmark_records(root, manifest)
    rebuilt = pilot._reconstruct_benchmark(records, pd.Timestamp("2021-03-08"), pd.Timestamp("2021-09-20"),
                                           manifest["retrieved_at"], False)
    assert rebuilt["date"].dt.strftime("%Y-%m-%d").tolist() == DATES[:2]


@pytest.mark.parametrize("http_status", [401, 403, 429])
def test_later_explicit_run_can_recover_after_stock_access_denial(tmp_path, synthetic_transport, http_status):
    calls, mode = synthetic_transport
    mode.update(value="all-valid", deny_code="600000", deny_status=http_status)
    first = pilot.acquire_pilot(tmp_path, research_end="2021-09-20", akshare_module=SyntheticSDK(),
                                 synthetic_only=True, retry_count=1, progress=lambda value: None)
    assert len(calls) == 1
    old_sources = {path.relative_to(tmp_path).as_posix(): path.read_bytes()
                   for path in (tmp_path / "sources").rglob("*") if path.is_file()}
    first_root = Path(first["manifest_path"]).parent
    old_run = {path.relative_to(first_root).as_posix(): path.read_bytes()
               for path in first_root.rglob("*") if path.is_file()}
    old_receipt_path = next((tmp_path / "sources").glob("*.request.json"))
    old_receipt = json.loads(old_receipt_path.read_text())
    assert old_receipt["status_code"] == http_status
    mode.pop("deny_code")  # Explicit caller invocation after supplier recovery.
    second = acquire(tmp_path)
    assert len(calls) == 22  # One prior denial; twenty stock requests + one CSI success.
    assert second["statuses"]["data"] == "EXPLORATORY_DATA_AVAILABLE"
    assert second["metrics"]["not_attempted_access_limit"] == 0
    assert second["metrics"]["native_http_responses_this_run"] == 21
    assert second["metrics"]["successful_research_securities"] == 10
    assert pilot.verify_pilot(second["manifest_path"], allow_synthetic=True)["supplier_access_limit"] is None
    receipts = [json.loads(path.read_text()) for path in (tmp_path / "sources").glob("*.request.json")]
    same_url = [entry for entry in receipts if entry["url"] == old_receipt["url"]]
    assert [entry["status_code"] for entry in sorted(same_url, key=lambda entry: entry["retrieved_at"])] == [http_status, 200]
    assert len({entry["response_file"] for entry in same_url}) == 2
    assert all((tmp_path / relative).read_bytes() == content for relative, content in old_sources.items())
    assert all((first_root / relative).read_bytes() == content for relative, content in old_run.items())
    pilot.verify_pilot(first["manifest_path"], allow_synthetic=True)


def test_later_explicit_run_rebuilds_csi_from_latest_success_after_429(tmp_path, synthetic_transport):
    calls, mode = synthetic_transport
    mode.update(value="all-valid", deny_code="000905", deny_status=429)
    first = pilot.acquire_pilot(tmp_path, research_end="2021-09-20", akshare_module=SyntheticSDK(),
                                 synthetic_only=True, retry_count=1, progress=lambda value: None)
    assert len(calls) == 21  # Twenty stocks, then one CSI denial with no immediate retry.
    first_root = Path(first["manifest_path"]).parent
    old_sources = {path.relative_to(tmp_path).as_posix(): path.read_bytes()
                   for path in (tmp_path / "sources").rglob("*") if path.is_file()}
    old_run = {path.relative_to(first_root).as_posix(): path.read_bytes()
               for path in first_root.rglob("*") if path.is_file()}
    assert first["statuses"]["data"] == "EXPLORATORY_DATA_UNAVAILABLE"
    mode.pop("deny_code")
    second = acquire(tmp_path)
    assert len(calls) == 22  # Stock cache resumes; the new run issues one ordinary CSI request.
    assert second["metrics"]["native_http_responses_this_run"] == 1
    manifest = pilot.verify_pilot(second["manifest_path"], allow_synthetic=True)
    assert manifest["supplier_access_limit"] is None
    assert manifest["benchmark_provenance"]["status"] == "NATIVE_RESPONSE_RECONSTRUCTED"
    records = pilot._read_archived_benchmark_records(Path(second["manifest_path"]).parent, manifest)
    assert len(records) == 1 and records[0]["entry"]["status_code"] == 200
    assert manifest["benchmark_provenance"]["native_response_sha256"] == [pilot.sha256(records[0]["body"])]
    assert all((tmp_path / relative).read_bytes() == content for relative, content in old_sources.items())
    assert all((first_root / relative).read_bytes() == content for relative, content in old_run.items())
    pilot.verify_pilot(first["manifest_path"], allow_synthetic=True)
