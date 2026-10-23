"""Generated SYNTHETIC software fixtures; never financial qualification."""

from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
import zipfile
from xml.sax.saxutils import escape

import pandas as pd
import pytest

from factorlab.data.csmar import native, profiles
from factorlab.data.csmar.binding import (
    parser_sha256, publish_diagnostic, reference, replay, verify_candidate, write_candidate,
)
from factorlab.data.evidence import archive_bytes, json_bytes, sha256
from factorlab.data.market_qualification import _reparse_raw, _unbound_real_roles
from factorlab.errors import DataQualityError, ProviderError
from factorlab.data.tdx import TdxError
from tests.research.market_data_fixture import qualification_inputs


def xlsx(sheet_rows):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as book:
        names, rels = [], []
        for i, (name, rows) in enumerate(sheet_rows.items(), 1):
            names.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (name, i, i))
            rels.append('<Relationship Id="rId%d" Target="worksheets/sheet%d.xml"/>' % (i, i))
            data = []
            for n, row in enumerate(rows, 1):
                cells = []
                for column, value in enumerate(row, 1):
                    label = ""
                    while column:
                        column, rem = divmod(column - 1, 26)
                        label = chr(65 + rem) + label
                    if value is not None:
                        cells.append('<c r="%s%d" t="inlineStr"><is><t>%s</t></is></c>' % (label, n, escape(str(value))))
                data.append('<row r="%d">%s</row>' % (n, "".join(cells)))
            # Deliberately wrong column dimension, matching observed CSMAR files.
            book.writestr('xl/worksheets/sheet%d.xml' % i,
                          '<worksheet xmlns="%s"><dimension ref="A1:A5"/><sheetData>%s</sheetData></worksheet>'
                          % (native.NS[1:-1], "".join(data)))
        book.writestr("xl/workbook.xml", '<workbook xmlns="%s" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>%s</sheets></workbook>' % (native.NS[1:-1], "".join(names)))
        book.writestr("xl/_rels/workbook.xml.rels", '<Relationships>%s</Relationships>' % "".join(rels))
    return out.getvalue()


def fixture(tmp_path, family="TRDNEW_Dalyr", *, sheets=None, dictionary=profiles.SYNTHETIC_DICTIONARY, extra=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    if family == "TRDNEW_Dalyr":
        headers = ["Stkcd", "Trddt", "Markettype", "Trdsta", "Filling", "Opnprc", "Hiprc", "Loprc", "Clsprc", "Dnshrtrd", "Dnvaltrd"]
        values = ["000001", "2024-01-02", "4", "1", "0", "10.10", "11.20", "9.00", "10.50", "1000", "10500.00"]
    elif family == "IDX_Idxtrd":
        headers = ["Indexcd", "Idxtrd01", "Idxtrd02", "Idxtrd03", "Idxtrd04", "Idxtrd05", "Idxtrd06", "Idxtrd07", "Idxtrd09"]
        values = ["000905", "2024-01-02", "5000", "5010", "4990", "5001", "1.2", "2.3", "中证500"]
    else:
        headers = ["TradingDate", "Symbol", "FwardFactor", "BwardFactor", "CumulateFwardFactor", "CumulateBwardFactor"]
        values = ["2024-01-02", "000001", "0.9", "1.111111", "0.9", "1.111111"]
    units = [profiles.MAPPINGS.get(family, {}).get(h, (None, None, None, None))[1] for h in headers]
    sheets = sheets or {"sheet1": [headers, headers, units, values]}
    content = xlsx(sheets)
    path = tmp_path / "SYNTHETIC-ONLY.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(family + ".xlsx", content)
        if dictionary is not None:
            archive.writestr(family + "[DES][xlsx].txt", dictionary)
        if extra:
            archive.writestr(extra, b"SYNTHETIC")
    if extra and "\\" in extra:
        # Windows ZipInfo normalizes separators when writing. Corrupt both raw
        # filename headers explicitly to exercise an actually malicious archive.
        path.write_bytes(path.read_bytes().replace(extra.replace("\\", "/").encode(), extra.encode()))
    ref = reference(source_name=path.name, source_sha256=sha256(path.read_bytes()), source_bytes=path.stat().st_size,
                    family=family, member=family + ".xlsx", member_sha256=sha256(content),
                    sheet_records={n: len(r) - 3 for n, r in sheets.items()}, codes=["000905" if family == "IDX_Idxtrd" else "000001"],
                    start="2024-01-02", end="2024-01-02", synthetic_only=True)
    return ref, sheets


def parse(tmp_path, ref):
    return replay(ref, source_root=tmp_path, scratch=tmp_path)


def test_native_roundtrip_exact_units_lineage_and_determinism(tmp_path):
    ref, _ = fixture(tmp_path)
    first, second = parse(tmp_path, ref), parse(tmp_path, ref)
    assert first.raw.symbol.tolist() == ["000001.SZ"]
    assert first.raw.date.tolist() == [pd.Timestamp("2024-01-02")]
    assert first.raw.iloc[0].open == 10.1
    assert first.native.iloc[0].Opnprc == "10.10"
    assert first.lineage.iloc[0].row_number == 4
    assert first.lineage.iloc[0].native_values_sha256 == sha256(json_bytes(first.native.iloc[0].to_dict()))
    for name in ("raw_sha256", "native_sha256", "lineage_sha256"):
        assert first.metrics[name] == second.metrics[name]
    assert first.metrics["formal_status"] == "FORMAL_ADMISSION_BLOCKED"


def test_native_integral_decimal_enumerations_are_explicit(tmp_path):
    _, sheets = fixture(tmp_path)
    sheets["sheet1"][-1][2:4] = ["4.0", "1.0"]
    ref, _ = fixture(tmp_path, sheets=sheets)
    assert parse(tmp_path, ref).raw.symbol.tolist() == ["000001.SZ"]


def test_multisheet_scans_complete_workbook_in_native_order(tmp_path):
    _, s = fixture(tmp_path)
    other = deepcopy(s["sheet1"])
    other[-1][0] = "000002"
    ref, _ = fixture(tmp_path, sheets={"second": s["sheet1"], "first": other})
    ref["scope"]["codes"] = ["000001", "000002"]
    result = parse(tmp_path, ref)
    assert result.lineage.sheet.tolist() == ["second", "first"]
    assert result.metrics["scanned_records"] == 2


def test_index_units_are_explicit_ten_thousand_and_not_percent(tmp_path):
    ref, _ = fixture(tmp_path, "IDX_Idxtrd")
    result = parse(tmp_path, ref)
    assert result.raw.symbol.tolist() == ["000905.SH"]
    assert result.raw.volume.tolist() == [12000.0]
    assert result.raw.amount.tolist() == [23000.0]


def test_native_currency_index_variants_are_excluded_without_stripping_suffix(tmp_path):
    _, sheets = fixture(tmp_path, "IDX_Idxtrd")
    variant = list(sheets["sheet1"][-1])
    variant[0] = "000905HKD00"
    sheets["sheet1"].append(variant)
    ref, _ = fixture(tmp_path, "IDX_Idxtrd", sheets=sheets)
    result = parse(tmp_path, ref)
    assert len(result.raw) == 1
    assert result.metrics["scanned_records"] == 2
    assert result.metrics["excluded_native_currency_index_variant_records"] == 1


def test_factors_retained_as_native_observations_without_hfq_or_identity(tmp_path):
    ref, _ = fixture(tmp_path, "TRD_AdjustFactor")
    result = parse(tmp_path, ref)
    assert result.raw.empty
    assert result.native.iloc[0].CumulateBwardFactor == "1.111111"
    assert {"ADJUSTMENT", "IDENTITY", "ANCHOR"}.issubset(result.metrics["unbound_financial_roles"])


@pytest.mark.parametrize("column,bad", [
    (0, "1"), (0, "00001"), (1, "2024-02-30"), (1, "2024-01-02T00:00:00"),
    (2, "2"), (2, "64"), (3, "0"), (4, "9"), (5, "NaN"), (5, "-9666"),
    (5, None), (6, "1"), (9, "1.2"), (9, "0"), (10, "inf"),
])
def test_reject_ambiguous_codes_dates_currency_fill_and_values(tmp_path, column, bad):
    _, sheets = fixture(tmp_path)
    sheets["sheet1"][-1][column] = bad
    ref, _ = fixture(tmp_path, sheets=sheets)
    with pytest.raises(DataQualityError):
        parse(tmp_path, ref)


@pytest.mark.parametrize("change", ["zip_hash", "member_hash", "dictionary", "missing_dictionary", "unit", "truncated", "duplicate", "missing_sheet", "parser", "profile", "runtime", "absolute", "escape", "scope"])
def test_reject_source_and_contract_tampering(tmp_path, change):
    ref, sheets = fixture(tmp_path)
    if change in {"dictionary", "missing_dictionary"}:
        ref, _ = fixture(tmp_path, dictionary=None if change == "missing_dictionary" else b"UNKNOWN UNIT")
    elif change == "unit":
        sheets["sheet1"][2][9] = "手"
        ref, _ = fixture(tmp_path, sheets=sheets)
    elif change == "duplicate":
        sheets["sheet1"].append(sheets["sheet1"][-1])
        ref, _ = fixture(tmp_path, sheets=sheets)
    elif change == "truncated":
        ref["sheet_records"]["sheet1"] = 2
    elif change == "missing_sheet":
        ref["sheet_records"]["absent"] = 1
    elif change == "zip_hash":
        (tmp_path / ref["source_name"]).write_bytes(b"changed original")
    elif change == "member_hash":
        ref["member_sha256"] = "0" * 64
    elif change in {"parser", "profile"}:
        ref[change + "_sha256"] = "0" * 64
    elif change == "runtime":
        ref["runtime"]["pyarrow"] = "unreviewed"
    elif change == "absolute":
        ref["source_name"] = "C:/secret.zip"
    elif change == "escape":
        ref["source_name"] = "../other.zip"
    elif change == "scope":
        ref["scope"]["end"] = "2024-01-01"
    with pytest.raises((DataQualityError, KeyError)):
        parse(tmp_path, ref)


@pytest.mark.parametrize("bad", ["../evil", "/evil", "C:/evil", "a\\evil"])
def test_reject_malicious_zip_paths(tmp_path, bad):
    ref, _ = fixture(tmp_path, extra=bad)
    with pytest.raises(DataQualityError, match="ZIP member"):
        parse(tmp_path, ref)


def test_filled_prices_never_enter_raw_as_trades(tmp_path):
    _, sheets = fixture(tmp_path)
    sheets["sheet1"][-1][4] = "1"
    sheets["sheet1"][-1][9:] = ["0", "0"]
    ref, _ = fixture(tmp_path, sheets=sheets)
    result = parse(tmp_path, ref)
    assert result.raw[["open", "high", "low", "close"]].isna().all().all()
    assert result.native.iloc[0].Opnprc == "10.10"


@pytest.mark.parametrize("code,name", [("000906", "中证500"), ("000905", "中证800")])
def test_no_wrong_index_substitution(tmp_path, code, name):
    _, sheets = fixture(tmp_path, "IDX_Idxtrd")
    sheets["sheet1"][-1][0] = code
    sheets["sheet1"][-1][-1] = name
    ref, _ = fixture(tmp_path, "IDX_Idxtrd", sheets=sheets)
    with pytest.raises(DataQualityError):
        parse(tmp_path, ref)


def test_candidate_replays_originals_and_cannot_become_v2(tmp_path):
    ref, _ = fixture(tmp_path)
    path, artifact, result = write_candidate(ref, source_root=tmp_path, scratch=tmp_path, output_root=tmp_path / "candidate")
    assert artifact.bytes < 10000
    replayed = verify_candidate(path, source_root=tmp_path, scratch=tmp_path)
    pd.testing.assert_frame_equal(result.raw, replayed.raw)
    from factorlab.data.market_qualification import load_qualification
    with pytest.raises(DataQualityError):
        load_qualification(path, stocks=result.raw, benchmark=result.raw)


@pytest.mark.parametrize("change", ["parquet", "receipt", "original", "path_escape", "forged_pass"])
def test_candidate_adversarial_changes(tmp_path, change):
    ref, _ = fixture(tmp_path)
    path, artifact, result = write_candidate(ref, source_root=tmp_path, scratch=tmp_path, output_root=tmp_path / "candidate")
    if change == "parquet":
        changed = result.raw.assign(close=99.0)
        changed.to_parquet(path.parent / "raw.parquet", index=False)
        value = json.loads(path.read_bytes())
        value["files"]["raw.parquet"] = sha256((path.parent / "raw.parquet").read_bytes())
        path.write_bytes(json_bytes(value))
    elif change == "receipt":
        Path(artifact.path).write_bytes(b"changed")
    elif change == "original":
        (tmp_path / ref["source_name"]).unlink()
    else:
        value = json.loads(path.read_bytes())
        value["reference"] = "../escape.bin" if change == "path_escape" else value["reference"]
        value["formal_admission"] = True
        value["source_binding"] = "PASS"
        path.write_bytes(json_bytes(value))
    if change == "forged_pass":
        from factorlab.data.market_qualification import load_qualification
        with pytest.raises(DataQualityError):
            load_qualification(path, stocks=result.raw, benchmark=result.raw)
    else:
        with pytest.raises((DataQualityError, OSError, TdxError)):
            verify_candidate(path, source_root=tmp_path, scratch=tmp_path)


def test_qualification_raw_reparse_and_all_other_roles_remain_unbound(tmp_path, monkeypatch):
    ref, _ = fixture(tmp_path)
    raw = parse(tmp_path, ref).raw
    artifact = archive_bytes(json_bytes(ref), tmp_path / "receipts", profiles.SOURCE_ID, "SYNTHETIC REFERENCE", "2026-10-21T00:00:00Z")
    monkeypatch.setenv("FACTORLAB_CSMAR_ROOT", str(tmp_path))
    monkeypatch.setenv("FACTORLAB_CSMAR_SCRATCH", str(tmp_path))
    i, _, _, _, _ = qualification_inputs(tmp_path)
    i = replace(i, raw=raw, raw_sources=(artifact,))
    assert _reparse_raw(i, True)
    assert "RAW" not in _unbound_real_roles(True)
    assert set(_unbound_real_roles(True)) == {"PIT", "STATUS", "CALENDAR", "IDENTITY", "UNITS", "ADJUSTMENT", "REFERENCE", "BENCHMARK", "TURNOVER", "LISTINGS", "ANCHOR"}
    with pytest.raises(DataQualityError, match="synthetic"):
        _reparse_raw(i, False)
    with pytest.raises(DataQualityError, match="differs"):
        _reparse_raw(replace(i, raw=raw.assign(close=99.0)), True)


def test_source_changes_during_replay_are_detected(tmp_path, monkeypatch):
    ref, _ = fixture(tmp_path)
    original = native.digest_stream
    calls = []
    def changed(handle):
        value = original(handle)
        calls.append(value)
        return "0" * 64 if len(calls) == 2 else value
    monkeypatch.setattr(native, "digest_stream", changed)
    with pytest.raises(DataQualityError, match="changed during"):
        parse(tmp_path, ref)


def test_actual_original_bytes_change_while_native_rows_are_consumed(tmp_path, monkeypatch):
    ref, _ = fixture(tmp_path)
    original = native.rows
    def changed(*args):
        for row in original(*args):
            if row[0] == 4:
                with (tmp_path / ref["source_name"]).open("ab") as handle:
                    handle.write(b"SYNTHETIC CONCURRENT MODIFICATION")
            yield row
    monkeypatch.setattr(native, "rows", changed)
    with pytest.raises(DataQualityError, match="changed during"):
        parse(tmp_path, ref)


@pytest.mark.parametrize("target", ["manifest", "reference"])
def test_actual_candidate_or_reference_changes_during_verification(tmp_path, monkeypatch, target):
    import factorlab.data.csmar.binding as binding
    ref, _ = fixture(tmp_path)
    path, artifact, _ = write_candidate(ref, source_root=tmp_path, scratch=tmp_path, output_root=tmp_path / "candidate")
    original = binding.replay
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        destination = path if target == "manifest" else Path(artifact.path)
        destination.write_bytes(destination.read_bytes() + b" ")
        return result
    monkeypatch.setattr(binding, "replay", changed)
    with pytest.raises(DataQualityError, match="changed during"):
        verify_candidate(path, source_root=tmp_path, scratch=tmp_path)


def test_xml_entity_formula_and_resource_limits(tmp_path, monkeypatch):
    with pytest.raises(DataQualityError, match="unsafe XML"):
        native.SafeXML(io.BytesIO(b'<!DOCTYPE a [<!ENTITY x "unsafe">]><a/>')).read()
    ref, _ = fixture(tmp_path)
    monkeypatch.setattr(native, "MAX_WORKBOOK", 1)
    with pytest.raises(DataQualityError, match="space limit"):
        parse(tmp_path, ref)


def test_complete_existing_qualification_recipe_copies_only_reference_and_rechecks_external_zip(tmp_path, monkeypatch):
    from factorlab.data.market_qualification import load_qualification, publish_qualification
    i, keys, dates, stocks, benchmark = qualification_inputs(tmp_path)
    _, sheets = fixture(tmp_path)
    header, names, units = sheets["sheet1"][:3]
    data = [["600000", str(d.date()), "1", "1", "0", "100", "101", "99", "100", "1000", "100000"] for d in dates]
    ref, _ = fixture(tmp_path, sheets={"sheet1": [header, names, units, *data]})
    ref["scope"] = {"codes": ["600000"], "start": str(dates.min().date()), "end": str(dates.max().date())}
    artifact = archive_bytes(json_bytes(ref), tmp_path / "receipts", profiles.SOURCE_ID, "SYNTHETIC REFERENCE", "2026-10-21T00:00:00Z")
    monkeypatch.setenv("FACTORLAB_CSMAR_ROOT", str(tmp_path))
    monkeypatch.setenv("FACTORLAB_CSMAR_SCRATCH", str(tmp_path))
    path = publish_qualification(replace(i, raw_sources=(artifact,)), keys, dates, stocks=stocks, benchmark=benchmark, output_root=tmp_path / "qualification")
    report, _, _, _, _ = load_qualification(path, stocks=stocks, benchmark=benchmark)
    assert report.stages["RAW"] == "PASS" and report.synthetic_only and not report.formal_admission
    payload = json.loads(path.read_bytes())
    assert ref["source_sha256"] not in payload["artifacts"].values()
    (tmp_path / ref["source_name"]).unlink()
    with pytest.raises(DataQualityError, match="qualification"):
        load_qualification(path, stocks=stocks, benchmark=benchmark)


def test_selected_byte_budget_and_unconfigured_operator_root(tmp_path, monkeypatch):
    import factorlab.data.csmar.binding as binding
    ref, _ = fixture(tmp_path)
    monkeypatch.setattr(binding, "MAX_SELECTED_ESTIMATED_BYTES", 1)
    with pytest.raises(DataQualityError, match="byte budget"):
        parse(tmp_path, ref)
    artifact = archive_bytes(json_bytes(ref), tmp_path / "receipts", profiles.SOURCE_ID, "SYNTHETIC REFERENCE", "2026-10-21T00:00:00Z")
    monkeypatch.delenv("FACTORLAB_CSMAR_ROOT", raising=False)
    monkeypatch.delenv("FACTORLAB_CSMAR_SCRATCH", raising=False)
    with pytest.raises(DataQualityError, match="operator"):
        binding.replay_artifact(artifact, synthetic=True)


@pytest.mark.parametrize("change", ["formula", "external_relationship", "duplicate_member", "zip_bomb", "file_type"])
def test_nested_zip_and_workbook_attack_surfaces(tmp_path, change):
    ref, _ = fixture(tmp_path)
    path = tmp_path / ref["source_name"]
    with zipfile.ZipFile(path) as original:
        content = original.read(ref["member"])
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as book, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as changed:
        for name in book.namelist():
            data = book.read(name)
            if change == "formula" and name.endswith("sheet1.xml"):
                data = data.replace(b'<c r="F4"', b'<c r="F4"', 1).replace(b'<is><t>10.10</t></is>', b'<f>1+1</f><v>2</v>')
            if change == "external_relationship" and name.endswith("workbook.xml.rels"):
                data = data.replace(b'Target="worksheets/sheet1.xml"', b'Target="https://example.invalid/data" TargetMode="External"')
            changed.writestr(name, data)
    content = out.getvalue()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(ref["member"], content)
        archive.writestr(ref["family"] + "[DES][xlsx].txt", profiles.SYNTHETIC_DICTIONARY)
        if change == "duplicate_member":
            with pytest.warns(UserWarning):
                archive.writestr(ref["member"], content)
        if change == "zip_bomb":
            archive.writestr("bomb.bin", b"0" * (2 * 1024 ** 2))
    ref["source_sha256"] = sha256(path.read_bytes())
    ref["source_bytes"] = path.stat().st_size
    ref["member_sha256"] = sha256(content)
    if change == "file_type":
        ref["source_name"] = "source.csv"
    with pytest.raises(DataQualityError):
        parse(tmp_path, ref)


def test_diagnostic_uses_existing_provider_and_still_rejects_formal(tmp_path, monkeypatch):
    raw_ref, _ = fixture(tmp_path / "raw")
    index_ref, _ = fixture(tmp_path / "index", "IDX_Idxtrd")
    root = tmp_path / "sources"
    root.mkdir()
    # Different source basenames preserve both originals.
    for folder, ref, name in [("raw", raw_ref, "raw.zip"), ("index", index_ref, "index.zip")]:
        (root / name).write_bytes((tmp_path / folder / ref["source_name"]).read_bytes())
        ref["source_name"] = name
    rp, _, r = write_candidate(raw_ref, source_root=root, scratch=tmp_path, output_root=tmp_path / "rc")
    bp, _, _ = write_candidate(index_ref, source_root=root, scratch=tmp_path, output_root=tmp_path / "bc")
    # Explicit synthetic no-event adjusted oracle, never generated for real RAW.
    stocks = r.raw.copy()
    for f in ("open", "high", "low", "close"):
        stocks["raw_" + f] = stocks[f]
        stocks["adj_" + f] = stocks[f]
    stocks["adj_factor"] = 1.0
    stocks["turnover"] = 0.01
    from factorlab.data.schema import CANONICAL_BAR_COLUMNS
    sp = tmp_path / "stocks.parquet"
    stocks[list(CANONICAL_BAR_COLUMNS)].to_parquet(sp, index=False)
    import factorlab.data.providers.local as local
    original_publish = local.publish_local_market_data
    def changed_caller_input(snapshot, *args, **kwargs):
        assert Path(snapshot) != sp
        sp.write_bytes(b"SYNTHETIC CONCURRENT CALLER FILE CHANGE")
        return original_publish(snapshot, *args, **kwargs)
    monkeypatch.setattr(local, "publish_local_market_data", changed_caller_input)
    published = publish_diagnostic(rp, bp, sp, source_root=root, scratch=tmp_path, output_root=tmp_path / "bundle")
    assert published["schema_version"] == 1
    from factorlab.data.providers.local import LocalParquetProvider
    from factorlab.data.admission import assert_formal_market_admission
    provider = LocalParquetProvider(tmp_path / "bundle/local")
    observed = provider.get_daily_bars("000001.SZ", "2024-01-02", "2024-01-02")
    assert len(observed) == 1 and observed.close.tolist() == [10.5]
    assert provider.get_research_qualification() is None
    with pytest.raises(ProviderError, match="v2 qualification"):
        assert_formal_market_admission(provider, membership=None, calendar=None, bars=None, benchmark=None, research_start=None, research_end=None)
    with pytest.raises(DataQualityError):
        publish_diagnostic(rp, bp, tmp_path / "rc/raw.parquet", source_root=root, scratch=tmp_path, output_root=tmp_path / "raw-only")
