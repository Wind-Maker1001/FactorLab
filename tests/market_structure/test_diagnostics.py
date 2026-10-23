from dataclasses import replace
from io import StringIO

import numpy as np
import pandas as pd
import pytest

import qbot.research.exploratory.market_structure.diagnostics as module
from qbot.research.exploratory.market_structure.contracts import ContractError
from qbot.research.exploratory.market_structure.diagnostics import (
    RedundancyPolicy,
    diagnose_features,
    structural_diagnostics,
)
from qbot.research.exploratory.market_structure.registry import (
    MarketStructureRegistry,
    build_default_registry,
)


def panel(
    grain="security", entities=24, sessions=3, windows=20, coverage=1.0, kind="copy"
):
    rows = []
    for window in range(windows):
        session = window % sessions
        end = pd.Timestamp("2021-01-04T02:00:00Z") + pd.Timedelta(
            days=session, minutes=(window // sessions) * 5
        )
        for i in range(entities):
            a = float(i) if grain != "market" else float(window)
            b = (
                a
                if kind == "copy"
                else -a if kind == "negative" else float((i * 7 + window) % 11)
            )
            if i >= entities * coverage:
                b = np.nan
            for feature, val in (("A", a), ("B", b)):
                rows.append(
                    dict(
                        scenario="controlled",
                        frequency=5,
                        grain=grain,
                        namespace="test",
                        feature_id=feature,
                        entity_id=str(i),
                        observation_end=end.isoformat(),
                        as_of=(end + pd.Timedelta(seconds=1)).isoformat(),
                        decision_time=(end + pd.Timedelta(seconds=1)).isoformat(),
                        session_id="SESSION_%d" % session,
                        value=val,
                        reason="OK",
                        input_coverage=coverage,
                    )
                )
    return pd.DataFrame(rows)


def test_independent_adapter_passes_explicit_options_and_keeps_last_intraday_window(
    monkeypatch,
):
    actual = module.diagnose_redundancy
    seen = []

    def spy(frame, candidates, originals, config, deadline=None):
        seen.append(config)
        assert not config.__class__.__name__ == "DiscoveryConfig"
        assert config.minimum_cross_section == 20
        assert config.minimum_evaluation_dates == 20
        assert config.minimum_pair_coverage == 0.8
        assert config.redundancy_threshold == 0.98
        assert config.development_end.hour == 2 and config.development_end.minute > 0
        return actual(frame, candidates, originals, config, deadline)

    monkeypatch.setattr(module, "diagnose_redundancy", spy)
    d = diagnose_features(panel())
    row = d["redundancy"].iloc[0]
    assert len(seen) == 2
    assert row.status == "NUMERIC_DUPLICATE"
    assert row.valid_observation_windows == 20 and row.distinct_trading_sessions == 3
    assert len(d["redundancy_windows"]) == 20
    assert not {"valid_dates", "development_dates"} & set(d["redundancy"].columns)


def test_many_minutes_on_one_day_never_become_many_trading_days():
    row = diagnose_features(panel(sessions=1, windows=60))["redundancy"].iloc[0]
    assert row.valid_observation_windows == 60 and row.distinct_trading_sessions == 1
    assert row.status == "UNDETERMINED"


@pytest.mark.parametrize("grain", ["industry", "concept"])
def test_group_entities_use_three_not_twenty(grain):
    good = diagnose_features(panel(grain=grain, entities=3))["redundancy"].iloc[0]
    bad = diagnose_features(panel(grain=grain, entities=2))["redundancy"].iloc[0]
    assert good.status == "NUMERIC_DUPLICATE" and good.minimum_entities == 3
    assert bad.status == "UNDETERMINED"


def test_pair_coverage_filters_each_window_even_when_mean_is_good():
    f = panel(windows=30)
    ends = f.observation_end.unique()
    # 10 poorly covered windows; mean paired coverage remains above 80%.
    bad = (
        f.observation_end.isin(ends[:10])
        & (f.feature_id == "B")
        & f.entity_id.astype(int).ge(16)
    )
    f.loc[bad, "value"] = np.nan
    row = diagnose_features(f)["redundancy"].iloc[0]
    assert row.mean_pair_coverage > 0.8
    assert row.valid_observation_windows == 20
    assert row.status == "UNDETERMINED"  # valid-window fraction is below 80%.


def test_negative_high_correlation_and_grain_isolation():
    security = panel(kind="negative")
    industry = panel(grain="industry", entities=4)
    d = diagnose_features(pd.concat([security, industry], ignore_index=True))
    assert len(d["redundancy"]) == 2
    assert set(d["redundancy"].status) == {"HIGHLY_REDUNDANT", "NUMERIC_DUPLICATE"}
    assert len(d["rejections"]) == 2


def test_market_scalar_is_time_series_and_constant_is_not_cross_section_failure():
    market = panel(grain="market", entities=1)
    d = diagnose_features(market)
    assert d["quality"].cross_section_status.eq("NOT_APPLICABLE").all()
    assert d["quality"].cross_section_constant_windows.eq(0).all()
    assert d["redundancy"].diagnostic_kind.eq("TIME_SERIES_DESCRIPTIVE").all()
    assert d["redundancy"].status.eq("NUMERIC_DUPLICATE").all()


def test_constant_and_insufficient_coverage_preserve_rejection_evidence():
    f = panel()
    f.loc[f.feature_id == "A", "value"] = 1.0
    f.loc[(f.feature_id == "B") & f.entity_id.astype(int).gt(2), "value"] = np.nan
    result = diagnose_features(f)
    assert set(result["rejections"].reason) == {"CONSTANT", "INSUFFICIENT_COMPLETENESS"}
    assert set(result["quality"].feature_id) == {"A", "B"}


def test_duplicate_rows_and_mixed_asof_are_rejected():
    f = panel()
    with pytest.raises(ContractError, match="duplicate"):
        diagnose_features(pd.concat([f, f.iloc[:1]]))
    f.loc[0, "as_of"] = "2021-01-04T01:00:00Z"
    with pytest.raises(ContractError, match="mixed"):
        diagnose_features(f)


def test_structure_includes_scope_semantics_and_keeps_duplicates():
    original = build_default_registry().get("SC_UPSTREAM_COUNT")
    registry = MarketStructureRegistry(
        [
            original,
            replace(original, feature_id="DUPLICATE"),
            replace(
                original,
                feature_id="OTHER_SCOPE",
                required_inputs=("another taxonomy",),
            ),
        ]
    )
    result = structural_diagnostics(registry)
    assert result.status.tolist() == [
        "DISTINCT_CONTRACT",
        "STRUCTURAL_DUPLICATE",
        "DISTINCT_CONTRACT",
    ]
    assert result.iloc[1].duplicate_of == original.feature_id


@pytest.mark.parametrize(
    "kwargs", [dict(minimum_security_entities=0), dict(minimum_pair_coverage=1.1)]
)
def test_invalid_policy(kwargs):
    with pytest.raises(ContractError):
        RedundancyPolicy(**kwargs)


def test_empty_diagnostic_evidence_keeps_parseable_csv_schema():
    f = panel()
    d = diagnose_features(f[f.feature_id == "A"])
    for name in ("redundancy", "redundancy_windows", "rejections"):
        assert d[name].empty
        parsed = pd.read_csv(StringIO(d[name].to_csv(index=False)))
        assert parsed.empty and {
            "scenario",
            "frequency",
            "grain",
            "namespace",
        }.issubset(parsed.columns)
    assert {"feature_id", "reason", "evidence"}.issubset(d["rejections"].columns)
