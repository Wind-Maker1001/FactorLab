"""Counterexamples from the final-head re-review; first run red on f1a4eb3."""

from dataclasses import replace

import numpy as np

from qbot.research.exploratory.market_structure.diagnostics import (
    RedundancyPolicy, diagnose_features,
)
from qbot.research.exploratory.market_structure.features import compute_features
from qbot.research.exploratory.market_structure.store import AsOfStore
from tests.market_structure.test_diagnostics import panel


def test_review_red_conflicting_flow_definitions_are_not_approved(manual):
    conflict = replace(
        manual.records[0], fact_id="conflicting-definition",
        payload=dict(manual.records[0].payload, unit="UNKNOWN"),
    )
    result = compute_features(
        AsOfStore(manual.records + [conflict]), manual.sessions, manual.request,
        ["FLOW_NET_ACTIVE_RATIO"],
    )
    assert result.values.reason.eq("BLOCKED_SOURCE_DEFINITION").all()
    assert result.values.value.isna().all()


def test_review_red_market_window_coverage_is_independent_of_entity_pair_policy():
    values = panel(grain="market", entities=1, windows=20, sessions=3)
    ends = values.observation_end.unique()
    values.loc[(values.feature_id == "B") & values.observation_end.isin(ends[-4:]), "value"] = np.nan
    result = diagnose_features(values, RedundancyPolicy(
        minimum_observation_windows=10, minimum_pair_coverage=0.5,
        minimum_window_coverage=1.0,
    ))
    assert result["redundancy"].iloc[0].status == "UNDETERMINED"
