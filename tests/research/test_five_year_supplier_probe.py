"""SYNTHETIC_TEST_ONLY fixed source layouts and coverage denominators."""

import json

import pandas as pd
import pytest
import requests

from examples.research.five_year_supplier_probe import response_rows
from examples.research.summarize_five_year_pilot import coverage_table, verified_benchmark_provenance
from examples.research import five_year_supplier_probe as probe_module
from qbot.research.exploratory.five_year import freeze_pilot


def tencent(rows, key="day"):
    return json.dumps({"code": 0, "data": {"sh600000": {key: rows}}}).encode("utf-8")


ROW = ["2021-09-13", "10", "11", "12", "9", "100"]


def test_native_tencent_hfq_key_must_be_explicit():
    assert response_rows(tencent([ROW]), "TENCENT", "600000.SH", "RAW") == ["2021-09-13"]
    assert response_rows(tencent([ROW], "hfqday"), "TENCENT", "600000.SH", "HFQ") == ["2021-09-13"]
    with pytest.raises(ValueError, match="price basis absent"):
        response_rows(tencent([ROW]), "TENCENT", "600000.SH", "HFQ")
    with pytest.raises(ValueError, match="identity absent"):
        response_rows(b'{"code":0,"data":{"sz000001":{}}}', "TENCENT", "600000.SH", "RAW")


@pytest.mark.parametrize("rows,pattern", [
    ([ROW, ROW], "duplicate"),
    ([["2021-09-18"] + ROW[1:]], "session"),
    ([["2021-09-13T00:00:00+08:00"] + ROW[1:]], "canonical daily"),
    ([[ROW[0], "10", "15", "12", "9", "100"]], "OHLC relationship"),
    ([[ROW[0], "10", "NaN", "12", "9", "100"]], "OHLC"),
])
def test_invalid_native_tencent_observations_fail(rows, pattern):
    with pytest.raises(ValueError, match=pattern):
        response_rows(tencent(rows), "TENCENT", "600000.SH", "RAW")


def test_fixed_sohu_wrapper_identity_and_shape():
    row = ["2021-09-13", "10", "11", "1", "10%", "9", "12", "100", "1", "2%"]
    native = ("historySearchHandler(" + json.dumps([{"status": 0, "code": "cn_600000", "hq": [row]}]) + ");").encode("gbk")
    assert response_rows(native, "SOHU", "600000.SH", "RAW") == ["2021-09-13"]
    with pytest.raises(ValueError, match="identity"):
        response_rows(native, "SOHU", "688981.SH", "RAW")
    drift = native.replace(b'"2%"', b'"2%", "UNKNOWN_EXTRA_COLUMN"')
    with pytest.raises(ValueError, match="layout"):
        response_rows(drift, "SOHU", "600000.SH", "RAW")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        response_rows(b"arbitraryCallback(__import__('os'))", "SOHU", "600000.SH", "RAW")


def test_monthly_coverage_uses_actual_sessions_retains_absent_security():
    dates = pd.to_datetime(["2021-09-13", "2021-09-14", "2021-10-08"])
    row = {prefix + field: 10 for prefix in ("raw_", "adj_")
           for field in ("open", "high", "low", "close")}
    bars = pd.DataFrame([{**row, "date": dates[0], "symbol": "600000.SH"}])
    result = coverage_table(bars, dates, ["600000.SH", "000961.SZ"], "2021-09-13", "2021-10-08")
    first = result.loc[result.symbol.eq("600000.SH") & result.month.eq("2021-09")].iloc[0]
    assert first.expected_actual_sessions == 2 and first.paired_observed_sessions == 1
    assert first.coverage_ratio == 0.5
    assert result.loc[result.symbol.eq("000961.SZ"), "paired_observed_sessions"].sum() == 0
    assert set(result.missing_cause) == {"UNKNOWN_UNVERIFIED"}


@pytest.mark.parametrize("version", ["V1", "V2"])
def test_legacy_publication_cannot_promote_self_asserted_native_calendar(version):
    claimed = {"status": "NATIVE_RESPONSE_RECONSTRUCTED", "native_response_sha256": ["untrusted"]}
    checked = {"manifest": {"schema": "FIVE_YEAR_PILOT_MANIFEST_" + version,
                            "benchmark_provenance": claimed},
               "verification": {"calendar_origin": "CACHE_ONLY_NOT_NATIVE_VERIFIED"}}
    result = verified_benchmark_provenance(checked)
    assert result["status"] == "CACHE_ONLY_NOT_NATIVE_VERIFIED"
    assert "native_response_sha256" not in result
    assert checked["manifest"]["benchmark_provenance"] == claimed


def test_supplier_denials_stop_all_remaining_requests_and_keep_bytes(tmp_path, monkeypatch):
    checkout = tmp_path / "synthetic-checkout"
    implementation = checkout / "examples/research/probe.py"
    implementation.parent.mkdir(parents=True)
    implementation.write_text("# SYNTHETIC_TEST_ONLY", encoding="utf-8")
    monkeypatch.setattr(probe_module, "__file__", str(implementation))
    pilot_root = checkout / "data/research/pilot"
    # The public-only probe requires a false synthetic marker; all transport is
    # intercepted here and every retained response is explicitly fictional.
    freeze_pilot(pilot_root)
    calendar = tmp_path / "calendar.csv"
    calendar.write_text("date\n2021-09-13\n", encoding="utf-8")
    calls = []

    def denied(url, params, timeout):
        calls.append(url)
        response = requests.Response()
        response.status_code = 403
        response.url = url
        response._content = b"SYNTHETIC_TEST_ONLY access denied"
        return response

    monkeypatch.setattr(probe_module.requests, "get", denied)
    monkeypatch.setattr(probe_module.time, "sleep", lambda value: None)
    output = checkout / "data/research/supplier"
    receipt = probe_module.probe(output, pilot_root, calendar_path=calendar)
    assert len(calls) == receipt["http_attempts"] == 2  # one per independent supplier
    assert sum(item["status"] == "NOT_ATTEMPTED_ACCESS_LIMIT" for item in receipt["attempts"]) == 7
    assert len(list(output.rglob("*.bin"))) == 2
    assert all(item.get("response_sha256") for item in receipt["attempts"] if item["status"] == "ACCESS_DENIED_OR_RATE_LIMIT")
    with pytest.raises(ValueError, match="private research root"):
        probe_module.probe(checkout / "docs/data/research", pilot_root, calendar_path=calendar)
    with pytest.raises(ValueError, match="disjoint"):
        probe_module.probe(pilot_root, pilot_root, calendar_path=calendar)
