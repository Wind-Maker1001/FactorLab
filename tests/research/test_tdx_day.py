"""Synthetic byte fixtures test engineering only, never market validation."""

import hashlib
from pathlib import Path
import socket
import struct

import numpy as np
import pytest

from factorlab.data.tdx import (
    DAY_DTYPE, TdxError, audit_day, classify, discover, raw_frame, safe_path,
)


def record(date=20200102, open_=1000, high=1100, low=900, close=1050,
           amount=10500., volume=1000, reserved=65536):
    return struct.pack("<IIIIIfII", date, open_, high, low, close, amount,
                       volume & 0xffffffff, reserved)


@pytest.fixture
def root(tmp_path):
    for market in ("sh", "sz", "bj"):
        (tmp_path / market / "lday").mkdir(parents=True)
    return tmp_path


def audit(root, content, filename="sh600000.day", market="sh", **kwargs):
    path = root / market / "lday" / filename
    path.write_bytes(content)
    return audit_day(path, market, root, "2020-01-02", "2026-10-09", **kwargs)


def codes(result):
    return {row["code"] for row in result.anomalies}


def test_standard_layout_exact_values_and_raw_contract(root):
    assert DAY_DTYPE.itemsize == 32
    content = record() + record(20200103, amount=10500.125, reserved=123)
    result = audit(root, content)
    frame = raw_frame(result)
    assert result.convertible
    assert result.inventory["source_sha256"] == hashlib.sha256(content).hexdigest()
    assert frame.date.dtype == np.dtype("datetime64[ns]")
    assert frame.close.tolist() == [10.5, 10.5]
    assert frame.amount.tolist() == [10500., 10500.125]
    assert frame.tdx_reserved.tolist() == [65536, 123]
    assert frame.volume.tolist() == [1000, 1000]
    assert not any(c.startswith("adj_") for c in frame.columns)
    assert not frame.research_eligible.any()
    assert set(frame.volume_unit) == {"UNKNOWN"}
    assert set(frame.price_scale_status) == {"UNVERIFIED"}


@pytest.mark.parametrize("content,expected", [
    (b"", "EMPTY_FILE"), (b"abc", "TRUNCATED_RECORD"),
    (record() + b"bad", "TRUNCATED_RECORD"),
    (record(20200230), "INVALID_DATE"), (record(20201301), "INVALID_DATE"),
    (record(2020012), "INVALID_DATE"),
    (record() + record(), "DUPLICATE_DATE"),
    (record(20200103) + record(), "NON_INCREASING_DATE"),
    (record(open_=0), "NON_POSITIVE_OR_SIGNED_NEGATIVE_PRICE"),
    (record(close=0xffffffff), "NON_POSITIVE_OR_SIGNED_NEGATIVE_PRICE"),
    (record(high=800), "INVALID_OHLC"), (record(low=1200), "INVALID_OHLC"),
    (record(amount=-1), "INVALID_AMOUNT"), (record(amount=float("nan")), "INVALID_AMOUNT"),
    (record(amount=float("inf")), "INVALID_AMOUNT"),
])
def test_corruption_is_hashed_and_never_converted(root, content, expected):
    result = audit(root, content)
    assert expected in codes(result)
    assert result.inventory["source_sha256"] == hashlib.sha256(content).hexdigest()
    with pytest.raises(TdxError, match="CANNOT_CONVERT"):
        raw_frame(result)


@pytest.mark.parametrize("filename", ["sz600000.day", "sh60000.day", "600000.day", "sh60000x.day"])
def test_invalid_name_or_market(root, filename):
    result = audit(root, record(), filename)
    assert "INVALID_FILENAME_OR_MARKET_PREFIX" in codes(result)
    assert not result.convertible


@pytest.mark.parametrize("volume", [0, -1, 2**31 + 10])
def test_zero_and_signed_negative_interpretation_retains_bits(root, volume):
    result = audit(root, record(volume=volume, amount=0 if volume == 0 else 1000))
    code = "ZERO_VOLUME_STATUS_UNKNOWN" if volume == 0 else "VOLUME_HIGH_BIT_SIGN_OR_OVERFLOW_UNKNOWN"
    assert code in codes(result)
    assert result.convertible
    assert raw_frame(result).volume.iloc[0] == volume & 0xffffffff
    assert result.coverage["suspension_or_missing_status"] == "UNKNOWN"


@pytest.mark.parametrize("market,code,kind,divisor", [
    ("sh", "600000", "A_SHARE_CANDIDATE", 100),
    ("sz", "000001", "A_SHARE_CANDIDATE", 100),
    ("bj", "920001", "A_SHARE_CANDIDATE", 100),
    ("bj", "430047", "AMBIGUOUS", 100),
    ("sh", "000905", "INDEX_CANDIDATE", 100),
    ("sz", "399001", "INDEX_CANDIDATE", 100),
    ("sh", "510300", "FUND_CANDIDATE", 1000),
    ("sz", "159915", "FUND_CANDIDATE", 1000),
    ("sh", "900901", "B_SHARE_CANDIDATE", 100),
    ("sz", "200001", "B_SHARE_CANDIDATE", 100),
    ("sh", "113001", "CONVERTIBLE_BOND_CANDIDATE", None),
    ("sz", "123001", "CONVERTIBLE_BOND_CANDIDATE", None),
    ("sh", "999999", "UNKNOWN", None),
])
def test_types_are_hypotheses_not_confirmed_equities(market, code, kind, divisor):
    result = classify(market, code)
    assert result["security_type"] == kind
    assert result["candidate_price_divisor"] == divisor
    assert not result["confirmed_a_share"]
    assert result["historical_identity_status"] == "UNKNOWN"


def test_different_history_and_no_network(root, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network forbidden"))
    short = audit(root, record(20261009), "bj920001.day", "bj")
    long = audit(root, record() + record(20200103) + record(20261009))
    assert short.inventory["record_count"] == 1
    assert long.inventory["record_count"] == 3
    assert short.coverage["records_2020"] == 0
    assert long.coverage["records_2020"] == 2
    assert short.coverage["research_and_warmup_complete"] == "UNKNOWN"


def test_reference_calendar_gaps_never_imply_suspension(root):
    import pandas as pd
    calendar = pd.DatetimeIndex(["2020-01-02", "2020-01-03", "2020-01-06"])
    result = audit(root, record() + record(20200106), calendar=calendar,
                   research_start="2020-01-03", research_end="2020-01-06", warmup_days=1)
    assert result.coverage["research_coverage_ratio"] == .5
    assert not result.coverage["research_and_warmup_complete"]
    assert result.coverage["warmup_observed_dates"] == 1
    assert "REFERENCE_CALENDAR_GAPS_STATUS_UNKNOWN" in codes(result)


def test_traversal_and_root_escape(root):
    with pytest.raises(TdxError, match="PATH_TRAVERSAL"):
        safe_path(root / "sh" / ".." / "other")
    with pytest.raises(TdxError, match="OUTSIDE_ROOT"):
        safe_path(root.parent / "other", root)


def test_symlink_and_link_ancestors(root):
    target = root / "sh" / "lday" / "sh600000.day"
    target.write_bytes(record())
    link = root / "sz" / "lday" / "sz000001.day"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("OS cannot create symbolic links")
    with pytest.raises(TdxError, match="LINK_OR_REPARSE"):
        safe_path(link, root)
    entries, issues = discover(root)
    assert len(entries) == 2
    assert any(i["code"] == "UNSAFE_OR_UNREADABLE_ENTRY" for i in issues)
    assert not audit_day(link, "sz", root, "2020-01-02", "2026-10-09").convertible


def test_recursive_inventory_includes_bad_names_and_non_day(root):
    nested = root / "sh" / "lday" / "sub"
    nested.mkdir()
    (nested / "bad.day").write_bytes(record())
    (root / "bj" / "lday" / "readme.txt").write_text("not a stock")
    entries, issues = discover(root)
    assert entries[0]["path"].name == "bad.day"
    assert issues[0]["code"] == "NON_DAY_ENTRY"


def test_missing_market_fails_closed(root):
    (root / "bj" / "lday").rmdir()
    with pytest.raises(TdxError, match="MARKET_LDAY_MISSING"):
        discover(root)
