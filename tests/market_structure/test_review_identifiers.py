"""Calendar dates come from UTC observations; security identities stay explicit."""

from dataclasses import replace

import pandas as pd
import pytest

from factorlab.exploratory.market_structure.adapters import regime_date
from factorlab.exploratory.market_structure.contracts import ContractError
from factorlab.exploratory.market_structure.features import compute_features
from factorlab.exploratory.market_structure.store import (
    AsOfStore,
    Session,
    window_requests,
)
from factorlab.exploratory.market_structure.synthetic import (
    make_record,
    make_synthetic_data,
)


@pytest.mark.parametrize(
    "field,name",
    [("upstream", "SC_UPSTREAM_COUNT"), ("downstream", "SC_DOWNSTREAM_COUNT")],
)
@pytest.mark.parametrize("value", ["", " ", None, 3, False, ["bad"]])
def test_malformed_relevant_relationship_endpoints_fail_closed(
    manual, field, name, value
):
    records = [
        (
            replace(r, payload=dict(r.payload, **{field: value}))
            if r.fact_id == "edge-0"
            else r
        )
        for r in manual.records
    ]
    with pytest.raises(ContractError, match="endpoint"):
        compute_features(AsOfStore(records), manual.sessions, manual.request, [name])


def test_unrelated_invalid_edge_does_not_enter_requested_graph_scope(manual):
    edge = next(r for r in manual.records if r.fact_id == "edge-0")
    extra = replace(
        edge,
        fact_id="unrelated",
        payload=dict(edge.payload, upstream="", downstream="OUTSIDE"),
    )
    before = compute_features(
        manual.store(), manual.sessions, manual.request, ["SC_UPSTREAM_COUNT"]
    )
    after = compute_features(
        AsOfStore(manual.records + [extra]),
        manual.sessions,
        manual.request,
        ["SC_UPSTREAM_COUNT"],
    )
    pd.testing.assert_frame_equal(before.values, after.values, check_exact=True)


@pytest.fixture(scope="module")
def daily_bundle():
    return make_synthetic_data(stock_count=4, session_count=1)


@pytest.mark.parametrize("labels", ["opaque", "unrelated_dates"])
def test_session_labels_do_not_define_regime_dates_or_order(daily_bundle, labels):
    b = daily_bundle
    names = {
        s.session_id: (
            "session-%d" % i
            if labels == "opaque"
            else str((pd.Timestamp("2099-01-01") - pd.Timedelta(days=i)).date())
        )
        for i, s in enumerate(b.context_sessions)
    }
    sessions = tuple(
        replace(s, session_id=names[s.session_id]) for s in b.context_sessions
    )
    records = [
        (
            replace(
                r, payload=dict(r.payload, session_id=names[r.payload["session_id"]])
            )
            if r.kind == "benchmark"
            else r
        )
        for r in b.records()
    ]
    req = next(window_requests(b.sessions, b.universe, 5))
    before = compute_features(
        AsOfStore(b.records()), b.context_sessions, req, ["BREADTH_ADVANCE_RATIO"]
    )
    after = compute_features(
        AsOfStore(records),
        sessions,
        replace(req, session_id=names[req.session_id]),
        ["BREADTH_ADVANCE_RATIO"],
    )
    assert after.context["status"] == "OK"
    columns = [
        "MARKET_REGIME",
        "INDEX_RET5",
        "INDEX_RET20",
        "INDEX_RET60",
        "INDEX_VOL20",
        "missing_session_count",
    ]
    assert {k: before.context[k] for k in columns} == {
        k: after.context[k] for k in columns
    }
    assert before.selected_input_sha256 == after.selected_input_sha256
    assert before.snapshot_id != after.snapshot_id


def test_regime_date_uses_final_observed_instant_of_half_open_utc_session():
    session = Session("opaque", (("2021-01-01T23:00:00Z", "2021-01-02T00:00:00Z"),))
    assert regime_date(session) == pd.Timestamp("2021-01-01")
    assert regime_date(session).tzinfo is None


def test_multiple_completed_sessions_on_one_regime_date_fail_explicitly(manual):
    start = manual.request.observation_start - pd.Timedelta(days=1)
    sessions = (
        Session("one", ((start, start + pd.Timedelta(hours=1)),)),
        Session(
            "two", ((start + pd.Timedelta(hours=2), start + pd.Timedelta(hours=3)),)
        ),
    )
    records = list(manual.records)
    for i, s in enumerate(sessions):
        end = s.segments[-1][1]
        records.append(
            make_record(
                "day-%d" % i,
                "benchmark",
                dict(session_id=s.session_id, adj_close=100.0 + i, complete=True),
                end + pd.Timedelta(seconds=1),
                observation_end=end,
            )
        )
    with pytest.raises(ContractError, match="distinct UTC regime dates"):
        compute_features(
            AsOfStore(records),
            sessions + manual.sessions,
            manual.request,
            ["BREADTH_ADVANCE_RATIO"],
        )


@pytest.mark.parametrize(
    "universe", [None, (), ("",), (" ",), (1,), (False,), (["bad"],)]
)
def test_invalid_requested_security_identifiers_fail_before_hashing(manual, universe):
    with pytest.raises(ContractError, match="universe"):
        replace(manual.request, universe=universe)


@pytest.mark.parametrize(
    "field,value",
    [("session_id", 1), ("scenario", 1), ("session_id", " "), ("scenario", " ")],
)
def test_request_scenario_and_session_labels_are_explicit_strings(manual, field, value):
    with pytest.raises(ContractError, match="universe/session/scenario"):
        replace(manual.request, **{field: value})
