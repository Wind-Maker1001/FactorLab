"""Reviewed counterexamples: graph endpoints and opaque session identifiers."""

from dataclasses import replace

import pytest

from qbot.research.exploratory.market_structure.contracts import ContractError
from qbot.research.exploratory.market_structure.features import compute_features
from qbot.research.exploratory.market_structure.store import AsOfStore, window_requests
from qbot.research.exploratory.market_structure.synthetic import make_synthetic_data


def test_empty_endpoint_cannot_be_counted_as_confirmed_supplier(manual):
    records = [replace(r, payload=dict(r.payload, upstream="")) if r.fact_id == "edge-0" else r for r in manual.records]
    with pytest.raises(ContractError, match="endpoint"):
        compute_features(AsOfStore(records), manual.sessions, manual.request, ["SC_UPSTREAM_COUNT"])


def test_opaque_session_ids_do_not_define_market_regime_dates():
    bundle = make_synthetic_data(stock_count=4, session_count=1)
    names = {s.session_id: "session-%d" % i for i, s in enumerate(bundle.context_sessions)}
    sessions = tuple(replace(s, session_id=names[s.session_id]) for s in bundle.context_sessions)
    records = [replace(r, payload=dict(r.payload, session_id=names[r.payload["session_id"]])) if r.kind == "benchmark" else r for r in bundle.records()]
    request = next(window_requests(bundle.sessions, bundle.universe, 5))
    result = compute_features(AsOfStore(records), sessions, replace(request, session_id=names[request.session_id]), ["BREADTH_ADVANCE_RATIO"])
    assert result.context["status"] == "OK"
    assert result.context["benchmark_session"].startswith("session-")
