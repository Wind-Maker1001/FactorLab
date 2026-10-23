"""Selected contracts alone determine feature-source reads and provenance."""

from dataclasses import replace

import pandas as pd
import pytest

from factorlab.exploratory.market_structure.contracts import ContractError, digest
from factorlab.exploratory.market_structure.features import compute_features
from factorlab.exploratory.market_structure.registry import build_default_registry
from factorlab.exploratory.market_structure.store import AsOfStore


class ReadGuard(AsOfStore):
    def __init__(self, records, allowed):
        super().__init__(records)
        self.allowed, self.reads = allowed, []

    def snapshot(self, kind, *args, **kwargs):
        assert kind in self.allowed, "unrelated source read: " + kind
        self.reads.append(kind)
        return super().snapshot(kind, *args, **kwargs)


@pytest.mark.parametrize(
    "name,kinds",
    [
        ("BREADTH_ADVANCE_RATIO", {"minute"}),
        ("BREADTH_LIMIT_UP_RATIO", {"minute"}),
        ("SENTIMENT_AMOUNT_HHI", {"minute"}),
        ("FLOW_NET_ACTIVE_RATIO", {"minute", "source_definition"}),
        ("SC_UPSTREAM_COUNT", {"scope", "edge"}),
        ("SC_DOWNSTREAM_COUNT", {"scope", "edge"}),
        ("SC_UPSTREAM_RET", {"scope", "edge", "minute"}),
        ("THEME_MEMBERSHIP_COUNT", {"scope", "membership"}),
    ],
)
def test_only_declared_source_kinds_are_read(manual, name, kinds):
    records = [r for r in manual.records if r.kind in kinds]
    if name.startswith("SC_"):
        records = [
            r
            for r in records
            if r.kind != "scope" or r.payload["family"] == "supply_chain"
        ]
    elif name == "THEME_MEMBERSHIP_COUNT":
        records = [r for r in records if r.payload.get("family") == "concept"]
    guard = ReadGuard(records, kinds)
    result = compute_features(
        guard, manual.sessions, manual.request, [name], include_market_context=False
    )
    assert result.values.value.notna().any()
    assert set(guard.reads) == kinds
    assert {r["kind"] for r in result.source_trace["feature_records"]} <= kinds
    assert result.context["status"] == "MARKET_CONTEXT_NOT_REQUESTED"
    assert (
        result.source_trace["declared_inputs"][name]
        == build_default_registry().get(name).required_inputs
    )


@pytest.mark.parametrize(
    "name", ["BREADTH_ADVANCE_RATIO", "FLOW_NET_ACTIVE_RATIO", "SC_UPSTREAM_COUNT"]
)
def test_unrelated_missing_or_malformed_classification_cannot_abort_or_change_hash(
    manual, name
):
    relevant = [
        r
        for r in manual.records
        if r.kind != "membership"
        and (r.kind != "scope" or r.payload["family"] == "supply_chain")
    ]
    before = compute_features(
        AsOfStore(relevant), manual.sessions, manual.request, [name]
    )
    malformed = [
        replace(r, payload=dict(r.payload, weight=2.0))
        for r in manual.records
        if r.kind == "membership"
    ]
    after = compute_features(
        AsOfStore(relevant + malformed), manual.sessions, manual.request, [name]
    )
    pd.testing.assert_frame_equal(before.values, after.values, check_exact=True)
    assert before.selected_input_sha256 == after.selected_input_sha256
    assert before.source_trace == after.source_trace


def test_unrelated_edges_and_source_definitions_do_not_enter_breadth_hash(manual):
    records = [r for r in manual.records if r.kind == "minute"]
    first = compute_features(
        AsOfStore(records), manual.sessions, manual.request, ["BREADTH_ADVANCE_RATIO"]
    )
    edges = [
        replace(r, payload=dict(r.payload, confidence=9.0))
        for r in manual.records
        if r.kind == "edge"
    ]
    other = replace(
        manual.records[0], fact_id="unrelated-definition", definition_version="v999"
    )
    second = compute_features(
        AsOfStore(records + edges + [other]),
        manual.sessions,
        manual.request,
        ["BREADTH_ADVANCE_RATIO"],
    )
    pd.testing.assert_frame_equal(first.values, second.values, check_exact=True)
    assert first.selected_input_sha256 == second.selected_input_sha256


@pytest.mark.parametrize(
    "family,name", [("industry", "IND_RET"), ("concept", "THEME_MEMBERSHIP_COUNT")]
)
def test_needed_classification_still_validates_weights_and_scope(manual, family, name):
    bad = [
        (
            replace(r, payload=dict(r.payload, weight=0.9))
            if r.kind == "membership" and r.payload["family"] == family
            else r
        )
        for r in manual.records
    ]
    with pytest.raises(ContractError, match="weights"):
        compute_features(AsOfStore(bad), manual.sessions, manual.request, [name])
    missing = [r for r in manual.records if r.payload.get("family") != family]
    with pytest.raises(ContractError, match="scopes"):
        compute_features(AsOfStore(missing), manual.sessions, manual.request, [name])


def test_needed_flow_definition_and_graph_evidence_remain_strict(manual):
    bad = [
        (
            replace(r, payload=dict(r.payload, unit="UNKNOWN"))
            if r.kind == "source_definition"
            else r
        )
        for r in manual.records
    ]
    result = compute_features(
        AsOfStore(bad), manual.sessions, manual.request, ["FLOW_NET_ACTIVE_RATIO"]
    )
    assert result.values.reason.eq("BLOCKED_SOURCE_DEFINITION").all()
    bad = [
        replace(r, payload=dict(r.payload, confidence=9.0)) if r.kind == "edge" else r
        for r in manual.records
    ]
    with pytest.raises(ContractError, match="confidence"):
        compute_features(
            AsOfStore(bad), manual.sessions, manual.request, ["SC_UPSTREAM_COUNT"]
        )


def test_mixed_requests_equal_separate_values_with_explicit_union_trace(manual):
    names = [
        "IND_RET",
        "THEME_MEMBERSHIP_COUNT",
        "SC_UPSTREAM_COUNT",
        "FLOW_NET_ACTIVE_RATIO",
        "BREADTH_ADVANCE_RATIO",
    ]
    combined = compute_features(
        manual.store(),
        manual.sessions,
        manual.request,
        names,
        include_market_context=False,
    )
    union = set()
    for name in names:
        single = compute_features(
            manual.store(),
            manual.sessions,
            manual.request,
            [name],
            include_market_context=False,
        )
        columns = [
            "feature_id",
            "entity_id",
            "namespace",
            "value",
            "reason",
            "input_coverage",
        ]
        pd.testing.assert_frame_equal(
            combined.values.query("feature_id==@name")[columns].reset_index(drop=True),
            single.values[columns].reset_index(drop=True),
            check_exact=True,
        )
        union.update(r["sha256"] for r in single.source_trace["feature_records"])
    assert {r["sha256"] for r in combined.source_trace["feature_records"]} == union
    assert combined.selected_input_sha256 == digest(sorted(union))
    assert len(combined.source_trace["declared_inputs"]) == len(names)


def test_context_trace_is_separate_from_feature_inputs(manual):
    from factorlab.exploratory.market_structure.synthetic import make_record

    record = make_record(
        "context",
        "benchmark",
        dict(session_id="2021-01-04", adj_close=100.0, complete=True),
        manual.sessions[0].segments[-1][1],
        observation_end=manual.sessions[0].segments[-1][1],
    )
    request = replace(
        manual.request, as_of=record.known_at, decision_time=record.known_at
    )
    name = ["BREADTH_ADVANCE_RATIO"]
    without = compute_features(manual.store(), manual.sessions, request, name)
    with_context = compute_features(
        AsOfStore(manual.records + [record]), manual.sessions, request, name
    )
    assert without.selected_input_sha256 == with_context.selected_input_sha256
    assert (
        without.source_trace["context_input_sha256"]
        != with_context.source_trace["context_input_sha256"]
    )
    assert with_context.source_trace["context_records"][0]["fact_id"] == "context"
    assert without.snapshot_id != with_context.snapshot_id


def test_empty_selection_and_implicit_context_flag_fail_closed(manual):
    with pytest.raises(ContractError, match="nonempty"):
        compute_features(manual.store(), manual.sessions, manual.request, [])
    with pytest.raises(ContractError, match="boolean"):
        compute_features(
            manual.store(),
            manual.sessions,
            manual.request,
            ["BREADTH_ADVANCE_RATIO"],
            include_market_context="yes",
        )
