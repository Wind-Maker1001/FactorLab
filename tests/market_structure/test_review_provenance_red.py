"""Facts and historical date evidence must meet their declared interchange types."""

from dataclasses import replace

import pytest

from factorlab.exploratory.market_structure.contracts import ContractError
from factorlab.exploratory.market_structure.features import compute_features
from factorlab.exploratory.market_structure.store import AsOfStore


def test_non_string_fact_identity_is_rejected_at_construction(manual):
    with pytest.raises(ContractError, match="identity"):
        replace(manual.records[0], fact_id=7)


def test_invalid_membership_change_date_cannot_be_valid_historical_evidence(manual):
    records = [replace(r, payload=dict(r.payload, change_date="not-a-date"))
               if r.kind == "membership" and r.payload["family"] == "concept" else r for r in manual.records]
    with pytest.raises(ContractError, match="change_date"):
        compute_features(AsOfStore(records), manual.sessions, manual.request, ["THEME_MEMBERSHIP_COUNT"])
