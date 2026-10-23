"""Immutable conversion, independent values, recovery and daily gate boundaries."""

from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from qbot.research.data.providers.local import _strict_bars
from qbot.research.data.providers.base import MarketDataProvider
from qbot.research.errors import ProviderError
from qbot.engineering.yhh30m import CONTRACT, SCHEMA, IntradayDataError, IntradayParquetProvider
from qbot.engineering.yhh30m.__main__ import main
from qbot.engineering.yhh30m.contract import FLAGS, RAW_COLUMNS, json_bytes
from qbot.engineering.yhh30m.conversion import convert
from qbot.engineering.yhh30m.normalize import DayValidator
from qbot.engineering.yhh30m.source import file_sha, load_inventory
from qbot.engineering.yhh30m.storage import verify_partition, writer_lock


def raw_rows(day, *, anomalies=False):
    return [["000001.SZ", "平安银行", day, day[:4] + "-" + day[4:6] + "-" + day[6:] + " " + t,
             10.0, 12.0, 9.0, 11.0, -1.0 if anomalies and i == 0 else 100.0,
             -10.0 if anomalies and i == 0 else 1000.0,
             "" if anomalies and i == 0 else 1.25]
            for i, t in enumerate(("09:30:00", "15:30:00" if anomalies else "10:00:00"))]


def fixture_bundle(tmp_path, *, duplicate=False, anomalies=False, many=False):
    source, audit = tmp_path / "source", tmp_path / "audit"
    source.mkdir()
    audit.mkdir()
    containers, members, daily = {}, [], []
    for index, day in enumerate(("20210104", "20210105", "20260203")):
        rows = raw_rows(day, anomalies=anomalies)
        if many:
            rows = []
            for code in range(200):
                r = raw_rows(day)
                for record in r:
                    record[0] = "%06d.SZ" % code
                rows.extend(r)
        if duplicate:
            rows += [rows[0]]
        text = io.StringIO(newline="")
        writer = csv.writer(text)
        writer.writerow(RAW_COLUMNS)
        writer.writerows(rows)
        content = text.getvalue().encode("utf-8-sig")
        name = day + "_行情.csv"
        digest = hashlib.sha256(content).hexdigest()
        if index < 2:
            container, locator = "2021.zip", "30min/" + name
            with zipfile.ZipFile(source / container, "a", compression=zipfile.ZIP_DEFLATED) as z:
                z.writestr(locator, content)
                info = z.getinfo(locator)
                members.append(dict(archive=container, member=locator, bytes=info.file_size,
                                    crc32="%08x" % info.CRC, sha256=digest))
        else:
            container, locator = "loose_2026_csv", name
            (source / name).write_bytes(content)
        record = dict(date=day[:4] + "-" + day[4:6] + "-" + day[6:], container=container,
                      locator=locator, rows=len(rows), securities=200 if many else 1,
                      bars_histogram=json.dumps({len(rows) // (200 if many else 1): 200 if many else 1}),
                      time_labels="09:30:00|" + ("15:30:00" if anomalies else "10:00:00"),
                      content_sha256=digest,
                      null_cells=1 if anomalies else 0, nonfinite_numeric_cells=1 if anomalies else 0,
                      nonpositive_price_rows=0, ohlc_logic_rows=0, negative_volume_rows=1 if anomalies else 0,
                      negative_amount_rows=1 if anomalies else 0, zero_volume_rows=0, zero_amount_rows=0,
                      outside_nine_label_rows=1 if anomalies else 0, nonpositive_factor_rows=0)
        daily.append(record)
    for path in source.iterdir():
        containers["<EXTERNAL_MARKET>/" + path.name] = file_sha(path)
    (audit / "input-hashes.json").write_bytes(json_bytes(dict(containers=containers, archive_members=members)))
    (audit / "summary.json").write_bytes(json_bytes(dict(csv_days=3, total_rows=sum(d["rows"] for d in daily))))
    with (audit / "daily-counts.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(daily[0]))
        writer.writeheader()
        writer.writerows(daily)
    outputs = {"evidence/" + name: file_sha(audit / name) for name in ("summary.json", "input-hashes.json", "daily-counts.csv")}
    (audit / "final-verification.json").write_bytes(json_bytes({"output_sha256": outputs}))
    return source, audit


def read_all(root, **kwargs):
    provider = IntradayParquetProvider(root, purpose="engineering")
    batches = list(provider.iter_batches(**kwargs))
    return pa.Table.from_batches(batches, schema=SCHEMA)


def test_exact_values_source_unchanged_and_monthly_partitioning(tmp_path):
    source, audit = fixture_bundle(tmp_path)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.iterdir()}
    out = tmp_path / "out"
    result = convert(source, audit, out)
    assert result["rows"] == result["expected_rows"] == 6
    assert result["partitions"] == 2 and result["days"] == 3
    assert not result["formal_admission"] and result["no_live_money"]
    table = read_all(out)
    assert table.schema.equals(SCHEMA, check_metadata=True)
    frame = table.to_pandas()
    assert list(frame.symbol.unique()) == ["000001.SZ"]
    assert frame.open.tolist() == [10.] * 6 and frame.volume_vendor.tolist() == [100.] * 6
    assert frame.adjustment_factor_vendor.tolist() == [1.25] * 6
    assert frame.timestamp_local.iloc[0] == pd.Timestamp("2021-01-04 09:30")
    assert frame.source_row.tolist() == [1, 2, 1, 2, 1, 2]
    assert frame.quality_flags.eq(0).all()
    assert not frame.duplicated(["symbol", "timestamp_local"]).any()
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.iterdir()}
    assert len(list(out.rglob("bars.parquet"))) == 2
    for receipt in out.glob("partitions/*/*/*/receipt.json"):
        assert verify_partition(receipt.parent)["output_roundtrip_verified"]


def test_anomalies_preserved_not_repaired(tmp_path):
    source, audit = fixture_bundle(tmp_path, anomalies=True)
    out = tmp_path / "out"
    convert(source, audit, out)
    frame = read_all(out).to_pandas()
    assert frame.volume_vendor.tolist() == [-1, 100] * 3
    assert frame.amount_vendor.tolist() == [-10, 1000] * 3
    assert frame.adjustment_factor_vendor.isna().sum() == 3
    assert (frame.quality_flags.iloc[::2] & FLAGS["negative_volume"]).all()
    assert (frame.quality_flags.iloc[1::2] & FLAGS["outside_regular_session"]).all()
    assert CONTRACT["adjustment_method"] == "UNAVAILABLE"


def test_idempotency_and_adapter_filter(tmp_path):
    source, audit = fixture_bundle(tmp_path)
    out = tmp_path / "out"
    convert(source, audit, out)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in out.rglob("bars.parquet")}
    convert(source, audit, out, block_size=2048)
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in out.rglob("bars.parquet")}
    assert len(read_all(out, start="2021-01-05", end="2021-01-05", symbols=["000001.SZ"])) == 2
    assert len(read_all(out, symbols=["600000.SH"])) == 0


def test_streaming_equivalent_to_single_block(tmp_path):
    source, audit = fixture_bundle(tmp_path, many=True)
    convert(source, audit, tmp_path / "small", block_size=1024)
    convert(source, audit, tmp_path / "large", block_size=16 * 1024 * 1024)
    assert read_all(tmp_path / "small").equals(read_all(tmp_path / "large"), check_metadata=True)
    receipts = [json.loads(p.read_text(encoding="utf-8")) for p in (tmp_path / "small").glob("partitions/*/*/*/receipt.json")]
    assert all(d["max_batch_rows"] < d["rows"] for r in receipts for d in r["days"])


def test_daily_pipeline_cannot_consume_intraday(tmp_path):
    source, audit = fixture_bundle(tmp_path)
    out = tmp_path / "out"
    convert(source, audit, out)
    provider = IntradayParquetProvider(out, purpose="exploratory")
    assert not isinstance(provider, MarketDataProvider)
    with pytest.raises(IntradayDataError, match="NOT_FORMALLY"):
        IntradayParquetProvider(out, purpose="formal")
    with pytest.raises(IntradayDataError, match="AGGREGATION_DISABLED"):
        provider.daily_bars()
    with pytest.raises(ProviderError, match="explicit canonical/raw/adjusted columns"):
        _strict_bars(read_all(out).to_pandas(), is_index=False, benchmark_symbol="000905.SH")


def test_duplicate_cross_batch_never_publishes(tmp_path):
    source, audit = fixture_bundle(tmp_path, duplicate=True, many=True)
    out = tmp_path / "out"
    with pytest.raises(IntradayDataError, match="DUPLICATE_PRIMARY_KEY"):
        convert(source, audit, out, block_size=1024)
    assert not (out / "dataset.json").exists()
    assert list((out / "failures").glob("*.json"))


@pytest.mark.parametrize("mutate", ["source", "audit", "extra"])
def test_changed_sources_or_evidence_rejected(tmp_path, mutate):
    source, audit = fixture_bundle(tmp_path)
    if mutate == "source":
        p = next(source.glob("*.csv"))
        p.write_bytes(p.read_bytes() + b"\n")
    elif mutate == "audit":
        (audit / "summary.json").write_text("{}")
    else:
        (source / "extra.csv").write_bytes(b"x")
    with pytest.raises(IntradayDataError, match="MISMATCH|CHANGED"):
        convert(source, audit, tmp_path / "out")


@pytest.mark.parametrize("target", ["bars.parquet", "receipt.json"])
def test_corrupt_publication_rejected_on_read_or_resume(tmp_path, target):
    source, audit = fixture_bundle(tmp_path)
    out = tmp_path / "out"
    convert(source, audit, out)
    path = next(out.glob("partitions/*/*/*/" + target))
    path.write_bytes(b"broken")
    with pytest.raises((IntradayDataError, ValueError)):
        read_all(out)
    with pytest.raises((IntradayDataError, ValueError)):
        convert(source, audit, out)
    assert path.read_bytes() == b"broken"


def test_atomic_failure_and_resume_reuses_completed_month(tmp_path, monkeypatch):
    import qbot.engineering.yhh30m.conversion as module
    source, audit = fixture_bundle(tmp_path)
    original = module.convert_source
    def fail_on_second_month(source, item, *args):
        if item["date"].startswith("2026"):
            raise OSError("simulated interruption")
        return original(source, item, *args)
    monkeypatch.setattr(module, "convert_source", fail_on_second_month)
    out = tmp_path / "out"
    with pytest.raises(OSError, match="interruption"):
        convert(source, audit, out)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in out.glob("partitions/*/*/[!.]*/bars.parquet")}
    assert len(before) == 1
    with pytest.raises(IntradayDataError, match="INCOMPLETE"):
        read_all(out)
    monkeypatch.setattr(module, "convert_source", original)
    result = convert(source, audit, out)
    assert result["rows"] == 6
    for p, value in before.items():
        assert (p.read_bytes(), p.stat().st_mtime_ns) == value
    assert len(list(out.glob("partitions/*/*/[!.]*/bars.parquet"))) == 2


def test_output_overlap_nonempty_and_os_lock(tmp_path):
    source, audit = fixture_bundle(tmp_path)
    with pytest.raises(IntradayDataError, match="OVERLAP"):
        convert(source, audit, source / "out")
    out = tmp_path / "tdx"
    out.mkdir()
    (out / "data.parquet").write_bytes(b"existing TDX bytes")
    with pytest.raises(IntradayDataError, match="NONEMPTY_UNMARKED"):
        convert(source, audit, out)
    with writer_lock(out):
        with pytest.raises(IntradayDataError, match="CONCURRENT_WRITER"):
            with writer_lock(out):
                pytest.fail("lock should not be acquired")
    with writer_lock(out):
        pass


@pytest.mark.parametrize("column,value,error", [("证券代码", "1", "SYMBOL"), ("交易日期", "2021-01-04", "TRADE_DATE"),
                                                  ("分钟时间", "2021-01-05 09:30:00", "MISMATCH"),
                                                  ("分钟时间", "2021-01-04 09:31", "FORMAT")])
def test_invalid_identity_and_timestamp_fail_closed(tmp_path, column, value, error):
    source, audit = fixture_bundle(tmp_path)
    items, _, _ = load_inventory(source, audit)
    frame = pd.DataFrame(raw_rows("20210104"), columns=RAW_COLUMNS)
    frame["交易日期"] = frame["交易日期"].astype(str)
    frame[column] = value
    with pytest.raises((IntradayDataError, ValueError), match=error):
        DayValidator(items[0]).convert(pa.RecordBatch.from_pandas(frame))


def test_cli_convert_and_verify(tmp_path, capsys):
    source, audit = fixture_bundle(tmp_path)
    out = tmp_path / "out"
    assert main(["convert", "--source", str(source), "--audit", str(audit), "--output", str(out)]) == 0
    assert main(["verify", "--dataset", str(out)]) == 0
    assert '"status": "VERIFIED"' in capsys.readouterr().out


def test_public_redaction_binding_is_explicit_and_fail_closed(tmp_path):
    source, audit = fixture_bundle(tmp_path)
    summary = audit / "summary.json"
    original = file_sha(summary)
    value = json.loads(summary.read_text(encoding="utf-8"))
    value["source"] = "<EXTERNAL_MARKET>"
    summary.write_bytes(json_bytes(value))
    publication = audit.parent / "pit-publication-20261018-v1"
    publication.mkdir()
    manifest = publication / "manifest.json"
    mapping = {"files": [{"path": "evidence/" + audit.name + "/summary.json", "original_sha256": original,
                           "public_sha256": file_sha(summary), "public_bytes": summary.stat().st_size}]}
    manifest.write_bytes(json_bytes(mapping))
    _, _, evidence = load_inventory(source, audit)
    assert evidence["summary.json:original_sha256"] == original
    assert evidence["publication_manifest_sha256"] == file_sha(manifest)
    mapping["files"][0]["original_sha256"] = "0" * 64
    manifest.write_bytes(json_bytes(mapping))
    with pytest.raises(IntradayDataError, match="PUBLIC_AUDIT_BINDING_MISMATCH"):
        load_inventory(source, audit)


def test_price_anomalies_flagged_and_symbol_normalization_preserves_vendor(tmp_path):
    source, audit = fixture_bundle(tmp_path)
    items, _, _ = load_inventory(source, audit)
    frame = pd.DataFrame(raw_rows("20210104"), columns=RAW_COLUMNS)
    frame["证券代码"] = " 000001.sz "
    frame.loc[0, "开盘价（元）"] = 0.0
    frame.loc[1, "最高价（元）"] = 5.0
    result = DayValidator(items[0]).convert(pa.RecordBatch.from_pandas(frame)).to_pandas()
    assert result.symbol.tolist() == ["000001.SZ"] * 2
    assert result.vendor_symbol.tolist() == [" 000001.sz "] * 2
    assert result.open.iloc[0] == 0.0 and result.high.iloc[1] == 5.0
    assert result.quality_flags.iloc[0] & FLAGS["nonpositive_price"]
    assert result.quality_flags.iloc[1] & FLAGS["invalid_ohlc"]


def test_atomic_rename_failure_never_exposes_unverified_partition(tmp_path, monkeypatch):
    import qbot.engineering.yhh30m.conversion as module
    source, audit = fixture_bundle(tmp_path)
    out = tmp_path / "out"
    def fail_publish(*args):
        raise OSError("simulated atomic rename failure")
    monkeypatch.setattr(module.os, "rename", fail_publish)
    with pytest.raises(OSError, match="rename failure"):
        convert(source, audit, out)
    assert not (out / "dataset.json").exists()
    assert list(out.glob("partitions/*/*/.incomplete-*/receipt.json"))
    assert not list(out.glob("partitions/*/*/[!.]*/receipt.json"))


def test_os_lock_releases_after_abrupt_process_exit(tmp_path):
    out = tmp_path / "lock"
    out.mkdir()
    script = ("from pathlib import Path; import os; "
              "from qbot.engineering.yhh30m.storage import writer_lock; "
              "guard=writer_lock(Path(os.environ['YHH_TEST_LOCK_ROOT'])); "
              "guard.__enter__(); os._exit(0)")
    import os
    result = subprocess.run([sys.executable, "-B", "-c", script], env=dict(os.environ, YHH_TEST_LOCK_ROOT=str(out)),
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert (out / ".writer.lock").exists()
    with writer_lock(out):
        pass


def test_prior_audit_count_conflict_refuses_publication(tmp_path):
    source, audit = fixture_bundle(tmp_path)
    path = audit / "daily-counts.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows[0]["negative_volume_rows"] = "1"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    final = audit / "final-verification.json"
    receipt = json.loads(final.read_text(encoding="utf-8"))
    receipt["output_sha256"]["evidence/daily-counts.csv"] = file_sha(path)
    final.write_bytes(json_bytes(receipt))
    with pytest.raises(IntradayDataError, match="PRIOR_AUDIT_CHECK_MISMATCH"):
        convert(source, audit, tmp_path / "out")
    assert not (tmp_path / "out/dataset.json").exists()
