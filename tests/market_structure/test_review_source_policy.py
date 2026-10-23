"""Strict corresponding definitions and independent market-window coverage."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from qbot.research.exploratory.market_structure.diagnostics import (
    RedundancyPolicy,
    diagnose_features,
)
from qbot.research.exploratory.market_structure.features import compute_features
from qbot.research.exploratory.market_structure.store import AsOfStore
from tests.market_structure.test_diagnostics import panel


@pytest.mark.parametrize(
    "field,value",
    [
        ("unit", "USD"),
        ("currency", "USD"),
        ("vendor", "UNVERIFIED"),
    ],
)
@pytest.mark.parametrize("order", ["valid-first", "invalid-first"])
def test_all_matching_definitions_must_agree_on_approved_basis(
    manual, field, value, order
):
    conflict = replace(
        manual.records[0],
        fact_id="other-definition",
        payload=dict(manual.records[0].payload, **{field: value}),
    )
    records = (
        manual.records + [conflict]
        if order == "valid-first"
        else [conflict] + manual.records
    )
    result = compute_features(
        AsOfStore(records), manual.sessions, manual.request, ["FLOW_NET_ACTIVE_RATIO"]
    )
    assert result.values.value.isna().all()
    assert result.values.reason.eq("BLOCKED_SOURCE_DEFINITION").all()
    assert {
        r["fact_id"]
        for r in result.source_trace["feature_records"]
        if r["kind"] == "source_definition"
    } == {"def", "other-definition"}


def test_identical_definitions_are_consistent_and_unrelated_versions_are_ignored(
    manual,
):
    same = replace(manual.records[0], fact_id="independent-identical-evidence")
    unrelated = replace(
        manual.records[0],
        fact_id="other-version",
        definition_version="v999",
        payload=dict(manual.records[0].payload, unit="USD"),
    )
    result = compute_features(
        AsOfStore(manual.records + [same, unrelated]),
        manual.sessions,
        manual.request,
        ["FLOW_NET_ACTIVE_RATIO"],
    )
    assert result.values.reason.eq("OK").all()
    np.testing.assert_allclose(result.values.value, 0.2)
    assert "other-version" not in {
        r["fact_id"] for r in result.source_trace["feature_records"]
    }


def test_future_conflict_cannot_change_old_snapshot_but_blocks_later_cutoff(manual):
    known = manual.request.as_of + pd.Timedelta(seconds=1)
    conflict = replace(
        manual.records[0],
        fact_id="future-conflict",
        published_at=known,
        available_at=known,
        captured_at=known,
        payload=dict(manual.records[0].payload, unit="USD"),
    )
    names = ["FLOW_NET_ACTIVE_RATIO"]
    original = compute_features(manual.store(), manual.sessions, manual.request, names)
    store = AsOfStore(manual.records + [conflict])
    unchanged = compute_features(store, manual.sessions, manual.request, names)
    pd.testing.assert_frame_equal(original.values, unchanged.values, check_exact=True)
    later = compute_features(
        store,
        manual.sessions,
        replace(manual.request, as_of=known, decision_time=known),
        names,
    )
    assert later.values.reason.eq("BLOCKED_SOURCE_DEFINITION").all()


@pytest.mark.parametrize(
    "pair_threshold,window_threshold,expected",
    [
        (0.5, 1.0, "UNDETERMINED"),
        (1.0, 0.5, "HIGHLY_REDUNDANT"),
        (0.5, 0.8, "HIGHLY_REDUNDANT"),
        (0.5, 0.800000001, "UNDETERMINED"),
    ],
)
def test_market_temporal_completeness_uses_window_threshold(
    pair_threshold, window_threshold, expected
):
    values = panel(grain="market", entities=1, windows=20, sessions=3)
    ends = values.observation_end.unique()
    values.loc[
        (values.feature_id == "B") & values.observation_end.isin(ends[-4:]), "value"
    ] = np.nan
    policy = RedundancyPolicy(
        minimum_observation_windows=10,
        minimum_pair_coverage=pair_threshold,
        minimum_window_coverage=window_threshold,
    )
    result = diagnose_features(values, policy)
    row = result["redundancy"].iloc[0]
    assert row.valid_observation_windows == 16
    assert row.total_observation_windows == 20
    assert row.mean_pair_coverage == 0.8
    assert row.status == expected
    assert result["quality"].cross_section_status.eq("NOT_APPLICABLE").all()
