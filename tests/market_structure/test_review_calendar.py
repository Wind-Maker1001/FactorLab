"""Taxonomy identifiers and applicable calendar grids are explicit inputs."""

from dataclasses import replace

import pandas as pd
import pytest

from factorlab.exploratory.market_structure.contracts import ContractError
from factorlab.exploratory.market_structure.features import compute_features
from factorlab.exploratory.market_structure.store import (
    AsOfStore,
    Session,
    window_requests,
)
from factorlab.exploratory.market_structure.synthetic import make_synthetic_data


@pytest.mark.parametrize(
    "family,name", [("industry", "IND_RET"), ("concept", "THEME_MEMBERSHIP_COUNT")]
)
@pytest.mark.parametrize(
    "groups", ["C0", ["C0", "C0"], [], [None], [7], [""], [" "], [False], {"C0": 1}]
)
def test_malformed_group_scope_cannot_assert_completeness(manual, family, name, groups):
    records = [
        (
            replace(r, payload=dict(r.payload, group_ids=groups))
            if r.kind == "scope" and r.payload["family"] == family
            else r
        )
        for r in manual.records
        if not (r.kind == "membership" and r.payload["family"] == family)
    ]
    with pytest.raises(ContractError, match="taxonomy group IDs"):
        compute_features(AsOfStore(records), manual.sessions, manual.request, [name])


def test_valid_groups_have_unique_native_feature_keys_and_future_invalid_revision_is_invisible(
    manual,
):
    scope = next(r for r in manual.records if r.fact_id == "scope-industry")
    known = manual.request.as_of + pd.Timedelta(seconds=1)
    future = replace(
        scope,
        revision=2,
        published_at=known,
        available_at=known,
        captured_at=known,
        payload=dict(scope.payload, group_ids=["I0", "I0"]),
    )
    before = compute_features(
        manual.store(), manual.sessions, manual.request, ["IND_RET"]
    )
    store = AsOfStore(manual.records + [future])
    after = compute_features(store, manual.sessions, manual.request, ["IND_RET"])
    pd.testing.assert_frame_equal(before.values, after.values, check_exact=True)
    assert not after.values.duplicated(["feature_id", "entity_id", "namespace"]).any()
    with pytest.raises(ContractError, match="taxonomy group IDs"):
        compute_features(
            store,
            manual.sessions,
            replace(manual.request, as_of=known, decision_time=known),
            ["IND_RET"],
        )


@pytest.fixture(scope="module")
def calendar_bundle():
    return make_synthetic_data(stock_count=4, session_count=1)


def calculate(bundle, sessions, records=None, include_context=True):
    request = next(window_requests(bundle.sessions, bundle.universe, 5))
    return compute_features(
        AsOfStore(bundle.records() if records is None else records),
        sessions,
        request,
        ["BREADTH_ADVANCE_RATIO"],
        include_market_context=include_context,
    )


def test_calendar_order_and_timezone_representation_do_not_change_snapshot(
    calendar_bundle,
):
    b = calendar_bundle
    original = calculate(b, b.context_sessions)
    equivalent = [
        Session(
            s.session_id,
            tuple(
                (a.tz_convert("Asia/Shanghai"), z.tz_convert("Asia/Shanghai"))
                for a, z in s.segments
            ),
        )
        for s in reversed(b.context_sessions)
    ]
    reordered = calculate(b, equivalent)
    pd.testing.assert_frame_equal(original.values, reordered.values, check_exact=True)
    assert original.source_trace == reordered.source_trace


def test_actual_segment_boundaries_bind_context_even_when_regime_values_match(
    calendar_bundle,
):
    b = calendar_bundle
    first = b.context_sessions[0]
    changed = replace(
        first,
        segments=(
            (first.segments[0][0] + pd.Timedelta(minutes=1), first.segments[0][1]),
        )
        + first.segments[1:],
    )
    before = calculate(b, b.context_sessions)
    after = calculate(b, (changed,) + b.context_sessions[1:])
    assert before.selected_input_sha256 == after.selected_input_sha256
    assert (
        before.source_trace["context_records"] == after.source_trace["context_records"]
    )
    assert (
        before.source_trace["context_calendar_sha256"]
        != after.source_trace["context_calendar_sha256"]
    )
    assert before.snapshot_id != after.snapshot_id


def test_future_calendar_addition_and_disabled_context_do_not_bind_unused_inputs(
    calendar_bundle,
):
    b = calendar_bundle
    future_start = b.sessions[-1].segments[0][0] + pd.Timedelta(days=10)
    future = Session(
        "2021-01-14", ((future_start, future_start + pd.Timedelta(minutes=60)),)
    )
    before = calculate(b, b.context_sessions)
    after = calculate(b, b.context_sessions + (future,))
    pd.testing.assert_frame_equal(before.values, after.values, check_exact=True)
    without = calculate(b, b.context_sessions, include_context=False)
    changed = calculate(b, b.context_sessions[1:], include_context=False)
    pd.testing.assert_frame_equal(without.values, changed.values, check_exact=True)
    assert without.source_trace["context_calendar_sha256"] is None


def test_unavailable_context_still_has_an_explicit_calendar_identity(calendar_bundle):
    b = calendar_bundle
    records = [r for r in b.records() if r.kind != "benchmark"]
    before = calculate(b, b.context_sessions, records)
    after = calculate(b, b.context_sessions[1:], records)
    assert (
        before.context["status"]
        == after.context["status"]
        == "MARKET_CONTEXT_UNAVAILABLE"
    )
    assert before.snapshot_id != after.snapshot_id
    assert (
        before.source_trace["context_records"]
        == after.source_trace["context_records"]
        == []
    )


@pytest.mark.parametrize("identity", ["", " ", 1, False])
def test_invalid_calendar_identity_fails_closed(manual, identity):
    with pytest.raises(ContractError, match="session ID"):
        replace(manual.sessions[0], session_id=identity)


def test_duplicate_calendar_cannot_silently_override_a_grid_session(calendar_bundle):
    b = calendar_bundle
    with pytest.raises(ContractError, match="duplicate session ID"):
        calculate(b, b.context_sessions + (b.context_sessions[0],))
