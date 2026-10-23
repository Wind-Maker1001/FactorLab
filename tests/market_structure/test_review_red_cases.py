"""Three minimal counterexamples, first executed against b30256d unchanged."""

import numpy as np
import pandas as pd
import pytest

from qbot.research.exploratory.market_structure.adapters import ResearchFeatureAdapter
from qbot.research.exploratory.market_structure.diagnostics import (
    RedundancyPolicy,
    diagnose_features,
)
from qbot.research.exploratory.market_structure.features import compute_features
from qbot.research.exploratory.market_structure.store import AsOfStore
from tests.market_structure.test_diagnostics import panel


def target_index(request, universe):
    fields = (
        "scenario", "frequency", "session_id", "observation_start",
        "observation_end", "as_of", "decision_time",
    )
    return pd.DataFrame(
        [dict({key: getattr(request, key) for key in fields}, entity_id=s)
         for s in universe]
    )


def test_review_red_spearman_uses_paired_ranks():
    frame = panel(entities=4, windows=1, sessions=1)
    for name, values in (("A", [1, 2, 3, np.nan]), ("B", [1, 2, 100, 1.5])):
        frame.loc[frame.feature_id == name, "value"] = values
    result = diagnose_features(
        frame,
        RedundancyPolicy(
            minimum_security_entities=3, minimum_observation_windows=1,
            minimum_trading_sessions=1, minimum_pair_coverage=0.75,
            correlation_threshold=0.99,
        ),
    )
    assert result["redundancy_windows"].iloc[0].spearman == pytest.approx(1.0)
    assert result["redundancy"].iloc[0].status == "HIGHLY_REDUNDANT"


def test_review_red_breadth_does_not_require_classifications(manual):
    store = AsOfStore(r for r in manual.records if r.kind == "minute")
    result = compute_features(
        store, manual.sessions, manual.request, ["BREADTH_ADVANCE_RATIO"]
    )
    assert result.values.iloc[0].value == pytest.approx(0.5)


def test_review_red_market_only_broadcast_keeps_target_stocks(manual):
    result = compute_features(manual.store(), manual.sessions, manual.request)
    market = result.values[result.values.feature_id == "BREADTH_ADVANCE_RATIO"]
    view = ResearchFeatureAdapter().build_view(
        market, ["BREADTH_ADVANCE_RATIO"], broadcast_market=True,
        target_index=target_index(manual.request, manual.universe),
    )
    assert len(view) == 4
    assert set(view.entity_id) == set(manual.universe)
    assert view.BREADTH_ADVANCE_RATIO.eq(0.5).all()
