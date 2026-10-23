"""B-01/B-02/B-03 invariants. SYNTHETIC software evidence; NO LIVE MONEY."""

from decimal import (localcontext, ROUND_DOWN, ROUND_UP, ROUND_HALF_EVEN,
                     ROUND_HALF_UP, ROUND_HALF_DOWN, ROUND_FLOOR, ROUND_CEILING, ROUND_05UP)
import importlib.util
import math
from pathlib import Path
import shutil
import sys

import pandas as pd
import pytest

import factorlab
from factorlab.data.csmar import binding, fingerprint
from factorlab.data.evidence import json_bytes, sha256
from factorlab.errors import DataQualityError
from tests.research.test_csmar_source_binding import fixture, parse


@pytest.mark.parametrize("precision,rounding", [
    (1, ROUND_DOWN), (2, ROUND_UP), (3, ROUND_FLOOR), (28, ROUND_HALF_EVEN),
    (200, ROUND_CEILING), (1, ROUND_HALF_UP), (2, ROUND_HALF_DOWN), (200, ROUND_05UP)])
def test_decimal_context_must_not_change_raw_with_same_identity(tmp_path, precision, rounding):
    ref, _ = fixture(tmp_path)
    expected = parse(tmp_path, ref)
    with localcontext() as context:
        context.prec, context.rounding = precision, rounding
        context.Emin, context.Emax, context.clamp = -1, 1, 1
        for signal in context.traps:
            context.traps[signal] = True
        before = context.copy()
        actual = parse(tmp_path, ref)
        assert binding.parser_sha256() == ref["parser_sha256"]
        assert context.flags == before.flags and context.traps == before.traps
        assert (context.prec, context.rounding, context.Emin, context.Emax, context.clamp) == (before.prec, before.rounding, before.Emin, before.Emax, before.clamp)
    pd.testing.assert_frame_equal(expected.raw, actual.raw, check_exact=True)
    for name in ("raw_sha256", "native_sha256", "lineage_sha256"):
        assert expected.metrics[name] == actual.metrics[name]


@pytest.mark.parametrize("value", ["1.2", "0.9999999999999999999999999999999999", "9007199254740993"])
def test_nonintegral_native_volume_must_remain_rejected_under_low_precision(tmp_path, value):
    _, sheets = fixture(tmp_path)
    sheets["sheet1"][-1][9] = value
    ref, _ = fixture(tmp_path, sheets=sheets)
    with localcontext() as context:
        context.prec = 1
        with pytest.raises(DataQualityError, match=r"ZIP=.* member=TRDNEW_Dalyr.xlsx sheet=sheet1 row=4 field=Dnshrtrd"):
            parse(tmp_path, ref)


@pytest.mark.parametrize("value", ["1e-999", "1e-324", "2e-324", "1e309", "1e9999", "1e10000"])
def test_positive_native_prices_must_not_underflow_to_zero(tmp_path, value):
    _, sheets = fixture(tmp_path)
    sheets["sheet1"][-1][5:9] = [value] * 4
    ref, _ = fixture(tmp_path, sheets=sheets)
    with pytest.raises(DataQualityError, match=r"row=4 field=Opnprc"):
        parse(tmp_path, ref)


@pytest.mark.parametrize("value", ["0.01", "10.1", "5000.123456789", "1e-307", "5e-324", "1.7976931348623157e308", "1." + "2" * 100])
def test_binary64_price_contract_accepts_normal_and_nonzero_boundary_values(value):
    with localcontext() as context:
        context.prec = 1
        actual = binding.number(value, positive=True)
    assert actual == float(value) and math.isfinite(actual) and actual > 0


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-1", "0", "-0", "1,000", "9" * 129, "1e-10000"])
def test_invalid_or_out_of_contract_values_fail(value):
    with pytest.raises(DataQualityError):
        binding.number(value, positive=True)


def test_source_integrality_precedes_exact_unit_conversion():
    with localcontext() as context:
        context.prec = 1
        assert binding.number("1.2", "10000", integral=True) == 12000
        assert binding.number("0.0001", "10000", integral=True) == 1
        assert binding.number("4.0", integral=True) == 4
        assert binding.number("9007199254740992", integral=True) == 9007199254740992
        for value, multiplier in [("1.2", "1"), ("0.00001", "10000")]:
            with pytest.raises(DataQualityError):
                binding.number(value, multiplier, integral=True)
        with pytest.raises(DataQualityError, match="multiplier"):
            binding.number("1", "0.1")
        with pytest.raises(DataQualityError, match="underflow"):
            binding.number("1e-999")


def load_package(name, folder):
    spec = importlib.util.spec_from_file_location(name, folder / "__init__.py", submodule_search_locations=[str(folder)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return sys.modules[name + ".binding"]


def test_executed_package_initializer_must_be_in_fingerprint(tmp_path):
    baseline, changed = tmp_path / "baseline", tmp_path / "changed"
    shutil.copytree(Path(binding.__file__).parent, baseline, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(baseline, changed)
    with (changed / "__init__.py").open("a", encoding="utf8") as stream:
        stream.write('\nfrom . import binding as _binding\n_original = _binding.number\ndef _number(*args, **kwargs):\n    value = _original(*args, **kwargs)\n    return value * 2 if kwargs.get("positive") else value\n_binding.number = _number\n')
    before = load_package("security_fix_csmar_before", baseline)
    after = load_package("security_fix_csmar_after", changed)
    sources = tmp_path / "sources"
    ref, _ = fixture(sources)
    first = before.replay(ref, source_root=sources, scratch=tmp_path)
    assert first.raw.close.tolist() == [10.5]
    assert before.parser_sha256() != after.parser_sha256()
    with pytest.raises(DataQualityError, match="parser/profile/runtime/schema mismatch"):
        after.replay(ref, source_root=sources, scratch=tmp_path)


DEPENDENCIES = sorted({entry["path"] for role in ("parser", "qualification")
                       for section in ("files", "configs")
                       for entry in fingerprint.implementation_manifest(role)[section]})


@pytest.mark.parametrize("path", DEPENDENCIES)
def test_each_executable_dependency_and_config_change_invalidates_old_identity(tmp_path, monkeypatch, path):
    root = Path(factorlab.__file__).resolve().parents[2]
    clone = tmp_path / "clone"
    for name in DEPENDENCIES:
        target = clone / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    ref, _ = fixture(tmp_path / "sources")
    monkeypatch.setattr(factorlab, "__file__", str(clone / "src/factorlab/__init__.py"))
    monkeypatch.setattr(fingerprint, "__file__", str(clone / "src/factorlab/data/csmar/fingerprint.py"))
    old = {role: sha256(json_bytes(fingerprint.implementation_manifest(role))) for role in ("parser", "qualification")}
    assert old["parser"] == ref["parser_sha256"]
    target = clone / path
    target.write_bytes(target.read_bytes() + b"\n# isolated dependency mutation\n")
    for role in ("parser", "qualification"):
        assert sha256(json_bytes(fingerprint.implementation_manifest(role))) != old[role]
    with pytest.raises(DataQualityError, match="mismatch"):
        parse(tmp_path / "sources", ref)


def test_relative_paths_disambiguate_same_named_modules():
    manifest = fingerprint.implementation_manifest("qualification")
    paths = [entry["path"] for entry in manifest["files"]]
    assert len(paths) == len(set(paths))
    assert "src/factorlab/data/adjustment/validation.py" in paths
    assert "src/factorlab/data/corporate_actions/validation.py" in paths
    for path in paths:
        assert not Path(path).is_absolute() and "\\" not in path
    assert manifest["contract"] == "factorlab-csmar-implementation-path-closure-v1"
