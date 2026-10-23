"""No absent-input attestations or implicit unions of supply graph scopes."""

from dataclasses import replace

import pandas as pd
import pytest

from factorlab.exploratory.market_structure.contracts import ContractError
from factorlab.exploratory.market_structure.features import compute_features
from factorlab.exploratory.market_structure.store import AsOfStore


def graph_scope(manual):
    return next(r for r in manual.records if r.fact_id == "scope-supply_chain")


@pytest.mark.parametrize("name", ["IND_RET", "IND_REL_RET", "THEME_MEMBERSHIP_COUNT"])
def test_classification_without_amounts_has_no_conservation_attestation(manual, name):
    result = compute_features(manual.store(), manual.sessions, manual.request, [name])
    assert result.conservation.empty


@pytest.mark.parametrize("invalid_amount", [False, True])
def test_conservation_distinguishes_verified_zero_from_incomplete_inputs(
    manual, invalid_amount
):
    records = [
        (
            replace(
                r, payload=dict(r.payload, amount=(-1.0 if invalid_amount else 0.0))
            )
            if r.kind == "minute"
            else r
        )
        for r in manual.records
    ]
    result = compute_features(
        AsOfStore(records), manual.sessions, manual.request, ["IND_AMOUNT_SHARE"]
    )
    assert not result.conservation.empty
    assert result.conservation.status.eq(
        "INCOMPLETE_AMOUNT_COVERAGE" if invalid_amount else "VERIFIED"
    ).all()
    assert result.conservation.market_amount.eq(0.0).all()


@pytest.mark.parametrize(
    "field,value", [("namespace", "another"), ("taxonomy_version", "v2")]
)
def test_different_graph_identities_rejected_even_when_each_is_complete(
    manual, field, value
):
    scope = graph_scope(manual)
    conflict = replace(
        scope, fact_id="conflict", payload=dict(scope.payload, **{field: value})
    )
    with pytest.raises(ContractError, match="ambiguous supply-chain scope"):
        compute_features(
            AsOfStore(manual.records + [conflict]),
            manual.sessions,
            manual.request,
            ["SC_UPSTREAM_COUNT", "SC_DOWNSTREAM_COUNT", "SC_UPSTREAM_RET"],
        )


@pytest.mark.parametrize(
    "name", ["SC_UPSTREAM_COUNT", "SC_DOWNSTREAM_COUNT", "SC_UPSTREAM_RET"]
)
def test_consistent_duplicate_scope_uses_its_actual_namespace(manual, name):
    scope = graph_scope(manual)
    same = replace(scope, fact_id="same-identity")
    before = compute_features(manual.store(), manual.sessions, manual.request, [name])
    after = compute_features(
        AsOfStore(manual.records + [same]), manual.sessions, manual.request, [name]
    )
    assert after.values.namespace.eq(scope.payload["namespace"]).all()
    pd.testing.assert_series_equal(before.values.value, after.values.value)
    assert before.selected_input_sha256 != after.selected_input_sha256


def test_future_graph_conflict_does_not_change_original_snapshot(manual):
    scope = graph_scope(manual)
    known = manual.request.as_of + pd.Timedelta(seconds=1)
    future = replace(
        scope,
        fact_id="future-scope",
        published_at=known,
        available_at=known,
        captured_at=known,
        payload=dict(scope.payload, namespace="future"),
    )
    names = ["SC_UPSTREAM_COUNT"]
    before = compute_features(manual.store(), manual.sessions, manual.request, names)
    store = AsOfStore(manual.records + [future])
    after = compute_features(store, manual.sessions, manual.request, names)
    pd.testing.assert_frame_equal(before.values, after.values, check_exact=True)
    assert before.source_trace == after.source_trace
    with pytest.raises(ContractError, match="ambiguous supply-chain scope"):
        compute_features(
            store,
            manual.sessions,
            replace(manual.request, as_of=known, decision_time=known),
            names,
        )


@pytest.mark.parametrize("field", ["namespace", "taxonomy_version"])
def test_missing_graph_identity_fails_closed(manual, field):
    scope = graph_scope(manual)
    bad = replace(scope, payload=dict(scope.payload, **{field: ""}))
    records = [bad if r.fact_id == scope.fact_id else r for r in manual.records]
    with pytest.raises(ContractError, match="namespace/version required"):
        compute_features(
            AsOfStore(records), manual.sessions, manual.request, ["SC_UPSTREAM_COUNT"]
        )


@pytest.mark.parametrize(
    "binding",
    [{"namespace": "supply_chain"}, {"namespace": "other", "taxonomy_version": "v1"}],
)
def test_explicit_edge_binding_must_match_unambiguous_graph(manual, binding):
    records = [
        replace(r, payload=dict(r.payload, **binding)) if r.kind == "edge" else r
        for r in manual.records
    ]
    with pytest.raises(ContractError, match="edge.*scope|edge namespace/version"):
        compute_features(
            AsOfStore(records), manual.sessions, manual.request, ["SC_UPSTREAM_COUNT"]
        )


def test_matching_explicit_edges_and_missing_scope_have_distinct_results(manual):
    scope = graph_scope(manual)
    records = [
        (
            replace(
                r,
                payload=dict(
                    r.payload,
                    namespace=scope.payload["namespace"],
                    taxonomy_version="v1",
                ),
            )
            if r.kind == "edge"
            else r
        )
        for r in manual.records
    ]
    result = compute_features(
        AsOfStore(records), manual.sessions, manual.request, ["SC_UPSTREAM_COUNT"]
    )
    assert result.values.reason.eq("OK").all()
    empty = compute_features(
        AsOfStore([r for r in manual.records if r.fact_id != scope.fact_id]),
        manual.sessions,
        manual.request,
        ["SC_UPSTREAM_COUNT"],
    )
    assert empty.values.value.isna().all()
    assert empty.values.namespace.eq("UNAVAILABLE_SUPPLY_SCOPE").all()
    assert empty.values.reason.eq("INCOMPLETE_GRAPH_SCOPE").all()
