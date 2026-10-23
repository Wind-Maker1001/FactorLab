"""Strict shared provenance, with history/date checks scoped to actual inputs."""

from dataclasses import replace

import pandas as pd
import pytest

from qbot.research.exploratory.market_structure.contracts import ContractError, instant
from qbot.research.exploratory.market_structure.features import compute_features
from qbot.research.exploratory.market_structure.store import AsOfStore


@pytest.mark.parametrize("value", [1, False, "", " ", ["id"], {"id": 1}])
def test_fact_id_is_a_nonblank_string_before_store_identity_sorting(manual, value):
    with pytest.raises(ContractError, match="identity"):
        replace(manual.records[0], fact_id=value)


@pytest.mark.parametrize(
    "family,name", [("industry", "IND_RET"), ("concept", "THEME_MEMBERSHIP_COUNT")]
)
@pytest.mark.parametrize(
    "value",
    ["not-a-date", 20210103, "2021-02-29", "2021-01-03T00:00:00Z", "20210103", " "],
)
def test_membership_change_date_requires_a_real_canonical_date(
    manual, family, name, value
):
    records = [
        (
            replace(r, payload=dict(r.payload, change_date=value))
            if r.kind == "membership" and r.payload["family"] == family
            else r
        )
        for r in manual.records
    ]
    with pytest.raises(ContractError, match="change_date"):
        compute_features(AsOfStore(records), manual.sessions, manual.request, [name])


@pytest.mark.parametrize(
    "family,name", [("industry", "IND_RET"), ("concept", "THEME_MEMBERSHIP_COUNT")]
)
def test_future_invalid_date_revision_is_not_validated_until_its_known_time(
    manual, family, name
):
    original = next(
        r
        for r in manual.records
        if r.kind == "membership" and r.payload["family"] == family
    )
    known = manual.request.as_of + pd.Timedelta(seconds=1)
    revision = replace(
        original,
        revision=2,
        published_at=known,
        available_at=known,
        captured_at=known,
        payload=dict(original.payload, change_date="bad"),
    )
    before = compute_features(manual.store(), manual.sessions, manual.request, [name])
    store = AsOfStore(manual.records + [revision])
    after = compute_features(store, manual.sessions, manual.request, [name])
    pd.testing.assert_frame_equal(before.values, after.values, check_exact=True)
    with pytest.raises(ContractError, match="change_date"):
        compute_features(
            store,
            manual.sessions,
            replace(manual.request, as_of=known, decision_time=known),
            [name],
        )


@pytest.mark.parametrize("name", ["BREADTH_ADVANCE_RATIO", "SC_UPSTREAM_COUNT"])
def test_unrequested_bad_membership_date_does_not_poison_other_features(manual, name):
    bad = [
        (
            replace(r, payload=dict(r.payload, change_date="bad"))
            if r.kind == "membership"
            else r
        )
        for r in manual.records
    ]
    before = compute_features(manual.store(), manual.sessions, manual.request, [name])
    after = compute_features(AsOfStore(bad), manual.sessions, manual.request, [name])
    pd.testing.assert_frame_equal(before.values, after.values, check_exact=True)


def test_valid_leap_date_is_date_metadata_without_implicit_publication_time(manual):
    records = [
        (
            replace(r, payload=dict(r.payload, change_date="2020-02-29"))
            if r.kind == "membership"
            else r
        )
        for r in manual.records
    ]
    result = compute_features(
        AsOfStore(records), manual.sessions, manual.request, ["THEME_MEMBERSHIP_COUNT"]
    )
    assert result.values.reason.eq("OK").all()
    assert result.values.value.eq(2).all()


@pytest.mark.parametrize(
    "field,value",
    [
        ("definition_version", 7),
        ("definition_version", " "),
        ("evidence", "reference"),
        ("evidence", [" "]),
        ("payload", ["definition_id", "vendor", "unit", "currency"]),
        ("kind", ["scope"]),
        ("source", ["SYNTHETIC_TEST_ONLY"]),
    ],
)
def test_shared_record_envelope_meets_interchange_types(manual, field, value):
    with pytest.raises(ContractError):
        replace(manual.records[0], **{field: value})


@pytest.mark.parametrize("value", ["not-an-instant", ["bad"]])
def test_malformed_instants_fail_as_contract_errors(value):
    with pytest.raises(ContractError, match="instant"):
        instant(value)


def test_non_json_or_nonfinite_evidence_has_an_explicit_contract_error(manual):
    original = manual.records[0]
    with pytest.raises(ContractError, match="JSON-compatible"):
        replace(original, payload=dict(original.payload, unsupported=float("inf")))
