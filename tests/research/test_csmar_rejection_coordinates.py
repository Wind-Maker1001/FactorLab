"""R-01: original native row/field locations on semantic refusals. NO LIVE MONEY."""

import io
import re
import zipfile
import xml.etree.ElementTree as ET

import pytest

from factorlab.data.csmar import binding, native, profiles
from factorlab.data.evidence import sha256
from factorlab.errors import DataQualityError
from tests.research.test_csmar_source_binding import fixture, parse, xlsx


STOCK_PRICES = ("Opnprc", "Hiprc", "Loprc", "Clsprc")
INDEX_PRICES = ("Idxtrd02", "Idxtrd03", "Idxtrd04", "Idxtrd05")
SEMANTIC_CASES = [
    ("TRDNEW_Dalyr", "Dnshrtrd", "0", "unfilled trade requires positive native volume", ("Dnshrtrd",)),
    ("TRDNEW_Dalyr", "Markettype", "2", "unsupported/ambiguous market currency or exchange", ("Markettype",)),
    ("TRDNEW_Dalyr", "Trdsta", "17", "unknown supplier Trdsta observation", ("Trdsta",)),
    ("TRDNEW_Dalyr", "Filling", "3", "unknown supplier Filling semantics", ("Filling",)),
    ("TRDNEW_Dalyr", "Filling", None, "unknown supplier Filling semantics", ("Filling",)),
    ("IDX_Idxtrd", "Indexcd", "000300", "benchmark must be native 000905 / 中证500", ("Indexcd", "Idxtrd09")),
    ("IDX_Idxtrd", "Idxtrd09", "other", "benchmark must be native 000905 / 中证500", ("Indexcd", "Idxtrd09")),
    *[("TRDNEW_Dalyr", f, v, "invalid native OHLC geometry", STOCK_PRICES)
      for f, v in zip(STOCK_PRICES, ("12", "1", "12", "12"))],
    *[("IDX_Idxtrd", f, v, "invalid native OHLC geometry", INDEX_PRICES)
      for f, v in zip(INDEX_PRICES, ("6000", "1", "6000", "6000"))],
]
NUMERIC_FIELDS = [(family, field) for family, mapping in profiles.MAPPINGS.items() for field in mapping]
NUMERIC_FIELDS += [("TRDNEW_Dalyr", "Markettype"), ("TRDNEW_Dalyr", "Trdsta")]


def original_fixture(tmp_path, family, field, value, *, variant):
    """Reordered headers, two sheets and sparse physical row numbers in XLSX."""
    _, sheets = fixture(tmp_path, family)
    rows = next(iter(sheets.values()))
    headers, names, units, values = [list(reversed(row)) for row in rows]
    values[headers.index(field)] = value
    # The earlier row is genuinely present but out of scope. An error on the
    # first selected output row must still identify the physical row 37/913.
    unselected = list(reversed(rows[-1]))
    day_field = profiles.PROFILES[family][2]
    unselected[headers.index(day_field)] = "2024-01-01"
    sheet = ("native_A", "原始页B")[variant]
    workbook = xlsx({"earlier_sheet": [headers, names, units, unselected],
                     sheet: [headers, names, units, unselected, values]})
    physical_row = (37, 913)[variant]
    revised = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(workbook)) as before, zipfile.ZipFile(revised, "w", zipfile.ZIP_DEFLATED) as after:
        for item in before.infolist():
            body = before.read(item.filename)
            if item.filename == "xl/worksheets/sheet2.xml":
                tree = ET.fromstring(body)
                target = next(row for row in tree.iter(native.NS + "row") if row.get("r") == "5")
                target.set("r", str(physical_row))
                for cell in target:
                    cell.set("r", re.sub(r"5$", str(physical_row), cell.get("r")))
                body = ET.tostring(tree)
            after.writestr(item, body)
    content = revised.getvalue()
    source_name = ("SYNTHETIC-R01-A.zip", "SYNTHETIC-R01-B.zip")[variant]
    member = family + ("7.xlsx", "23.xlsx")[variant]
    path = tmp_path / source_name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, content)
        archive.writestr(family + "[DES][xlsx].txt", profiles.SYNTHETIC_DICTIONARY)
    code_field = profiles.PROFILES[family][1]
    ref = binding.reference(source_name=source_name, source_sha256=sha256(path.read_bytes()),
        source_bytes=path.stat().st_size, family=family, member=member, member_sha256=sha256(content),
        sheet_records={"earlier_sheet": 1, sheet: 2}, codes=[values[headers.index(code_field)]],
        start="2024-01-02", end="2024-01-02", synthetic_only=True)
    return ref, sheet, physical_row


def assert_location(message, ref, sheet, row, fields):
    assert "ZIP=" + ref["source_name"] in message
    assert "member=" + ref["member"] in message
    assert "sheet=" + sheet in message
    assert re.search(r"\brow=%d\b" % row, message)
    assert re.findall(r"\bfield=([A-Za-z0-9_]+)", message) == list(fields)
    assert "row=0" not in message and "row=1 " not in message


@pytest.mark.parametrize("variant", [0, 1])
@pytest.mark.parametrize("family,field,value,semantic,fields", SEMANTIC_CASES)
def test_semantic_refusals_keep_original_coordinates(tmp_path, variant, family, field, value, semantic, fields):
    ref, sheet, row = original_fixture(tmp_path, family, field, value, variant=variant)
    with pytest.raises(DataQualityError, match=re.escape(semantic)) as error:
        parse(tmp_path, ref)
    assert_location(str(error.value), ref, sheet, row, fields)


@pytest.mark.parametrize("family,field", NUMERIC_FIELDS)
def test_every_numeric_field_refusal_keeps_original_coordinates(tmp_path, family, field):
    ref, sheet, row = original_fixture(tmp_path, family, field, "NaN", variant=1)
    with pytest.raises(DataQualityError, match="missing or ambiguous native numeric value") as error:
        parse(tmp_path, ref)
    assert_location(str(error.value), ref, sheet, row, (field,))


@pytest.mark.parametrize("family", ["TRDNEW_Dalyr", "IDX_Idxtrd"])
@pytest.mark.parametrize("value,semantic", [("2024/01/02", "canonical YYYY-MM-DD required"),
                                            ("2024-02-30", "invalid evidence date")])
def test_direct_projection_date_refusal_keeps_supplied_original_coordinates(tmp_path, family, value, semantic):
    _, sheets = fixture(tmp_path, family)
    rows = sheets["sheet1"]
    record = dict(zip(rows[0], rows[-1]))
    field = profiles.PROFILES[family][2]
    record[field] = value
    with pytest.raises(DataQualityError, match=semantic) as error:
        binding.raw_projection(record, family, "ZIP=direct.zip member=native.xlsx sheet=dates row=73")
    assert_location(str(error.value), {"source_name": "direct.zip", "member": "native.xlsx"}, "dates", 73, (field,))


def test_supported_native_table_without_raw_projection_retains_coordinates():
    with pytest.raises(DataQualityError, match="table has no canonical RAW projection") as error:
        binding.raw_projection({"Symbol": "000001", "TradingDate": "2024-01-02"}, "TRD_AdjustFactor",
                               "ZIP=factors.zip member=TRD_AdjustFactor.xlsx sheet=factors row=781")
    assert_location(str(error.value), {"source_name": "factors.zip", "member": "TRD_AdjustFactor.xlsx"},
                    "factors", 781, ("Symbol", "TradingDate"))


@pytest.mark.parametrize("family,field,value,semantic,fields", SEMANTIC_CASES)
def test_missing_provenance_preserves_rejection_without_invented_coordinates(tmp_path, family, field, value, semantic, fields):
    _, sheets = fixture(tmp_path, family)
    rows = sheets["sheet1"]
    record = dict(zip(rows[0], rows[-1]))
    record[field] = value
    with pytest.raises(DataQualityError, match=re.escape(semantic)) as error:
        binding.raw_projection(record, family)
    message = str(error.value)
    assert re.findall(r"\bfield=([A-Za-z0-9_]+)", message) == list(fields)
    for invented in ("ZIP=", "member=", "sheet=", "row="):
        assert invented not in message
