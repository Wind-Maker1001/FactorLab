"""SYNTHETIC_TEST_ONLY: offline acquisition provenance regression tests."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from qbot.research.exploratory.acquire import (
    RecordingAKShare,
    freeze_sample,
    ingest_firecrawl_capture,
    utc_now,
)
from qbot.research.exploratory.contract import sha256_file


@pytest.mark.parametrize(
    "cache_arguments, expected_cache",
    [
        ([], None),
        (
            ["--benchmark-cache", "data/research/benchmark"],
            Path("data/research/benchmark"),
        ),
    ],
)
def test_cli_fresh_checkout_selects_live_or_explicit_cache(
    tmp_path, monkeypatch, cache_arguments, expected_cache
):
    import qbot.research.exploratory.acquire as acquisition

    calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        acquisition, "acquire", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    acquisition.main(["--output", "private-dataset"] + cache_arguments)
    assert len(calls) == 1
    assert calls[0][0][0] == Path("private-dataset")
    assert calls[0][1]["benchmark_cache_root"] == expected_cache
    assert not (tmp_path / "private-dataset").exists()


def test_stock_list_cannot_be_refrozen_after_acquisition(tmp_path):
    frozen = freeze_sample(tmp_path, ["600000.SH"], selection_date="2026-10-14")
    original = (tmp_path / "sample.json").read_bytes()
    assert frozen["pit"] is False
    assert frozen["classification"] == "NON_PIT_CONVENIENCE"
    assert freeze_sample(tmp_path, ["600000.SH"]) == frozen
    with pytest.raises(ValueError, match="pre-frozen"):
        freeze_sample(tmp_path, ["600001.SH"])
    assert (tmp_path / "sample.json").read_bytes() == original


def test_native_source_replay_preserves_bytes_and_skips_network(tmp_path, monkeypatch):
    calls = []

    def fake_request(session, method, url, **kwargs):
        calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response.url = (
            requests.Request(method, url, params=kwargs.get("params")).prepare().url
        )
        response._content = b'{"fixture":"SYNTHETIC_TEST_ONLY","value":1}'
        response.headers["Content-Type"] = "application/json"
        return response

    monkeypatch.setattr(requests.sessions.Session, "request", fake_request)
    sdk = SimpleNamespace(__version__="SYNTHETIC_TEST_ONLY")
    first = RecordingAKShare(sdk, tmp_path)
    url = "https://example.invalid/fixture"
    with first.capture("synthetic_fixture", {"symbol": "600000", "adjust": "hfq"}):
        response = requests.get(url, params={"symbol": "600000", "adjust": "hfq"})
    original = {
        path.name: path.read_bytes() for path in (tmp_path / "sources").iterdir()
    }
    second = RecordingAKShare(sdk, tmp_path)
    with second.capture("synthetic_fixture", {"symbol": "600000", "adjust": "hfq"}):
        replay = requests.get(url, params={"symbol": "600000", "adjust": "hfq"})
    assert replay.content == response.content
    assert len(calls) == 1
    assert {
        path.name: path.read_bytes() for path in (tmp_path / "sources").iterdir()
    } == original
    receipt = json.loads(
        next((tmp_path / "sources").glob("*.request.json")).read_text()
    )
    assert receipt["retrieved_at"].endswith("+00:00")
    assert receipt["upstream_vintage"] == "UNKNOWN_UNVERIFIED"


def test_corrupt_cached_native_response_fails_before_replay(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "0001-synthetic.bin").write_bytes(b"SYNTHETIC_TEST_ONLY: corrupted")
    (sources / "0001-synthetic.request.json").write_text(
        json.dumps(
            {
                "response_file": "sources/0001-synthetic.bin",
                "response_sha256": "0" * 64,
                "url": "https://example.invalid/fixture",
            }
        )
    )
    with pytest.raises(ValueError, match="source hash mismatch"):
        RecordingAKShare(SimpleNamespace(__version__="SYNTHETIC_TEST_ONLY"), tmp_path)


def synthetic_capture(tmp_path, native_code="600000", suffix=""):
    freeze_sample(tmp_path, ["600000.SH"])
    directory = tmp_path / "sources" / "firecrawl"
    directory.mkdir(parents=True)
    path = directory / "SYNTHETIC_TEST_ONLY.json"
    path.write_text(
        json.dumps(
            {
                "capture_retrieved_at": utc_now(),
                "structuredContent": {
                    "rawHtml": json.dumps(
                        {
                            "rc": 0,
                            "data": {
                                "code": native_code,
                                "market": 1,
                                "klines": ["2025-01-02,10,11,12,9,100,110000,1,1,1,3"],
                            },
                        }
                    ),
                    "metadata": {
                        "statusCode": 200,
                        "contentType": "application/json; charset=UTF-8",
                        "sourceURL": "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600000&fqt=2&klt=101&beg=20250101&end=20260911"
                        + suffix,
                    },
                },
            }
        )
    )
    return path


def test_rawhtml_vendor_symbol_must_match_requested_symbol(tmp_path):
    path = synthetic_capture(tmp_path, native_code="600001")
    with pytest.raises(ValueError, match="identity/status"):
        ingest_firecrawl_capture(tmp_path, path)


def test_rawhtml_rejects_ambiguous_adjustment_query(tmp_path):
    path = synthetic_capture(tmp_path, suffix="&fqt=0")
    with pytest.raises(ValueError, match="one nonempty"):
        ingest_firecrawl_capture(tmp_path, path)


def test_rawhtml_representation_and_envelope_hashes_are_recorded(tmp_path):
    path = synthetic_capture(tmp_path)
    receipt = ingest_firecrawl_capture(tmp_path, path)
    assert receipt["symbol"] == "600000.SH"
    assert receipt["role"] == "HFQ"
    assert receipt["transport"] == "FIRECRAWL_RAWHTML_REPRESENTATION"
    assert receipt["response_sha256"] == sha256_file(
        tmp_path / receipt["response_file"]
    )
    assert receipt["envelope_sha256"] == sha256_file(path)
