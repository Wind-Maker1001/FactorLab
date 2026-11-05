"""Synthetic read-only directory discovery and real parser integration."""
from copy import deepcopy
import json
import zipfile

import pandas as pd
import pytest

from factorlab.data.csmar import profiles
from factorlab.data.csmar.catalog import catalog_source, references
from factorlab.data.csmar.prepare import action_candidates, market_candidate, prepare
from factorlab.data.csmar.binding import Projection, replay
from factorlab.data.evidence import json_bytes, sha256
from factorlab.errors import DataQualityError
from tests.research.test_csmar_source_binding import fixture


def source(tmp_path, monkeypatch):
    _, sheets = fixture(tmp_path)
    rows = sheets["sheet1"]
    rows[0].append("Adjprcwd")
    rows[1].append("Comparable reinvestment close")
    rows[2].append("元/股")
    template = rows.pop()
    days = list(pd.bdate_range("2017-11-01", periods=22)) + list(pd.bdate_range("2018-01-02", periods=5))
    for day in days:
        row = list(template)
        row[0], row[1] = "601857", str(day.date())
        row[2] = "1"
        row.append("21")
        rows.append(row)
    # Exact symbol/time filtering excludes a second security and future year.
    other = list(rows[-1]); other[0] = "601858"; rows.append(other)
    other = list(rows[-1]); other[0] = "601857"; other[1] = "2019-01-02"; rows.append(other)
    fixture(tmp_path, sheets=sheets)
    monkeypatch.setitem(profiles.PROFILES, "TRDNEW_Dalyr",
                        (sha256(profiles.SYNTHETIC_DICTIONARY), "Stkcd", "Trddt"))
    path = tmp_path / "SYNTHETIC-ONLY.zip"
    return path, sheets


def test_discovery_prepare_exact_scope_units_hashes_and_determinism(tmp_path, monkeypatch):
    root = tmp_path / "raw"
    path, _ = source(root, monkeypatch)
    before = path.read_bytes()
    entry = catalog_source(path, scratch=tmp_path, scan_families=["TRDNEW_Dalyr"])
    book = next(m for m in entry["members"] if m["name"].endswith(".xlsx"))
    assert book["records"] == 29 and book["security_count"] == 2
    assert (book["date_min"], book["date_max"]) == ("2017-11-01", "2019-01-02")
    assert book["sheet_records"] == {"sheet1": 29}
    catalog = dict(sources=[entry])
    kwargs = dict(source_root=root, scratch=tmp_path, symbol="601857.SH", start="2018-01-01",
                  end="2018-12-31", warmup_start="2017-01-01")
    for name in ("one", "two"):
        manifest = prepare(catalog, output=tmp_path / name, **kwargs)
        value = json.loads(manifest.read_bytes())
        assert value["formal_status"] == "FORMAL_ADMISSION_BLOCKED"
        assert value["coverage"]["research_records"] == 5
        assert value["coverage"]["warmup_records"] == 22
        for filename, digest in value["files"].items():
            assert sha256((manifest.parent / filename).read_bytes()) == digest
    first, second = [pd.read_parquet(tmp_path / n / "market/bars.parquet") for n in ("one", "two")]
    pd.testing.assert_frame_equal(first, second, check_exact=True)
    assert first.symbol.unique().tolist() == ["601857.SH"]
    assert first.raw_close.eq(10.5).all() and first.adj_close.eq(21).all()
    assert first.volume.eq(1000).all() and first.amount.eq(10500).all()
    assert first.trade_status.eq(1).all() and first.is_st.eq(0).all()
    assert first.date.max() < pd.Timestamp("2019-01-01")
    assert path.read_bytes() == before
    with pytest.raises(FileExistsError):
        prepare(catalog, output=tmp_path / "one", **kwargs)


def test_pinned_source_and_member_tampering_fail(tmp_path, monkeypatch):
    root = tmp_path / "raw"
    path, _ = source(root, monkeypatch)
    catalog = dict(sources=[catalog_source(path, scratch=tmp_path, scan_families=["TRDNEW_Dalyr"])])
    ref = references(catalog, family="TRDNEW_Dalyr", codes=["601857"], start="2018-01-01", end="2018-12-31")[0]
    tampered = deepcopy(ref); tampered["member_sha256"] = "0" * 64
    with pytest.raises(DataQualityError, match="member hash"):
        replay(tampered, source_root=root, scratch=tmp_path)
    body = bytearray(path.read_bytes()); body[10] ^= 1; path.write_bytes(body)
    with pytest.raises(DataQualityError, match="source ZIP hash"):
        replay(ref, source_root=root, scratch=tmp_path)


def test_overlapping_shards_and_unscanned_family_are_rejected(tmp_path, monkeypatch):
    path, _ = source(tmp_path / "raw", monkeypatch)
    entry = catalog_source(path, scratch=tmp_path, scan_families=["TRDNEW_Dalyr"])
    with pytest.raises(DataQualityError, match="overlapping"):
        prepare(dict(sources=[entry, entry]), output=tmp_path / "out", source_root=path.parent,
                scratch=tmp_path, symbol="601857.SH", start="2018-01-01", end="2018-12-31", warmup_start="2017-01-01")
    entry = catalog_source(path, scratch=tmp_path)
    with pytest.raises(DataQualityError, match="not scanned"):
        references(dict(sources=[entry]), family="TRDNEW_Dalyr", codes=["601857"], start="2018-01-01", end="2018-12-31")


@pytest.mark.parametrize("column,value,match", [("Adjprcwd", "-1", "numeric"), ("Trdsta", "16", "unsupported"),
                                               ("Markettype", "4", "identity")])
def test_unreviewed_signal_price_status_or_exchange_fail(tmp_path, monkeypatch, column, value, match):
    path, sheets = source(tmp_path / "raw", monkeypatch)
    rows = sheets["sheet1"]
    rows[3][rows[0].index(column)] = value
    fixture(path.parent, sheets=sheets)
    entry = catalog_source(path, scratch=tmp_path, scan_families=["TRDNEW_Dalyr"])
    ref = references(dict(sources=[entry]), family="TRDNEW_Dalyr", codes=["601857"], start="2017-01-01", end="2018-12-31")[0]
    with pytest.raises(DataQualityError, match=match):
        market_candidate(replay(ref, source_root=path.parent, scratch=tmp_path), "601857.SH")


def test_action_candidate_keeps_original_coordinates_without_invented_record_date():
    record = dict(Stkcd="601857", Disttyp="CA", Exdistdt="2018-06-21", Markettype="1",
                  Annodt="2018-06-12", Paydt="2018-06-21", Amount="0.06074", Roprc="0")
    origin = dict(source_sha256="a" * 64, member="TRDNEW_Cptl.xlsx", member_sha256="b" * 64,
                  sheet="sheet1", row_number=45, native_values_sha256=sha256(json_bytes(record)))
    projection = Projection(pd.DataFrame(), pd.DataFrame([record]), pd.DataFrame([origin]), {})
    candidate = action_candidates(projection, "601857.SH", "2026-11-05T04:00:00Z")[0]
    assert candidate["event"]["record_date"] is None
    assert str(candidate["event"]["cash"]) == "0.06074"
    assert candidate["event"]["status"] == "PROPOSED"
    assert candidate["event"]["pay_date"] == "2018-06-21"
    assert candidate["origin"] == origin
    assert candidate["tax_status"] == "UNKNOWN"
