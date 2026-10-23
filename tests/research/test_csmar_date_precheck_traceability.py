"""Production date precheck origins and selection order. NO LIVE MONEY."""

from copy import deepcopy
import re

import pytest

from qbot.research.data.csmar import binding, profiles
from qbot.research.errors import DataQualityError
from tests.research.test_csmar_rejection_coordinates import assert_location, original_fixture
from tests.research.test_csmar_source_binding import fixture, parse


FAMILIES = ("TRDNEW_Dalyr", "IDX_Idxtrd")
INVALID_DATES = [("2024-02-30", "invalid evidence date"),
                 ("2024/01/02", "canonical YYYY-MM-DD required"),
                 (None, "canonical YYYY-MM-DD required")]


@pytest.mark.parametrize("variant", [0, 1])
@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("value,core", INVALID_DATES)
def test_production_date_precheck_preserves_physical_origin(tmp_path, variant, family, value, core):
    field = profiles.PROFILES[family][2]
    ref, sheet, row = original_fixture(tmp_path, family, field, value, variant=variant)
    with pytest.raises(DataQualityError, match=core) as error:
        parse(tmp_path, ref)
    message = str(error.value)
    assert_location(message, ref, sheet, row, (field,))
    for label in ("ZIP=", "member=", "sheet=", "row=", "field="):
        assert message.count(label) == 1
    assert "A1=" not in message


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("excluded", ["other_code", "currency_variant", "outside_date_range"])
def test_excluded_rows_keep_original_parse_and_filter_order(tmp_path, family, excluded):
    if excluded == "currency_variant" and family != "IDX_Idxtrd":
        # There is no stock currency-variant exclusion in the existing contract.
        excluded = "other_code"
    _, sheets = fixture(tmp_path, family)
    rows = sheets["sheet1"]
    code_field, date_field = profiles.PROFILES[family][1:]
    ignored = deepcopy(rows[-1])
    if excluded == "outside_date_range":
        ignored[rows[0].index(date_field)] = "2024-01-01"
        ignored[rows[0].index(next(iter(profiles.MAPPINGS[family])))] = "NaN"
    else:
        ignored[rows[0].index(code_field)] = "000500HKD00" if excluded == "currency_variant" else "999999"
        ignored[rows[0].index(date_field)] = "2024-02-30"
    rows.insert(3, ignored)
    ref, _ = fixture(tmp_path, family, sheets=sheets)
    result = parse(tmp_path, ref)
    assert result.metrics["scanned_records"] == 2
    assert result.metrics["selected_records"] == 1
    assert result.lineage.row_number.tolist() == [5]


@pytest.mark.parametrize("family", FAMILIES)
def test_selected_invalid_date_precedes_numeric_and_range_checks(tmp_path, family):
    _, sheets = fixture(tmp_path, family)
    rows = sheets["sheet1"]
    field = profiles.PROFILES[family][2]
    rows[-1][rows[0].index(field)] = "2024-02-30"
    rows[-1][rows[0].index(next(iter(profiles.MAPPINGS[family])))] = "NaN"
    ref, _ = fixture(tmp_path, family, sheets=sheets)
    with pytest.raises(DataQualityError, match="invalid evidence date") as error:
        parse(tmp_path, ref)
    assert_location(str(error.value), ref, "sheet1", 4, (field,))
    assert "numeric" not in str(error.value)


@pytest.mark.parametrize("boundary", ["start", "end"])
@pytest.mark.parametrize("value,core", INVALID_DATES[:2])
def test_invalid_reference_scope_has_no_invented_row_origin(tmp_path, boundary, value, core):
    ref, _ = fixture(tmp_path)
    ref["scope"][boundary] = value
    with pytest.raises(DataQualityError, match=core) as error:
        binding.replay(ref, source_root=tmp_path / "not_opened", scratch=tmp_path)
    assert not re.search(r"\b(?:ZIP|member|sheet|row|field)=", str(error.value))


@pytest.mark.parametrize("family", FAMILIES)
def test_direct_invalid_date_without_provenance_does_not_invent_coordinates(tmp_path, family):
    _, sheets = fixture(tmp_path, family)
    rows = sheets["sheet1"]
    record = dict(zip(rows[0], rows[-1]))
    field = profiles.PROFILES[family][2]
    record[field] = "2024-02-30"
    with pytest.raises(DataQualityError, match="invalid evidence date") as error:
        binding.raw_projection(record, family)
    assert re.findall(r"\bfield=([A-Za-z0-9_]+)", str(error.value)) == [field]
    assert not re.search(r"\b(?:ZIP|member|sheet|row)=", str(error.value))


@pytest.mark.parametrize("family", FAMILIES)
def test_valid_leap_date_still_projects(tmp_path, family):
    _, sheets = fixture(tmp_path, family)
    rows = sheets["sheet1"]
    rows[-1][rows[0].index(profiles.PROFILES[family][2])] = "2024-02-29"
    ref, _ = fixture(tmp_path, family, sheets=sheets)
    ref["scope"].update(start="2024-02-29", end="2024-02-29")
    result = parse(tmp_path, ref)
    assert result.raw.date.dt.strftime("%Y-%m-%d").tolist() == ["2024-02-29"]
