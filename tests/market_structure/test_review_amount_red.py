"""Original amount-dependency counterexamples; first run red on 58dc13b."""

from dataclasses import replace

import numpy as np

from factorlab.exploratory.market_structure.features import compute_features
from factorlab.exploratory.market_structure.store import AsOfStore


def bad_prices(manual):
    return AsOfStore([
        replace(r, payload=dict(r.payload, open="INVALID", volume="INVALID"))
        if r.kind == "minute" else r for r in manual.records
    ])


def test_review_red_amount_only_ignores_invalid_prices(manual):
    result = compute_features(bad_prices(manual), manual.sessions, manual.request,
                              ["FLOW_NET_ACTIVE_RATIO", "SENTIMENT_AMOUNT_HHI"])
    assert result.values.value.notna().all()
    np.testing.assert_allclose(result.values.query("feature_id=='FLOW_NET_ACTIVE_RATIO'").value, 0.2)


def test_review_red_mixed_request_retains_valid_amount_values(manual):
    result = compute_features(bad_prices(manual), manual.sessions, manual.request)
    cash = result.values.query("feature_id=='FLOW_NET_ACTIVE_RATIO'")
    assert cash.value.notna().all()
    assert result.values.query("feature_id=='BREADTH_ADVANCE_RATIO'").value.isna().all()
