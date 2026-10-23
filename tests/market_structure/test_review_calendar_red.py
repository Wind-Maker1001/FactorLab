"""Counterexamples for malformed taxonomy groups and unbound calendar grids."""

from dataclasses import replace

import pytest

from qbot.research.exploratory.market_structure.contracts import ContractError
from qbot.research.exploratory.market_structure.features import compute_features
from qbot.research.exploratory.market_structure.store import AsOfStore, window_requests
from qbot.research.exploratory.market_structure.synthetic import make_synthetic_data


def test_group_string_cannot_prove_complete_empty_taxonomy(manual):
    records = [replace(r, payload=dict(r.payload, group_ids="C0"))
               if r.fact_id == "scope-concept" else r for r in manual.records
               if not (r.kind == "membership" and r.payload["family"] == "concept")]
    with pytest.raises(ContractError, match="taxonomy group IDs"):
        compute_features(AsOfStore(records), manual.sessions, manual.request, ["THEME_MEMBERSHIP_COUNT"])


def test_different_used_calendar_grids_cannot_share_context_snapshot():
    bundle = make_synthetic_data(stock_count=4, session_count=1)
    missing = bundle.context_sessions[-4].session_id
    records = [r for r in bundle.records() if not (r.kind == "benchmark" and r.payload["session_id"] == missing)]
    request = next(window_requests(bundle.sessions, bundle.universe, 5))
    store = AsOfStore(records)
    before = compute_features(store, bundle.context_sessions, request, ["BREADTH_ADVANCE_RATIO"])
    after = compute_features(store, [s for s in bundle.context_sessions if s.session_id != missing], request, ["BREADTH_ADVANCE_RATIO"])
    assert before.context["missing_session_count"] != after.context["missing_session_count"]
    assert before.selected_input_sha256 == after.selected_input_sha256
    assert before.source_trace["context_input_sha256"] != after.source_trace["context_input_sha256"]
    assert before.snapshot_id != after.snapshot_id
