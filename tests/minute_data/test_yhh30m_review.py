"""Independent acceptance regressions for catalog integrity and iterator filters."""

import json

import pytest

from tests.minute_data.test_yhh30m import fixture_bundle
from factorlab.engineering.yhh30m import IntradayDataError, IntradayParquetProvider
from factorlab.engineering.yhh30m.__main__ import main
from factorlab.engineering.yhh30m.contract import json_bytes
from factorlab.engineering.yhh30m.conversion import convert
from factorlab.engineering.yhh30m.source import file_sha


@pytest.mark.parametrize("mutation", ["rows", "parquet_hash", "audit_binding"])
def test_verify_cli_rejects_catalog_receipt_disagreement(tmp_path, mutation):
    source, audit = fixture_bundle(tmp_path)
    root = tmp_path / "published"
    convert(source, audit, root)
    catalog = json.loads((root / "dataset.json").read_text(encoding="utf-8"))
    part = catalog["partitions"][0]
    if mutation == "rows":
        part["rows"] += 1
        catalog["rows"] += 1
    elif mutation == "parquet_hash":
        part["parquet_sha256"] = "0" * 64
    else:
        path = root / part["receipt"]
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt["audit_evidence"]["daily-counts.csv"] = "0" * 64
        path.write_bytes(json_bytes(receipt))
        part["receipt_sha256"] = file_sha(path)
    (root / "dataset.json").write_bytes(json_bytes(catalog))
    with pytest.raises(IntradayDataError):
        main(["verify", "--dataset", str(root)])


def test_complete_flag_must_be_boolean_true(tmp_path):
    source, audit = fixture_bundle(tmp_path)
    root = tmp_path / "published"
    convert(source, audit, root)
    catalog = json.loads((root / "dataset.json").read_text(encoding="utf-8"))
    catalog["complete"] = "false"
    (root / "dataset.json").write_bytes(json_bytes(catalog))
    with pytest.raises(IntradayDataError):
        IntradayParquetProvider(root, purpose="engineering")


def test_generator_symbol_filter_remains_effective_across_batches(tmp_path):
    source, audit = fixture_bundle(tmp_path, many=True)
    root = tmp_path / "published"
    convert(source, audit, root)
    provider = IntradayParquetProvider(root, purpose="exploratory")
    batches = provider.iter_batches(symbols=(s for s in ["000001.SZ"]), batch_size=1)
    assert sum(len(batch) for batch in batches) == 6


@pytest.mark.parametrize("mutation", ["partition_identity", "root_selection", "sample_source", "receipt_total", "roundtrip_flag"])
def test_metadata_tampering_rejected_by_both_read_and_verify(tmp_path, mutation):
    source, audit = fixture_bundle(tmp_path)
    root = tmp_path / "published"
    convert(source, audit, root)
    catalog = json.loads((root / "dataset.json").read_text(encoding="utf-8"))
    part = catalog["partitions"][0]
    if mutation == "partition_identity":
        part["identity"] = "0" * 64
    elif mutation == "root_selection":
        path = root / "selection.json"
        selection = json.loads(path.read_text(encoding="utf-8"))
        selection["implementation_sha256"] = "0" * 64
        path.write_bytes(json_bytes(selection))
    else:
        path = root / part["receipt"]
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "sample_source":
            receipt["days"][0]["samples"][0]["normalized"]["source_id"] = "0" * 64
        elif mutation == "receipt_total":
            receipt["days"][0]["rows"] += 1
        else:
            receipt["output_roundtrip_verified"] = "true"
        path.write_bytes(json_bytes(receipt))
        part["receipt_sha256"] = file_sha(path)
    (root / "dataset.json").write_bytes(json_bytes(catalog))
    with pytest.raises(IntradayDataError):
        main(["verify", "--dataset", str(root)])
    with pytest.raises(IntradayDataError):
        provider = IntradayParquetProvider(root, purpose="exploratory")
        list(provider.iter_batches())


def test_projection_iterator_is_reusable_across_batches(tmp_path):
    source, audit = fixture_bundle(tmp_path)
    root = tmp_path / "published"
    convert(source, audit, root)
    provider = IntradayParquetProvider(root, purpose="engineering")
    batches = list(provider.iter_batches(columns=(c for c in ["symbol", "close"]), batch_size=1))
    assert len(batches) == 6
    assert all(b.schema.names == ["symbol", "close"] for b in batches)
