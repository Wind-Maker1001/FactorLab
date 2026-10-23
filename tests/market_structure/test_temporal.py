from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from qbot.research.exploratory.market_structure.adapters import market_context
from qbot.research.exploratory.market_structure.aggregation import aggregate_intraday
from qbot.research.exploratory.market_structure.contracts import ContractError
from qbot.research.exploratory.market_structure.features import compute_features
from qbot.research.exploratory.market_structure.store import (
    AsOfStore,
    memberships,
    window_requests,
)
from qbot.research.exploratory.market_structure.synthetic import (
    make_record,
    make_synthetic_data,
)


@pytest.mark.parametrize(
    "kind", ["membership", "edge", "minute", "source_definition", "scope"]
)
def test_future_revisions_and_withdrawals_cannot_change_original_snapshot(manual, kind):
    first = compute_features(manual.store(), manual.sessions, manual.request)
    original = next(r for r in manual.records if r.kind == kind)
    future = pd.Timestamp("2026-01-01T00:00:00Z")
    replacement = replace(
        original,
        revision=2,
        published_at=future,
        available_at=future,
        captured_at=future,
        withdrawn=True,
    )
    store = AsOfStore(manual.records + [replacement])
    after = compute_features(store, manual.sessions, manual.request)
    pd.testing.assert_frame_equal(first.values, after.values, check_exact=True)
    assert first.selected_input_sha256 == after.selected_input_sha256
    assert first.snapshot_id == after.snapshot_id


def test_latest_revision_is_selected_before_effective_interval(manual):
    original = next(r for r in manual.records if r.kind == "membership")
    later = manual.request.as_of
    # New knowledge says this assertion is not effective until 2022.
    revision = replace(
        original,
        revision=2,
        valid_from=pd.Timestamp("2022-01-01T00:00:00Z"),
        published_at=later,
        available_at=later,
        captured_at=later,
    )
    selected = AsOfStore(manual.records + [revision]).snapshot(
        "membership", manual.request.observation_end, later
    )
    assert original.fact_id not in {r.fact_id for r in selected}


def test_late_old_version_does_not_override_known_new_revision(manual):
    original = next(r for r in manual.records if r.kind == "membership")
    later_old = replace(
        original, published_at=manual.request.as_of, available_at=manual.request.as_of
    )
    newer = replace(original, revision=2, withdrawn=True)
    selected = AsOfStore([later_old, newer]).snapshot(
        "membership", manual.request.observation_end, manual.request.as_of
    )
    assert not selected


def test_2026_classification_cannot_backfill_2021_without_availability_proof(manual):
    future = pd.Timestamp("2026-01-01T00:00:00Z")
    original = next(r for r in manual.records if r.kind == "membership")
    backdated = replace(
        original,
        revision=2,
        payload=dict(original.payload, group_id="FUTURE_2026"),
        captured_at=future,
        historical_availability_proven=False,
    )
    known = AsOfStore([backdated]).snapshot(
        "membership", manual.request.observation_end, manual.request.as_of
    )
    assert not known and backdated.known_at == future


def test_publication_delay_and_exact_knowledge_boundary(manual):
    # At end+0s, last minute exists but is not published until end+1s.
    at_end = replace(manual.request, as_of=manual.request.observation_end)
    unavailable = aggregate_intraday(manual.store(), manual.sessions, at_end).frame
    assert unavailable.reason.eq("MISSING_OR_NOT_YET_AVAILABLE_MINUTES").all()
    at_publish = replace(
        manual.request, as_of=manual.request.observation_end + pd.Timedelta(seconds=1)
    )
    assert (
        aggregate_intraday(manual.store(), manual.sessions, at_publish)
        .frame.reason.eq("OK")
        .all()
    )


def test_late_minute_is_missing_then_visible_only_to_later_decision(manual):
    original = next(r for r in manual.records if r.fact_id == "minute-A-4")
    later = manual.request.as_of + pd.Timedelta(minutes=10)
    late = replace(original, published_at=later, available_at=later, captured_at=later)
    records = [late if r == original else r for r in manual.records]
    store = AsOfStore(records)
    old = compute_features(store, manual.sessions, manual.request)
    assert np.isnan(
        old.values.query(
            "feature_id=='FLOW_NET_ACTIVE_RATIO' and entity_id=='A'"
        ).value.iloc[0]
    )
    with pytest.raises(ContractError, match="decision_time"):
        replace(manual.request, as_of=later)
    new = compute_features(
        store,
        manual.sessions,
        replace(manual.request, as_of=later, decision_time=later),
    )
    assert new.snapshot_id != old.snapshot_id
    assert new.values.query(
        "feature_id=='FLOW_NET_ACTIVE_RATIO' and entity_id=='A'"
    ).value.iloc[0] == pytest.approx(0.2)


@pytest.fixture(scope="module")
def intraday():
    return make_synthetic_data(stock_count=4, session_count=1)


@pytest.mark.parametrize("future_close", [1e100, None])
def test_same_day_future_close_perturbation_leaves_all_historical_minutes_identical(
    intraday, future_close
):
    b = intraday
    today = b.sessions[0].session_id
    changed = [
        (
            replace(r, payload=dict(r.payload, adj_close=future_close))
            if r.kind == "benchmark" and r.payload["session_id"] == today
            else r
        )
        for r in b.records()
    ]
    old, new = AsOfStore(b.records()), AsOfStore(changed)
    for frequency in (5, 15, 30):
        for request in window_requests(b.sessions, b.universe, frequency):
            before = compute_features(old, b.context_sessions, request)
            after = compute_features(new, b.context_sessions, request)
            pd.testing.assert_frame_equal(before.values, after.values, check_exact=True)
            assert before.context == after.context
            assert before.selected_input_sha256 == after.selected_input_sha256
            assert before.context["benchmark_session"] != today


def test_daily_close_after_completion_still_waits_for_actual_publication(intraday):
    b = intraday
    req = list(window_requests(b.sessions, b.universe, 30))[-1]
    store = AsOfStore(b.records())
    before, _ = market_context(store, b.context_sessions, req)
    assert before["benchmark_session"] != b.sessions[0].session_id
    publication = b.sessions[0].segments[-1][1] + pd.Timedelta(minutes=1)
    after, _ = market_context(
        store,
        b.context_sessions,
        replace(req, as_of=publication, decision_time=publication),
    )
    assert after["benchmark_session"] == b.sessions[0].session_id
    assert pd.Timestamp(after["available_at"]) <= publication


def test_market_grid_does_not_compress_missing_daily_session(intraday):
    b = intraday
    missing = b.context_sessions[-4].session_id
    records = [
        r
        for r in b.records()
        if not (r.kind == "benchmark" and r.payload["session_id"] == missing)
    ]
    request = next(window_requests(b.sessions, b.universe, 5))
    result, _ = market_context(AsOfStore(records), b.context_sessions, request)
    assert result["missing_session_count"] == 1
    assert np.isnan(result["INDEX_VOL20"])


def test_industry_concept_and_supply_switch_fixture_has_historical_versions():
    b = make_synthetic_data(stock_count=4, session_count=3)
    store = AsOfStore(b.records())
    first = next(window_requests(b.sessions[:1], b.universe, 5))
    last = next(window_requests(b.sessions[-1:], b.universe, 5))
    m1, _, _ = memberships(store, "industry", first)
    m2, _, _ = memberships(store, "industry", last)
    assert m1[m1.security_id == b.universe[1]].group_id.tolist() == ["I1"]
    assert m2[m2.security_id == b.universe[1]].group_id.tolist() == ["I2"]
    c1, _, _ = memberships(store, "concept", first)
    c2, _, _ = memberships(store, "concept", last)
    assert len(c1[c1.security_id == b.universe[2]]) == 2
    assert len(c2[c2.security_id == b.universe[2]]) == 1
    e1 = store.snapshot("edge", first.observation_end, first.as_of)
    e2 = store.snapshot("edge", last.observation_end, last.as_of)
    assert "edge-0" in {r.fact_id for r in e1} and "edge-0" not in {
        r.fact_id for r in e2
    }
    assert "edge-switch" in {r.fact_id for r in e2}


def test_unknown_scope_never_becomes_zero_relationships(manual):
    records = [
        (
            replace(r, payload=dict(r.payload, complete=False))
            if r.fact_id == "scope-supply_chain"
            else r
        )
        for r in manual.records
    ]
    f = compute_features(AsOfStore(records), manual.sessions, manual.request).values
    assert (
        f[f.feature_id.isin(["SC_UPSTREAM_COUNT", "SC_DOWNSTREAM_COUNT"])]
        .value.isna()
        .all()
    )


def test_contract_rejects_naive_time_real_source_and_duplicate_revision(manual):
    with pytest.raises(ContractError, match="timezone"):
        replace(manual.request, as_of="2021-01-04")
    with pytest.raises(ContractError, match="REAL_RESEARCH_BLOCKED"):
        replace(manual.request, synthetic_only=False)
    with pytest.raises(ContractError, match="duplicate"):
        AsOfStore(manual.records + [manual.records[0]])
    with pytest.raises(ContractError, match="synthetic"):
        make_record("real", "scope", {}, manual.request.as_of, source="REAL_VENDOR")
