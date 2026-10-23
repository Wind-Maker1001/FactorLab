from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from factorlab.exploratory.market_structure.adapters import ResearchFeatureAdapter
from factorlab.exploratory.market_structure.aggregation import aggregate_intraday
from factorlab.exploratory.market_structure.contracts import (
    ContractError,
    ComputeRequest,
)
from factorlab.exploratory.market_structure.features import compute_features
from factorlab.exploratory.market_structure.registry import build_default_registry
from factorlab.exploratory.market_structure.store import (
    AsOfStore,
    memberships,
    window_requests,
)
from factorlab.exploratory.market_structure.synthetic import make_synthetic_data


def value(result, name, entity):
    return result.values.set_index(["feature_id", "entity_id"]).loc[
        (name, entity), "value"
    ]


def test_all_fourteen_against_independent_hand_arithmetic(manual):
    result = compute_features(manual.store(), manual.sessions, manual.request)
    expected = {
        "IND_RET": {"I0": 0.0, "I1": 0.15, "I2": 0.025},
        "IND_REL_RET": {"A": 0.025, "B": -0.1125, "C": 0.1125, "D": -0.025},
        "IND_AMOUNT_SHARE": {"I0": 0.15, "I1": 0.2, "I2": 0.65},
        "THEME_ADVANCE_RATIO": {"C0": 0.5, "C1": 0.5},
        "THEME_MEMBERSHIP_COUNT": {s: 2.0 for s in manual.universe},
        "SC_UPSTREAM_COUNT": dict(A=0.0, B=2.0, C=0.0, D=1.0),
        "SC_DOWNSTREAM_COUNT": dict(A=1.0, B=1.0, C=1.0, D=0.0),
        "SC_UPSTREAM_RET": dict(B=0.15, D=-0.1),
        "FLOW_NET_ACTIVE_RATIO": {s: 0.2 for s in manual.universe},
        "BREADTH_ADVANCE_RATIO": {"MARKET": 0.5},
        "BREADTH_LIMIT_UP_RATIO": {"MARKET": 0.25},
        "BREADTH_LIMIT_DOWN_RATIO": {"MARKET": 0.25},
        "SENTIMENT_AMOUNT_HHI": {"MARKET": 0.3},
        "SENTIMENT_INDUSTRY_DIFFUSION": {"MARKET": 1.0},
    }
    # Floating point makes I0 a tiny positive. Check diffusion on deliberate nonzero input instead.
    for name, entities in expected.items():
        if name == "SENTIMENT_INDUSTRY_DIFFUSION":
            continue
        for entity, target in entities.items():
            assert value(result, name, entity) == pytest.approx(target, abs=1e-12)
    positive = manual.records.copy()
    positive = [
        (
            replace(r, payload=dict(r.payload, close=11.0, high=11.0))
            if r.kind == "minute"
            and r.payload["security_id"] == "B"
            and r.fact_id.endswith("-4")
            else r
        )
        for r in positive
    ]
    diffusion = compute_features(AsOfStore(positive), manual.sessions, manual.request)
    assert value(diffusion, "SENTIMENT_INDUSTRY_DIFFUSION", "MARKET") == 1.0
    assert np.isnan(value(result, "SC_UPSTREAM_RET", "A"))
    assert result.values.feature_id.nunique() == 14
    assert result.conservation.error.abs().max() < 1e-8
    assert result.conservation.allocated_amount.eq(5000).all()


@pytest.mark.parametrize("frequency", [5, 15, 30])
def test_each_frequency_ohlcv_is_hand_calculable(manual, frequency):
    original = next(r for r in manual.records if r.fact_id == "minute-A-0")
    records = [r for r in manual.records if r.kind != "minute"]
    for s in manual.universe:
        for i in range(frequency):
            start = manual.request.observation_start + pd.Timedelta(minutes=i)
            end = start + pd.Timedelta(minutes=1)
            records.append(
                replace(
                    original,
                    fact_id="%s-%d" % (s, i),
                    valid_from=start,
                    observation_end=end,
                    published_at=end,
                    available_at=end,
                    captured_at=end,
                    payload=dict(
                        original.payload,
                        security_id=s,
                        open=10.0 + i,
                        high=12.0 + i,
                        low=9.0 + i,
                        close=11.0 + i,
                        amount=10.0,
                        buy=6.0,
                        sell=4.0,
                        volume=2.0,
                    ),
                )
            )
    end = manual.request.observation_start + pd.Timedelta(minutes=frequency)
    req = replace(
        manual.request,
        frequency=frequency,
        observation_end=end,
        as_of=end,
        decision_time=end,
    )
    f = aggregate_intraday(AsOfStore(records), manual.sessions, req).frame
    assert f.open.eq(10.0).all() and f.close.eq(10.0 + frequency).all()
    assert f.high.eq(11.0 + frequency).all() and f.low.eq(9.0).all()
    assert f.amount.eq(10 * frequency).all() and f.volume.eq(2 * frequency).all()
    assert (
        compute_features(
            AsOfStore(records), manual.sessions, req
        ).values.feature_id.nunique()
        == 14
    )


@pytest.mark.parametrize("unknown_share", [0.0, 0.2, 1.0])
def test_unclassified_cash_is_reported_and_never_faked_zero(manual, unknown_share):
    changed = []
    for r in manual.records:
        if r.kind == "minute":
            amount = r.payload["amount"]
            r = replace(
                r,
                payload=dict(
                    r.payload,
                    buy=amount * (1 - unknown_share) / 2,
                    sell=amount * (1 - unknown_share) / 2,
                    unclassified=amount * unknown_share,
                ),
            )
        changed.append(r)
    f = compute_features(AsOfStore(changed), manual.sessions, manual.request).values
    cash = f[f.feature_id == "FLOW_NET_ACTIVE_RATIO"]
    assert cash.unclassified_amount_ratio.eq(unknown_share).all()
    if unknown_share:
        assert (
            cash.value.isna().all()
            and cash.classification_status.eq("INCOMPLETE_CLASSIFICATION").all()
        )
    else:
        assert cash.value.eq(0).all()


@pytest.mark.parametrize(
    "problem,reason",
    [
        ("definition", "BLOCKED_SOURCE_DEFINITION"),
        ("unit", "BLOCKED_SOURCE_DEFINITION"),
        ("partition", "INVALID_CASH_PARTITION"),
        ("mixed", "MIXED_SOURCE_DEFINITION"),
        ("zero", "ZERO_DENOMINATOR"),
    ],
)
def test_cash_rejection_reasons(manual, problem, reason):
    records = []
    for r in manual.records:
        if r.kind == "minute":
            p = dict(r.payload)
            if problem == "definition":
                p["flow_definition"] = "主力资金-未定义"
            if problem == "unit":
                p["flow_unit"] = "TEN_THOUSAND_CNY"
            if problem == "partition":
                p["buy"] += 1
            if problem == "mixed" and r.fact_id.endswith("-0"):
                p["flow_definition"] = "OTHER"
            if problem == "zero":
                p.update(amount=0.0, buy=0.0, sell=0.0, unclassified=0.0)
            r = replace(r, payload=p)
        records.append(r)
    cash = aggregate_intraday(AsOfStore(records), manual.sessions, manual.request).frame
    assert cash.flow_reason.eq(reason).all()


@pytest.mark.parametrize("weights", [(0.8, 0.8), (None, 0.5), (-0.1, 1.1)])
def test_invalid_multi_membership_weights_rejected(manual, weights):
    changed, counter = [], 0
    for r in manual.records:
        if (
            r.kind == "membership"
            and r.payload["family"] == "industry"
            and r.payload["security_id"] == "A"
        ):
            r = replace(r, payload=dict(r.payload, weight=weights[counter]))
            counter += 1
        changed.append(r)
    with pytest.raises(ContractError):
        compute_features(AsOfStore(changed), manual.sessions, manual.request)


def test_equal_split_and_multiple_taxonomies_conserve_separately(manual):
    records = []
    for r in manual.records:
        if r.kind == "membership" and r.payload["family"] == "industry":
            records.append(replace(r, payload=dict(r.payload, weight=None)))
            records.append(
                replace(
                    r,
                    fact_id=r.fact_id + "-taxonomy2",
                    payload=dict(r.payload, namespace="industry2", weight=None),
                )
            )
        else:
            records.append(r)
        if r.fact_id == "scope-industry":
            records.append(
                replace(
                    r,
                    fact_id="scope-industry2",
                    payload=dict(r.payload, namespace="industry2"),
                )
            )
    store = AsOfStore(records)
    m, _, _ = memberships(store, "industry", manual.request)
    assert m.weight_origin.eq("GENERATED_EQUAL_SPLIT").all()
    f = compute_features(store, manual.sessions, manual.request)
    checks = f.conservation[f.conservation.family == "industry"]
    assert len(checks) == 2 and checks.allocated_amount.eq(5000).all()
    shares = f.values[f.values.feature_id == "IND_AMOUNT_SHARE"]
    assert np.allclose(shares.groupby("namespace").value.sum(), 1)


def test_catalog_and_adapter_are_not_production_admission(manual):
    registry = build_default_registry()
    assert (
        len(registry.specs) == 32 and sum(s.implemented for s in registry.specs) == 14
    )
    with pytest.raises(ContractError, match="CONTRACT_ONLY"):
        compute_features(manual.store(), manual.sessions, manual.request, ["THEME_RET"])
    f = compute_features(manual.store(), manual.sessions, manual.request)
    adapter = ResearchFeatureAdapter()
    view = adapter.build_view(f.values, ["SC_UPSTREAM_COUNT"])
    assert view.attrs["formal_admission"] is False
    with pytest.raises(ContractError, match="broadcast"):
        adapter.build_view(f.values, ["BREADTH_ADVANCE_RATIO"])
    with pytest.raises(ContractError, match="projection"):
        adapter.build_view(f.values, ["IND_RET"])
    assert len(adapter.model_interfaces) == 4
    from factorlab.factors.registry import FACTOR_REGISTRY, get_factor

    assert len(FACTOR_REGISTRY) == 14
    for spec in registry.specs:
        with pytest.raises(Exception, match="Unknown factor"):
            get_factor(spec.feature_id)


def test_intraday_calendar_has_no_lunch_or_overnight_windows():
    b = make_synthetic_data(stock_count=4, session_count=1)
    assert [
        len(list(window_requests(b.sessions, b.universe, n))) for n in (5, 15, 30)
    ] == [48, 16, 8]
    for req in window_requests(b.sessions, b.universe, 30):
        assert req.observation_end <= req.as_of <= req.decision_time


def test_unclosed_and_misaligned_windows(manual):
    req = replace(
        manual.request, as_of=manual.request.observation_end - pd.Timedelta(seconds=1)
    )
    f = aggregate_intraday(manual.store(), manual.sessions, req).frame
    assert f.reason.eq("WINDOW_NOT_CLOSED").all()
    result = compute_features(manual.store(), manual.sessions, req)
    assert result.values.value.isna().all()
    assert result.values.reason.eq("WINDOW_NOT_CLOSED").all()
    assert result.values.input_coverage.eq(0).all()
    with pytest.raises(ContractError, match="decision_time"):
        replace(
            manual.request, as_of=manual.request.decision_time + pd.Timedelta(seconds=1)
        )
    with pytest.raises(ContractError, match="aligned"):
        req = replace(
            manual.request,
            observation_start=manual.request.observation_start
            + pd.Timedelta(minutes=1),
            observation_end=manual.request.observation_end + pd.Timedelta(minutes=1),
        )
        aggregate_intraday(manual.store(), manual.sessions, req)


def test_inputs_are_immutable_and_repeatable(manual):
    before = tuple(r.sha256 for r in manual.records)
    first = compute_features(manual.store(), manual.sessions, manual.request)
    second = compute_features(manual.store(), manual.sessions, manual.request)
    pd.testing.assert_frame_equal(first.values, second.values, check_exact=True)
    assert first.snapshot_id == second.snapshot_id
    assert before == tuple(r.sha256 for r in manual.records)
    with pytest.raises(TypeError):
        manual.records[0].payload["currency"] = "OTHER"
