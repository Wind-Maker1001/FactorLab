"""Price, amount, and cash validation remain independent in mixed requests."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from factorlab.exploratory.market_structure.features import compute_features
from factorlab.exploratory.market_structure.store import AsOfStore


@pytest.mark.parametrize(
    "field,value",
    [
        ("open", "INVALID"),
        ("high", "INVALID"),
        ("low", -1),
        ("close", "INVALID"),
        ("volume", -1),
        ("price_basis", "UNKNOWN"),
        ("low", 1000),
    ],
)
def test_amount_features_ignore_bad_prices_in_solo_and_mixed_requests(
    manual, field, value
):
    records = [
        (
            replace(r, payload=dict(r.payload, **{field: value}))
            if r.kind == "minute"
            else r
        )
        for r in manual.records
    ]
    store = AsOfStore(records)
    names = ["FLOW_NET_ACTIVE_RATIO", "SENTIMENT_AMOUNT_HHI", "IND_AMOUNT_SHARE"]
    mixed = compute_features(store, manual.sessions, manual.request)
    for name in names:
        solo = compute_features(store, manual.sessions, manual.request, [name])
        actual = mixed.values.query("feature_id==@name").reset_index(drop=True)
        columns = [
            "feature_id",
            "entity_id",
            "namespace",
            "value",
            "reason",
            "input_coverage",
        ]
        pd.testing.assert_frame_equal(
            actual[columns], solo.values[columns], check_exact=True
        )
        assert actual.value.notna().all() and actual.reason.eq("OK").all()
    assert mixed.values.query("feature_id=='BREADTH_ADVANCE_RATIO'").value.isna().all()
    assert mixed.conservation.error.abs().max() < 1e-8
    np.testing.assert_allclose(
        mixed.values.query("feature_id=='FLOW_NET_ACTIVE_RATIO'").value, 0.2
    )
    assert mixed.values.query("feature_id=='SENTIMENT_AMOUNT_HHI'").value.iloc[
        0
    ] == pytest.approx(0.3)


def test_price_only_ignores_invalid_amount_and_cash_fields(manual):
    records = [
        (
            replace(
                r,
                payload=dict(
                    r.payload,
                    amount={"invalid": True},
                    buy="BAD",
                    flow_definition={"undefined": True},
                ),
            )
            if r.kind == "minute"
            else r
        )
        for r in manual.records
    ]
    result = compute_features(
        AsOfStore(records), manual.sessions, manual.request, ["BREADTH_ADVANCE_RATIO"]
    )
    assert result.values.value.iloc[0] == pytest.approx(0.5)


def test_invalid_amount_does_not_hide_valid_prices_but_blocks_amount_features(manual):
    records = [
        replace(r, payload=dict(r.payload, amount=-1)) if r.kind == "minute" else r
        for r in manual.records
    ]
    result = compute_features(AsOfStore(records), manual.sessions, manual.request)
    assert result.values.query("feature_id=='BREADTH_ADVANCE_RATIO'").value.iloc[
        0
    ] == pytest.approx(0.5)
    assert (
        result.values.query("feature_id=='FLOW_NET_ACTIVE_RATIO'")
        .reason.eq("INVALID_AMOUNT_INPUT")
        .all()
    )
    assert result.values.query("feature_id=='SENTIMENT_AMOUNT_HHI'").value.isna().all()


def test_cash_partition_failure_does_not_invalidate_amount_or_prices(manual):
    records = [
        (
            replace(r, payload=dict(r.payload, buy=r.payload["amount"] + 1))
            if r.kind == "minute"
            else r
        )
        for r in manual.records
    ]
    result = compute_features(AsOfStore(records), manual.sessions, manual.request)
    assert (
        result.values.query("feature_id=='FLOW_NET_ACTIVE_RATIO'")
        .reason.eq("INVALID_CASH_PARTITION")
        .all()
    )
    assert result.values.query("feature_id=='SENTIMENT_AMOUNT_HHI'").value.iloc[
        0
    ] == pytest.approx(0.3)
    assert result.values.query("feature_id=='BREADTH_ADVANCE_RATIO'").value.iloc[
        0
    ] == pytest.approx(0.5)


def test_amount_only_missing_minutes_are_not_filled_with_zero(manual):
    records = [r for r in manual.records if r.fact_id != "minute-A-0"]
    result = compute_features(
        AsOfStore(records),
        manual.sessions,
        manual.request,
        ["FLOW_NET_ACTIVE_RATIO", "SENTIMENT_AMOUNT_HHI"],
    )
    assert (
        result.values.query("feature_id=='FLOW_NET_ACTIVE_RATIO' and entity_id=='A'")
        .value.isna()
        .all()
    )
    assert result.values.query("feature_id=='SENTIMENT_AMOUNT_HHI'").value.isna().all()
