"""Classification identity validation remains scoped to historical dependencies."""

from dataclasses import replace

import pandas as pd
import pytest

from qbot.research.exploratory.market_structure.contracts import ContractError
from qbot.research.exploratory.market_structure.features import compute_features
from qbot.research.exploratory.market_structure.store import AsOfStore


@pytest.mark.parametrize(
    "family,name", [("industry", "IND_RET"), ("concept", "THEME_MEMBERSHIP_COUNT")]
)
@pytest.mark.parametrize("field", ["namespace", "taxonomy_version"])
@pytest.mark.parametrize("value", ["", "  ", None, 0, False, []])
def test_scope_identifiers_require_nonempty_strings_even_without_members(
    manual, family, name, field, value
):
    records = [
        (
            replace(r, payload=dict(r.payload, **{field: value}))
            if r.kind == "scope" and r.payload["family"] == family
            else r
        )
        for r in manual.records
        if not (r.kind == "membership" and r.payload["family"] == family)
    ]
    with pytest.raises(ContractError, match="scope namespace/version"):
        compute_features(AsOfStore(records), manual.sessions, manual.request, [name])


@pytest.mark.parametrize(
    "family,name", [("industry", "IND_RET"), ("concept", "THEME_MEMBERSHIP_COUNT")]
)
def test_valid_identified_empty_classification_retains_its_semantics(
    manual, family, name
):
    records = [
        r
        for r in manual.records
        if not (r.kind == "membership" and r.payload["family"] == family)
    ]
    result = compute_features(
        AsOfStore(records), manual.sessions, manual.request, [name]
    )
    assert result.values.namespace.eq(family).all()
    if family == "concept":
        assert result.values.value.eq(0).all()
        assert result.values.reason.eq("OK").all()
    else:
        assert result.values.value.isna().all()
    assert result.conservation.empty


@pytest.mark.parametrize(
    "family,name", [("industry", "IND_RET"), ("concept", "THEME_MEMBERSHIP_COUNT")]
)
def test_later_invalid_revision_cannot_contaminate_old_classification_snapshot(
    manual, family, name
):
    original = next(
        r for r in manual.records if r.kind == "scope" and r.payload["family"] == family
    )
    known = manual.request.as_of + pd.Timedelta(seconds=1)
    revision = replace(
        original,
        revision=2,
        published_at=known,
        available_at=known,
        captured_at=known,
        payload=dict(original.payload, taxonomy_version=""),
    )
    store = AsOfStore(manual.records + [revision])
    before = compute_features(manual.store(), manual.sessions, manual.request, [name])
    after = compute_features(store, manual.sessions, manual.request, [name])
    pd.testing.assert_frame_equal(before.values, after.values, check_exact=True)
    assert before.source_trace == after.source_trace
    with pytest.raises(ContractError, match="scope namespace/version"):
        compute_features(
            store,
            manual.sessions,
            replace(manual.request, as_of=known, decision_time=known),
            [name],
        )


@pytest.mark.parametrize("name", ["BREADTH_ADVANCE_RATIO", "FLOW_NET_ACTIVE_RATIO"])
def test_unrequested_invalid_classification_remains_out_of_feature_inputs(manual, name):
    before = compute_features(manual.store(), manual.sessions, manual.request, [name])
    bad = [
        (
            replace(r, payload=dict(r.payload, namespace=[], taxonomy_version=""))
            if r.kind == "scope" and r.payload["family"] in {"industry", "concept"}
            else r
        )
        for r in manual.records
    ]
    after = compute_features(AsOfStore(bad), manual.sessions, manual.request, [name])
    pd.testing.assert_frame_equal(before.values, after.values, check_exact=True)
    assert before.source_trace == after.source_trace
