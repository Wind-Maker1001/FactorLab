"""SYNTHETIC_TEST_ONLY publication checks with zero empirical observations.

The source run is a truthful BLOCKED missing-input report. Added byte fixtures
exercise file exclusion only; they are never represented as vendor prices.
"""

import hashlib
import json

import pytest

from factorlab.exploratory.contract import ExploratoryContractError, sha256_file
from factorlab.exploratory.publication import publish_derived, verify_publication
from factorlab.exploratory.runner import run_research, verify_receipt


def write_receipt(path, payload):
    payload = {key: value for key, value in payload.items() if key != "receipt_sha256"}
    payload["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def blocked_private_run(tmp_path):
    # No generated prices and no assertion of a real supplier dataset.
    result = run_research(
        tmp_path / "missing-real-input.json", tmp_path / "exploratory"
    )
    assert result.status == "BLOCKED"
    root = result.output_dir
    fixture_bytes = {
        "raw.bin": b"SYNTHETIC_TEST_ONLY; PRIVATE EXCLUSION FIXTURE\n",
        "price.csv": b"SYNTHETIC_TEST_ONLY; NO EMPIRICAL PRICE OBSERVATIONS\n",
        "stock-label.parquet": b"SYNTHETIC_TEST_ONLY; NOT A PARQUET DATASET\n",
        "daily_ic.csv": b"scope,date,factor,observations,IC,RankIC\n",
    }
    payload = verify_receipt(root / "receipt.json")
    for name, content in fixture_bytes.items():
        (root / name).write_bytes(content)
        payload["artifacts"][name] = sha256_file(root / name)
    payload["reasons"].append(
        "SYNTHETIC_TEST_ONLY publication exclusion fixtures; zero empirical observations"
    )
    write_receipt(root / "receipt.json", payload)
    assert verify_receipt(root / "receipt.json")["status"] == "BLOCKED"
    return root


def test_private_market_bytes_and_labels_never_enter_publication(
    blocked_private_run, tmp_path
):
    destination = publish_derived(blocked_private_run, tmp_path / "public")
    assert not (destination / "raw.bin").exists()
    assert not (destination / "price.csv").exists()
    assert not (destination / "stock-label.parquet").exists()
    assert (destination / "daily_ic.csv").read_text(encoding="utf-8").count("\n") == 1
    receipt = verify_publication(destination / "receipt.json")
    assert receipt["status"] == "BLOCKED"
    assert receipt["raw_source_bytes_published"] is False
    assert receipt["private_run_receipt_sha256"] == sha256_file(
        blocked_private_run / "receipt.json"
    )
    index = json.loads(
        (destination / "input_manifest.json").read_text(encoding="utf-8")
    )
    assert index["raw_source_bytes_published"] is False
    assert index["reproduction_requires_authorized_private_input"] is True


def test_synthetic_run_cannot_be_published_as_real_evidence(
    blocked_private_run, tmp_path
):
    payload = verify_receipt(blocked_private_run / "receipt.json")
    payload["synthetic_only"] = True
    write_receipt(blocked_private_run / "receipt.json", payload)
    with pytest.raises(ExploratoryContractError, match="synthetic"):
        publish_derived(blocked_private_run, tmp_path / "public")
    assert not (tmp_path / "public").exists()


@pytest.mark.parametrize("tamper", ["extra_raw", "changed_csv"])
def test_published_extra_market_file_and_modified_derived_csv_are_rejected(
    blocked_private_run, tmp_path, tamper
):
    destination = publish_derived(blocked_private_run, tmp_path / "public")
    if tamper == "extra_raw":
        (destination / "extra-raw.bin").write_bytes(b"SYNTHETIC_TEST_ONLY")
    else:
        path = destination / "daily_ic.csv"
        path.write_bytes(path.read_bytes() + b"SYNTHETIC_TEST_ONLY; TAMPERED\n")
    with pytest.raises(ExploratoryContractError, match="artifact"):
        verify_publication(destination / "receipt.json")


@pytest.mark.parametrize("field", ["forward_allowed", "live_allowed"])
def test_rehashed_publication_cannot_authorize_forward_or_live(
    blocked_private_run, tmp_path, field
):
    destination = publish_derived(blocked_private_run, tmp_path / "public")
    path = destination / "receipt.json"
    payload = verify_publication(path)
    payload[field] = True
    write_receipt(path, payload)
    with pytest.raises(ExploratoryContractError):
        verify_publication(path)
