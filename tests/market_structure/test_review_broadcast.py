"""Explicit target stocks and exact historical snapshots for market broadcasts."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from factorlab.exploratory.market_structure.adapters import ResearchFeatureAdapter
from factorlab.exploratory.market_structure.contracts import ContractError
from factorlab.exploratory.market_structure.features import compute_features
from tests.market_structure.test_review_red_cases import target_index


def market_panel(manual):
    first = manual.request
    later = replace(
        first,
        observation_start=first.observation_start + pd.Timedelta(minutes=5),
        observation_end=first.observation_end + pd.Timedelta(minutes=5),
        as_of=first.as_of + pd.Timedelta(minutes=5),
        decision_time=first.decision_time + pd.Timedelta(minutes=5),
    )
    requests = [
        first,
        later,
        replace(first, scenario="alternate"),
        replace(
            first,
            frequency=15,
            observation_end=first.observation_start + pd.Timedelta(minutes=15),
            as_of=first.as_of + pd.Timedelta(minutes=10),
            decision_time=first.decision_time + pd.Timedelta(minutes=10),
        ),
    ]
    targets = pd.concat(
        [target_index(r, ("A", "B")) for r in requests], ignore_index=True
    )
    rows = []
    for r, value in zip(requests, (0.1, np.nan, 0.7, 0.9)):
        row = target_index(r, ("MARKET",)).iloc[0].to_dict()
        rows.append(
            dict(
                row,
                feature_id="MARKET_FEATURE",
                grain="market",
                namespace="source-A",
                value=value,
            )
        )
    return pd.DataFrame(rows), targets


def test_multiwindow_multistock_scenario_frequency_and_missing_value_preserved(manual):
    values, target = market_panel(manual)
    before, target_before = values.copy(deep=True), target.copy(deep=True)
    view = ResearchFeatureAdapter().build_view(
        values, ["MARKET_FEATURE"], target_index=target, broadcast_market=True
    )
    assert len(view) == 8
    np.testing.assert_allclose(
        view.MARKET_FEATURE,
        [0.1, 0.1, np.nan, np.nan, 0.7, 0.7, 0.9, 0.9],
        equal_nan=True,
    )
    assert view.MARKET_FEATURE__broadcast.eq(True).all()
    assert view.attrs["broadcast_features"] == ("MARKET_FEATURE",)
    assert view.attrs["feature_namespaces"] == {"MARKET_FEATURE": "source-A"}
    assert view.attrs["formal_admission"] is view.attrs["forward_allowed"] is False
    pd.testing.assert_frame_equal(values, before)
    pd.testing.assert_frame_equal(target, target_before)


@pytest.mark.parametrize(
    "kind",
    [
        "none",
        "empty",
        "duplicate",
        "missing-time",
        "blank-security",
        "naive",
        "decision-before-asof",
        "wrong-frequency",
        "target-namespace",
    ],
)
def test_unverifiable_target_fails_closed(manual, kind):
    values, target = market_panel(manual)
    if kind == "none":
        target = None
    elif kind == "empty":
        target = target.iloc[:0]
    elif kind == "duplicate":
        target = pd.concat([target, target.iloc[:1]], ignore_index=True)
    elif kind == "missing-time":
        target = target.drop(columns="as_of")
    elif kind == "blank-security":
        target.loc[0, "entity_id"] = ""
    elif kind == "naive":
        target["as_of"] = pd.to_datetime(target.as_of, utc=True).dt.tz_localize(None)
    elif kind == "decision-before-asof":
        target["decision_time"] = target.as_of - pd.Timedelta(seconds=1)
    elif kind == "wrong-frequency":
        target["frequency"] = 30
    else:
        target["namespace"] = "source-B"
    with pytest.raises(ContractError):
        ResearchFeatureAdapter().build_view(
            values, ["MARKET_FEATURE"], target_index=target, broadcast_market=True
        )


@pytest.mark.parametrize(
    "key", ["scenario", "session_id", "as_of", "decision_time", "observation_start"]
)
def test_snapshot_mismatch_is_not_joined_to_another_time(manual, key):
    values, target = market_panel(manual)
    if key in ("scenario", "session_id"):
        target[key] = "DIFFERENT"
    else:
        target[key] += pd.Timedelta(seconds=1)
        if key == "as_of":
            target["decision_time"] = target[key]
    with pytest.raises(ContractError, match="snapshot"):
        ResearchFeatureAdapter().build_view(
            values, ["MARKET_FEATURE"], target_index=target, broadcast_market=True
        )


def test_namespace_requires_explicit_selection_and_does_not_cross_join(manual):
    values, target = market_panel(manual)
    another = values.copy()
    another["namespace"], another["value"] = "source-B", -10.0
    both = pd.concat([values, another], ignore_index=True)
    adapter = ResearchFeatureAdapter()
    with pytest.raises(ContractError, match="namespace"):
        adapter.build_view(
            both, ["MARKET_FEATURE"], target_index=target, broadcast_market=True
        )
    view = adapter.build_view(
        both,
        ["MARKET_FEATURE"],
        target_index=target,
        broadcast_market=True,
        feature_namespaces={"MARKET_FEATURE": "source-B"},
    )
    assert len(view) == len(target) and view.MARKET_FEATURE.eq(-10).all()
    assert view.attrs["feature_namespaces"]["MARKET_FEATURE"] == "source-B"


def test_duplicate_market_rows_and_missing_snapshot_are_rejected(manual):
    values, target = market_panel(manual)
    adapter = ResearchFeatureAdapter()
    with pytest.raises(ContractError, match="ambiguous"):
        adapter.build_view(
            pd.concat([values, values.iloc[:1]]),
            ["MARKET_FEATURE"],
            target_index=target,
            broadcast_market=True,
        )
    with pytest.raises(ContractError, match="snapshot unavailable"):
        adapter.build_view(
            values.iloc[1:],
            ["MARKET_FEATURE"],
            target_index=target,
            broadcast_market=True,
        )


def test_missing_authorization_and_unknown_namespace_selector_are_rejected(manual):
    values, target = market_panel(manual)
    adapter = ResearchFeatureAdapter()
    with pytest.raises(ContractError, match="opt-in"):
        adapter.build_view(values, ["MARKET_FEATURE"], target_index=target)
    with pytest.raises(ContractError, match="unrequested"):
        adapter.build_view(
            values,
            ["MARKET_FEATURE"],
            target_index=target,
            broadcast_market=True,
            feature_namespaces={"OTHER": "source-A"},
        )


def test_mixed_security_and_market_selection_preserves_target_and_values(manual):
    names = ["BREADTH_ADVANCE_RATIO", "SC_UPSTREAM_COUNT"]
    result = compute_features(manual.store(), manual.sessions, manual.request, names)
    target = target_index(manual.request, ("B", "D"))
    view = ResearchFeatureAdapter().build_view(
        result.values, names, target_index=target, broadcast_market=True
    )
    assert view.entity_id.tolist() == ["B", "D"]
    assert view.SC_UPSTREAM_COUNT.tolist() == [2.0, 1.0]
    assert view.BREADTH_ADVANCE_RATIO.eq(0.5).all()
    assert view.attrs["broadcast_features"] == ("BREADTH_ADVANCE_RATIO",)
