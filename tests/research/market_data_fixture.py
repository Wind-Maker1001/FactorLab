"""Synthetic evidence recipes only; never real financial qualification."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from factorlab.data.adjustment import AdjustmentAnchor, SourceAdjustmentContract, build_hfq
from factorlab.data.adjustment.contracts import AFFINE
from factorlab.data.corporate_actions import CorporateAction, EventCoverage, SourceSnapshot, select_events
from factorlab.data.evidence import snapshot_file, table_sha256
from factorlab.data.market_qualification import AdjustmentInput, QualificationInputs
from factorlab.data.schema import CANONICAL_BAR_COLUMNS
from factorlab.data.units import UnitContract
from factorlab.universe.identity import IdentitySnapshot, SecurityIdentity


def cutoffs(dates):
    return {pd.Timestamp(d): (pd.Timestamp(d).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15, minutes=30)).isoformat() for d in dates}


def evidence(tmp_path):
    p = tmp_path / "synthetic-source.txt"
    if not p.exists():
        p.write_text("SYNTHETIC SOFTWARE FIXTURE ONLY; NOT MARKET DATA\n", encoding="utf-8")
    return snapshot_file(p, "SYNTHETIC_FIXTURE_SOURCE", "2024-12-31T00:00:00+08:00")


def event(artifact, *, day="2024-01-04", event_id="cash-1", **kwargs):
    values = dict(security_id="fixture-security", event_id=event_id, revision=1,
                  event_type="CASH_DIVIDEND", effective_date=day,
                  announced_at="2023-12-01", available_at="2023-12-01",
                  source_id="SYNTHETIC_FIXTURE_SOURCE", source_sha256=artifact.sha256,
                  status="IMPLEMENTED", currency="CNY", cash=Decimal(2), bonus=Decimal(0),
                  capitalization=Decimal(0), rights=Decimal(0), reference_close=Decimal(100),
                  record_date=(pd.Timestamp(day)-pd.Timedelta(days=1)).date().isoformat(),
                  pay_date=day, credit_date=day, tradable_date=day,
                  rule_id="STANDARD_CNY_PER_OLD_SHARE_V1",
                  original_terms="SYNTHETIC ONLY explicit per-old-share terms", synthetic_only=True)
    values.update(kwargs)
    return CorporateAction(**values)


def snapshot(artifact, events=(), *, start="2024-01-02", end="2024-12-30", issues=()):
    return SourceSnapshot("SYNTHETIC_FIXTURE_SOURCE", "synthetic-v1", "2024-12-31T00:00:00+08:00",
                          tuple(events), (EventCoverage("fixture-security", start, end, "2023-12-01", artifact.sha256, "COMPLETE"),),
                          (artifact,), tuple(issues), True, "CONTEMPORANEOUS")


def raw(dates, opens=None):
    p = np.full(len(dates), 100.0) if opens is None else np.asarray(opens, dtype=float)
    return pd.DataFrame({"date": dates, "symbol": "600000.SH", "open": p, "high": p+1,
                         "low": p-1, "close": p, "volume": np.full(len(dates), 1000.0),
                         "amount": p*1000.0})


def anchor(artifact, method=AFFINE, day="2024-01-02"):
    return AdjustmentAnchor("fixture-security", day, method, Decimal(1), Decimal(0),
                            "2023-12-01", artifact.sha256, True)


def build(raw_bars, snapshot_value, artifact, method=AFFINE, anchor_value=None):
    view = select_events(snapshot_value, effective_through=raw_bars.date.max().date().isoformat(),
                         knowledge_as_of=cutoffs(raw_bars.date)[raw_bars.date.max()])
    return build_hfq(raw_bars, view, method=method,
                     anchor=anchor_value or anchor(artifact, method), signal_as_of=cutoffs(raw_bars.date))


def qualification_inputs(tmp_path, periods=8):
    artifact = evidence(tmp_path)
    dates = pd.bdate_range("2024-01-02", periods=periods)
    source = raw(dates)
    snap = snapshot(artifact)
    view = select_events(snap, effective_through=str(dates.max().date()), knowledge_as_of=cutoffs(dates)[dates.max()])
    independent = source[["date", "symbol"]].copy()
    for field in ("open", "high", "low", "close"):
        independent["adj_"+field] = source[field]  # Manually known no-event identity oracle.
    contract = SourceAdjustmentContract("INDEPENDENT_SYNTHETIC_ORACLE", "golden-v1", AFFINE,
                                        artifact.sha256, table_sha256(independent), synthetic_only=True)
    spec = AdjustmentInput(source, view, AFFINE, anchor(artifact), cutoffs(dates), independent, contract)
    identity = IdentitySnapshot((SecurityIdentity("fixture-security", "600000.SH", str(dates.min().date()), str(dates.max().date()),
                                                   "1991-04-03", "2023-12-01", artifact.sha256, security_type="A_SHARE"),), (artifact,), True)
    units = (UnitContract("600000.SH", str(dates.min().date()), str(dates.max().date()), "synthetic-v1",
                           "CNY", Decimal(1), "SHARES", "CNY", Decimal(1), "2023-12-01", artifact, True),)
    keys = source[["date", "symbol"]].copy()
    status = keys.assign(trade_status=1, is_st=False)
    membership = keys.assign(is_member=True)
    listings = pd.DataFrame({"symbol": ["600000.SH"], "listing_date": [pd.Timestamp("1991-04-03")]})
    turnover = keys.assign(turnover=0.01)
    benchmark = source.copy()
    benchmark["symbol"] = "000905.SH"
    for field in ("open", "high", "low", "close"):
        benchmark["raw_"+field] = benchmark[field]
        benchmark["adj_"+field] = benchmark[field]
    benchmark["turnover"] = 0.0
    benchmark["adj_factor"] = 1.0
    benchmark = benchmark[list(CANONICAL_BAR_COLUMNS)]
    inputs = QualificationInputs(source, turnover, identity, units, {"fixture-security": spec}, cutoffs(dates),
                                  "synthetic-v1", status, listings, membership, (artifact,),
                                  pd.DataFrame({"date": dates}), artifact, artifact, artifact,
                                  expected_member_count=1, synthetic_only=True,
                                  research_start=str(dates.min().date()), research_end=str(dates.max().date()))
    from factorlab.data.assemble import assemble_market_data
    result = build(source, snap, artifact)
    tables = assemble_market_data(source, {"fixture-security": result}, turnover, identity, units,
                                  source_version="synthetic-v1", signal_as_of=cutoffs(dates), benchmark=benchmark)
    return inputs, keys, dates, tables.stocks, tables.benchmark
