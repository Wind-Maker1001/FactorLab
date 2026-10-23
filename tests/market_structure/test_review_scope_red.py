"""Minimal counterexamples for the e6d48d1 follow-up review."""

from dataclasses import replace

import pytest

from factorlab.exploratory.market_structure.contracts import ContractError
from factorlab.exploratory.market_structure.features import compute_features
from factorlab.exploratory.market_structure.store import AsOfStore


def test_no_amount_request_cannot_claim_zero_conservation(manual):
    result = compute_features(
        manual.store(), manual.sessions, manual.request, ["THEME_MEMBERSHIP_COUNT"]
    )
    assert result.conservation.empty


def test_complete_graph_scopes_cannot_be_merged_implicitly(manual):
    scope = next(
        r for r in manual.records
        if r.kind == "scope" and r.payload["family"] == "supply_chain"
    )
    other = replace(scope, fact_id="different-graph", payload=dict(scope.payload, namespace="other"))
    with pytest.raises(ContractError, match="ambiguous supply-chain scope"):
        compute_features(
            AsOfStore(manual.records + [other]), manual.sessions, manual.request,
            ["SC_UPSTREAM_COUNT"]
        )
