"""Exact candidate scope, historical knowledge and separated valuation contracts."""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
import importlib.util
from pathlib import Path

import pytest

from factorlab.data.evidence import json_bytes, sha256
from factorlab.data.market_evidence_sidecar import (
    ALLOWED, POLICY, SAFETY, SCHEMA, SUSPENSION_DATES, load_sidecar,
)
from factorlab.backtest.rules import PriceLimitSchedule
from factorlab.errors import DataQualityError


@pytest.fixture
def case(tmp_path):
    manifest = b'{"test_source":"synthetic"}'
    public = b'synthetic independent issuer suspension and risk notice'
    digest = sha256(public)
    obs = dict(security="002408.SZ", date="2023-04-25", field="raw_close",
               status="OBSERVED_RAW_CLOSE", price="7.12", volume=100,
               source_sha256=sha256(b'synthetic observed price shard'))
    observation_bytes = json_bytes(obs)
    documents = {digest: public, sha256(observation_bytes): observation_bytes,
                 obs["source_sha256"]: b'synthetic observed price shard'}
    rows = []
    def row(security, day, field, value, evidence):
        return dict(target_security=security, target_date=day, field=field,
            original_value=None, candidate_value=value, evidence_class=evidence,
            source_uri="synthetic://document", source_document_sha256=digest,
            source_publication_time="2023-04-26", known_at_status="EX_POST_ONLY",
            retrospectively_verified_at="2026-11-11T12:00:00+08:00",
            verification_status="DOCUMENT_CONFIRMED", consumption_scope="RESEARCH_CANDIDATE_ONLY",
            limitation="Synthetic evidence does not authorize trading")
    for day in SUSPENSION_DATES:
        rows.append(row("002408.SZ", day, "suspension_confirmed", True, "ISSUER_CONFIRMED_SUSPENSION"))
        carry = row("002408.SZ", day, "valuation_mark", "7.12", "MODEL_VALUATION_ASSUMPTION")
        carry.update(source_document_sha256=sha256(observation_bytes), valuation_policy=POLICY,
            valuation_policy_version="1", valuation_status="MODEL_VALUATION_ASSUMPTION",
            valuation_source_date="2023-04-25", valuation_source_price="7.12",
            valuation_source_price_identity=obs["source_sha256"], nontradable=True)
        rows.append(carry)
    st = row("000540.SZ", "2023-05-08", "is_st", True, "ISSUER_RISK_DESIGNATION")
    st.update(known_at_status="SIGNAL_TIME_STATE_KNOWN", source_publication_time="2023-04-29")
    rows.append(st)
    rows.append(row("000540.SZ", "2023-05-08", "trading_observed", True, "EX_POST_TRADING_CONFIRMED"))
    rows[-1]["source_publication_time"] = "2023-05-10T02:46:00+08:00"
    rows.append(row("000540.SZ", "2023-05-08", "execution_price_available", True, "EXECUTION_PRICE_AVAILABLE"))
    payload = dict(schema=SCHEMA, version=1, source_manifest_sha256=sha256(manifest), safety=list(SAFETY), candidates=rows)
    originals = {k: None for k in ALLOWED}
    def load(p=None, m=None, original=None, docs=None):
        path = tmp_path / "sidecar.json"
        path.write_bytes(json_bytes(payload if p is None else p))
        return load_sidecar(path, source_manifest=manifest if m is None else m,
            originals=originals if original is None else original, documents=documents if docs is None else docs)
    return payload, obs, load, originals, documents


@pytest.mark.parametrize("day", SUSPENSION_DATES)
def test_exact_suspension_and_existing_holdings(case, day):
    _, obs, load, _, _ = case
    result = load().carry("002408.SZ", day, shares=13300, raw_close=None, previous_observation=obs)
    assert result.book_value == Decimal("94696.00")
    assert result.nontradable and result.raw_close is None
    assert result.valuation_status == "MODEL_VALUATION_ASSUMPTION"
    assert result.valuation_source_date == "2023-04-25"
    assert result.valuation_source_price_sha256 == obs["source_sha256"]


def test_no_observed_or_execution_price_injection(case):
    _, obs, load, _, _ = case
    raw = dict(raw_open=None, raw_close=None)
    result = load().carry("002408.SZ", "2023-04-26", shares=13600,
                          raw_close=raw["raw_close"], previous_observation=obs)
    assert raw == dict(raw_open=None, raw_close=None)
    assert not {"raw_open", "execution_price", "fill_price"} & asdict(result).keys()
    with pytest.raises(DataQualityError):
        load().fact("002408.SZ", "2023-04-26", "valuation_mark", purpose="EXECUTION")


@pytest.mark.parametrize("security,day,field", [
    ("002409.SZ", "2023-04-26", "valuation_mark"),
    ("002408.SZ", "2023-04-25", "valuation_mark"),
    ("002408.SZ", "2023-05-05", "valuation_mark"),
    ("002408.SZ", "2023-04-26", "raw_open"),
    ("000540.SZ", "2023-05-09", "is_st"),
    ("000540.SZ", "2023-05-08", "trade_status"),
])
def test_scope_does_not_expand(case, security, day, field):
    with pytest.raises(DataQualityError):
        case[2]().fact(security, day, field)


@pytest.mark.parametrize("field,value", [
    ("price", None), ("price", "0"), ("date", "2023-05-05"),
    ("status", "MODEL_VALUATION_ASSUMPTION"), ("security", "000540.SZ"),
    ("volume", 0), ("source_sha256", "0" * 64),
])
def test_bad_previous_observation_refused(case, field, value):
    _, obs, load, _, _ = case
    p = dict(obs, **{field: value})
    with pytest.raises(DataQualityError):
        load().carry("002408.SZ", "2023-04-26", shares=13300, raw_close=None, previous_observation=p)


def test_missing_previous_observation_refused(case):
    with pytest.raises(DataQualityError):
        case[2]().carry("002408.SZ", "2023-04-26", shares=1, raw_close=None, previous_observation=None)


def test_missing_independent_suspension_refused(case):
    payload, obs, load, _, _ = case
    payload["candidates"] = [r for r in payload["candidates"] if r["field"] != "suspension_confirmed"]
    with pytest.raises(DataQualityError):
        load().carry("002408.SZ", "2023-04-26", shares=1, raw_close=None, previous_observation=obs)


def test_observed_resumption_not_carried(case):
    _, obs, load, _, _ = case
    with pytest.raises(DataQualityError):
        load().carry("002408.SZ", "2023-05-05", shares=1, raw_close=6.76, previous_observation=obs)
    import pandas as pd
    from factorlab.backtest.execution import AShareExecutionBacktester
    dates = pd.to_datetime(["2023-04-24", "2023-04-25", "2023-04-26", "2023-05-05"])
    bars = pd.DataFrame(dict(date=dates, symbol="002408.SZ", raw_open=[7.12, 7.12, None, 7.13],
        raw_close=[7.12, 7.12, None, 6.76], volume=[100000, 100000, 0, 100000],
        trade_status=[1, 1, 0, 1], is_st=False))
    benchmark = pd.DataFrame(dict(date=dates, adj_open=1., adj_close=1.))
    targets = pd.DataFrame([dict(date=dates[0], symbol="002408.SZ", target_weight=.1)])
    result = AShareExecutionBacktester().run_target_weights(targets, bars, benchmark)
    held = result.positions.set_index("date")
    assert held.loc[dates[2], "mark_price"] == 7.12
    assert held.loc[dates[3], "mark_price"] == 6.76
    assert not (result.trades.execution_date == dates[2]).any()
    assert pd.isna(bars.loc[2, "raw_close"])


def test_unknown_status_and_st_are_not_defaults(case):
    _, _, load, originals, _ = case
    evidence = load()
    assert all(v is None for v in originals.values())
    with pytest.raises(DataQualityError):
        evidence.fact("000540.SZ", "2023-05-08", "trade_status")
    assert evidence.fact("000540.SZ", "2023-05-08", "is_st", purpose="SIGNAL_STATE",
                         cutoff="2023-05-05T15:00:00+08:00").candidate_value is True
    with pytest.raises(DataQualityError):
        evidence.fact("000540.SZ", "2023-05-08", "is_st", purpose="SIGNAL_STATE", cutoff="2023-04-28")


def test_ex_post_does_not_authorize_signal_or_execution(case):
    evidence = case[2]()
    for purpose in ("SIGNAL_STATE", "EXECUTION"):
        with pytest.raises(DataQualityError):
            evidence.fact("000540.SZ", "2023-05-08", "trading_observed", purpose=purpose,
                          cutoff="2023-05-08T09:30:00+08:00")
    payload = deepcopy(case[0])
    payload["candidates"][-2]["known_at_status"] = "SIGNAL_TIME_STATE_KNOWN"
    with pytest.raises(DataQualityError):
        case[2](payload)


def test_unknown_limit_evidence_never_authorizes_order(case):
    with pytest.raises(DataQualityError):
        case[2]().fact("000540.SZ", "2023-05-08", "execution_price_available", purpose="EXECUTION")
    blocked, bounds = PriceLimitSchedule.a_share_default().blocks(
        "000540.SZ", "2023-05-08", "sell", .60, .63, is_st=True)
    assert blocked and bounds.lower == .60


def test_manifest_drift_refused(case):
    with pytest.raises(DataQualityError):
        case[2](m=b'changed')


def test_conflicting_source_refused(case):
    payload = deepcopy(case[0])
    conflicting = dict(payload["candidates"][0], candidate_value=False)
    payload["candidates"].append(conflicting)
    with pytest.raises(DataQualityError):
        case[2](payload)


def test_changed_document_refused(case):
    docs = dict(case[4])
    docs[next(iter(docs))] = b'changed'
    with pytest.raises(DataQualityError):
        case[2](docs=docs)


@pytest.mark.parametrize("value", [0, False])
def test_null_original_is_not_zero_or_false(case, value):
    originals = dict(case[3])
    originals[("000540.SZ", "2023-05-08", "is_st")] = value
    with pytest.raises(DataQualityError):
        case[2](original=originals)


def test_safety_cannot_upgrade(case):
    assert case[2]().safety == SAFETY
    payload = deepcopy(case[0]); payload["safety"] = []
    with pytest.raises(DataQualityError):
        case[2](payload)


@pytest.mark.parametrize("field,value", [("verification_status", "UNKNOWN"),
    ("candidate_value", False), ("evidence_class", "EX_POST_TRADING_CONFIRMED")])
def test_unproven_st_not_promoted(case, field, value):
    payload = deepcopy(case[0]); payload["candidates"][-3][field] = value
    with pytest.raises(DataQualityError):
        case[2](payload)


def test_missing_original_price_source_refused(case):
    docs = dict(case[4]); del docs[case[1]["source_sha256"]]
    with pytest.raises(DataQualityError):
        case[2](docs=docs)


@pytest.mark.parametrize("vendor", ["CSMAR", "CNRDS"])
def test_bounded_vendor_reader(tmp_path, vendor):
    import io
    import zipfile
    from openpyxl import Workbook
    path = Path(__file__).resolve().parents[2] / "tools/research/verify_p9_market_evidence.py"
    spec = importlib.util.spec_from_file_location("p9_verify", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    workbook = Workbook(); sheet = workbook.active
    if vendor == "CSMAR":
        for _ in range(3):
            sheet.append(["header"] * 27)
        row = [None] * 27
        row[:10] = ["000540", "2023-05-08", 4, 3, "0", .6, .6, .6, .6, 5953200]
        sheet.append(row)
        for day in SUSPENSION_DATES:
            sheet.append(["002408", day, 4, 1, "1", 7.12, 7.12, 7.12, 7.12, 0])
        sheet.append(["002409", "2023-04-25"])
        member = "TRDNEW_Dalyr.xlsx"
    else:
        sheet.append(["header"] * 19); sheet.append(["description"] * 19)
        for day in SUSPENSION_DATES:
            sheet.append(["002408", "synthetic", day, 7.12, 7.12, 7.12, 7.12])
        sheet.append(["002409", "synthetic", "2023-04-25"])
        member = "2023/个股日回报率-2023_1.xlsx"
    stream = io.BytesIO(); workbook.save(stream)
    archive = tmp_path / "vendor.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(member, stream.getvalue())
    result = module.inspect_workbook(archive, vendor)
    assert result["target_rows"] == (5 if vendor == "CSMAR" else 4)
    assert result["scanned_prefix_rows"] == (6 if vendor == "CSMAR" else 5)
    if vendor == "CSMAR":
        assert result["suspension_filled_rows"] == 4
    else:
        assert result["target_000540_rows"] == 0


@pytest.mark.parametrize("field,value", [("target_security", "000001.SZ"),
    ("target_date", "2023-05-05"), ("field", "raw_close"),
    ("nontradable", False), ("valuation_status", "OBSERVED_RAW_CLOSE")])
def test_forged_valuation_contract_refused(case, field, value):
    payload = deepcopy(case[0]); payload["candidates"][1][field] = value
    with pytest.raises(DataQualityError):
        case[2](payload)
