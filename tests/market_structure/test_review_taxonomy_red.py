"""Reviewed source must reject an unnamed classification before asserting zero."""

from dataclasses import replace

import pytest

from qbot.research.exploratory.market_structure.contracts import ContractError
from qbot.research.exploratory.market_structure.features import compute_features
from qbot.research.exploratory.market_structure.store import AsOfStore


@pytest.mark.parametrize("family,name", [("industry", "IND_RET"), ("concept", "THEME_MEMBERSHIP_COUNT")])
def test_empty_taxonomy_cannot_prove_complete_empty_membership(manual, family, name):
    records = [
        replace(r, payload=dict(r.payload, taxonomy_version=""))
        if r.kind == "scope" and r.payload["family"] == family else r
        for r in manual.records
        if not (r.kind == "membership" and r.payload["family"] == family)
    ]
    with pytest.raises(ContractError, match="scope namespace/version"):
        compute_features(AsOfStore(records), manual.sessions, manual.request, [name])
