"""Synthetic SOFTWARE tests only; no market, identity or HFQ certification."""

import json
import struct

import pytest

from examples.research.audit_tdx_quality import (
    candidate_gbbq, compare_status, exact_ohlc, supplier_frame,
)


@pytest.mark.parametrize("integers,reference,expected", [
    ([100, 120, 90, 110], ["1.00", "1.20", "0.90", "1.10"], "EXACT_MATCH"),
    ([100, 120, 90, 110], ["1.00", "1.21", "0.90", "1.10"], "DIFFERENT"),
    ([100, 120, 90, 110], ["1.00", "1.200000000000001", "0.90", "1.10"], "DIFFERENT"),
    ([100, 120, 90, 110], ["2.00", "2.40", "1.80", "2.20"], "DIFFERENT"),
])
def test_fixed_scale_zero_tolerance(integers, reference, expected):
    assert exact_ohlc(integers, reference) == expected


@pytest.mark.parametrize("reference", [
    ["NaN", 2, 1, 1], ["Infinity", 2, 1, 1], [0, 2, 1, 1], [-1, 2, 1, 1],
    [1, 2, 1], [1, 2, 1, 1, 1],
])
def test_reject_invalid_reference(reference):
    with pytest.raises(ValueError):
        exact_ohlc([100, 200, 100, 100], reference)


def test_reject_fitted_scale_and_truncated_zip():
    with pytest.raises(ValueError):
        exact_ohlc([100, 200, 100, 100], [1, 2, 1, 1], 50)
    with pytest.raises(ValueError):
        exact_ohlc([100, 200, 100, 100, 100], [1, 2, 1, 1])


@pytest.mark.parametrize("has_tdx,has_ref,basis,units,numerical,expected", [
    (True, True, "RAW", True, "EXACT_MATCH", "EXACT_MATCH"),
    (True, True, "RAW", True, "DIFFERENT", "DIFFERENT"),
    (True, True, "RAW", False, "EXACT_MATCH", "SOURCE_UNIT_UNVERIFIED"),
    (True, True, "RAW", False, "DIFFERENT", "SOURCE_UNIT_UNVERIFIED"),
    (False, True, "RAW", False, "NOT_COMPARED", "NO_VALID_REFERENCE"),
    (True, False, "RAW", False, "NOT_COMPARED", "NO_VALID_REFERENCE"),
    (True, True, "HFQ", True, "EXACT_MATCH", "SOURCE_SEMANTICS_NOT_COMPARABLE"),
    (False, False, "HFQ", False, "NOT_COMPARED", "SOURCE_SEMANTICS_NOT_COMPARABLE"),
])
def test_numerical_equality_cannot_promote_unverified_financial_semantics(
    has_tdx, has_ref, basis, units, numerical, expected,
):
    assert compare_status(has_tdx, has_ref, basis, units, numerical) == expected


def tencent(key="day", code="sh600000"):
    return json.dumps({"code": 0, "data": {code: {key: [
        ["2025-07-16", "10", "11", "12", "9", "100"],
    ]}}}).encode()


def test_tencent_ohlc_order_and_raw_basis():
    frame = supplier_frame(tencent(), "TENCENT", "600000.SH", "RAW")
    assert list(frame.iloc[0]) == ["10", "12", "9", "11"]


@pytest.mark.parametrize("content,basis", [(tencent(), "HFQ"), (tencent("hfqday"), "RAW"),
                                           (tencent(code="sz000001"), "RAW")])
def test_never_fallback_to_wrong_price_basis_or_identity(content, basis):
    with pytest.raises(ValueError):
        supplier_frame(content, "TENCENT", "600000.SH", basis)


def test_sohu_layout_drift_stays_incomparable():
    payload = [{"status": 0, "code": "cn_600000", "hq": [
        ["2025-07-16", "10", "11", "0", "0", "9", "12", "100", "1000", "1", "extra"],
    ]}]
    with pytest.raises(ValueError):
        supplier_frame(json.dumps(payload).encode(), "SOHU", "600000.SH", "RAW")


def test_decoder_reference_is_never_executed(tmp_path):
    marker = tmp_path / "executed"
    source = ("open(%r, 'w').write('bad')" % str(marker)).encode()
    with pytest.raises(ValueError, match="unpinned"):
        candidate_gbbq(struct.pack("<I", 0), source)
    assert not marker.exists()


@pytest.mark.parametrize("content", [b"", b"123", struct.pack("<I", 1), struct.pack("<I", 0) + b"x"])
def test_gbbq_count_length_fails_before_parse(monkeypatch, content):
    import examples.research.audit_tdx_quality as module

    source = b"class GbbqReader: pass"
    monkeypatch.setattr(module, "DECODER_SHA", module.digest(source))
    with pytest.raises(ValueError, match="count|length"):
        candidate_gbbq(content, source)
